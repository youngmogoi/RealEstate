"""Charlotte Future 2040 Policy Map place types into `policy_map`, and one per parcel.

Run with `make place-types`. Source and settings live under `policy_map` in config/ingest.yaml
(the city's ArcGIS Online layer behind its official Policy Map viewer; scriptable).

Tables (each with a `_history` table; see ingest/versioning.py):
- policy_map: one row per published place-type shape.
- parcel_place_types: per parcel, the place type covering the largest share of it, the
  runner-up, and `place_type_split`. The layer covers Charlotte's planning jurisdiction only;
  other parcels get no row values (NULL).

The layer's short code (PlaceTypeCde) disagrees with the full name on a few shapes (2026-10:
10 of 5,649, e.g. "Neighborhood 2" coded N1). The full name is used; such parcels get
place_type_code_mismatch.

The townhome rezoning yield reads the place type (config/zoning_rules.yaml rezoning_targets).
"""

import json
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

import geopandas as gpd
import pandas as pd
import shapely
import yaml
from sqlalchemy import Engine, text

from ingest.common import _polygonal, fix_geometries
from ingest.db import copy_dataframe, get_engine
from ingest.versioning import apply_versioned, create_staging_from_query, row_hash

ROOT = Path(__file__).resolve().parent.parent
CRS_EPSG = 2264

FIELDS = {
    "OBJECTID": "objectid",
    "PlaceTypeFullTxt": "place_type",
    "PlaceTypeCde": "place_type_code",
    "AdoptionType": "adoption_type",
    "AdoptionDate": "adoption_date",
    "CommunityPlanningArea": "planning_area",
}


def download(url: str, out_dir: Path, today: date, page: int) -> Path:
    """Save the full layer as GeoJSON (WGS84), paging through results."""
    features, offset = [], 0
    while True:
        q = urllib.parse.urlencode({"where": "1=1", "outFields": ",".join(FIELDS), "outSR": 4326, "f": "geojson",
                                    "resultOffset": offset, "resultRecordCount": page, "orderByFields": "OBJECTID"})
        with urllib.request.urlopen(f"{url}/query?{q}", timeout=300) as r:
            data = json.load(r)
        if "error" in data:
            raise RuntimeError(f"{url}: {data['error']}")
        features += data["features"]
        if len(data["features"]) < page:
            break
        offset += page
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"policy_map_{today:%Y-%m-%d}.geojson"
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    return path


def read(path: Path, codes: dict[str, str]) -> gpd.GeoDataFrame:
    g = gpd.read_file(path).to_crs(CRS_EPSG)
    df = g[list(FIELDS)].rename(columns=FIELDS)
    df["objectid"] = df["objectid"].astype("int64")
    df["adoption_date"] = pd.to_datetime(df["adoption_date"], unit="ms", errors="coerce").dt.date
    df["place_type_code_mismatch"] = df["place_type"].map(codes) != df["place_type_code"].str.upper()
    df["source_file"] = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else path.name
    geoms, _ = fix_geometries(gpd.GeoSeries(shapely.force_2d(g.geometry.values), crs=CRS_EPSG))
    df["geom"] = [x if x.geom_type == "MultiPolygon" else _polygonal(x) for x in geoms]
    df = df[~df["geom"].apply(lambda x: x is None or x.is_empty)]
    return gpd.GeoDataFrame(df.reset_index(drop=True), geometry="geom", crs=CRS_EPSG)


def _rows(chunk: pd.DataFrame) -> pd.DataFrame:
    geoms = shapely.set_srid(chunk["geom"].values, CRS_EPSG)
    rows = pd.DataFrame(chunk.drop(columns="geom")).assign(geom=shapely.to_wkb(geoms, hex=True, include_srid=True))
    return rows.assign(row_hash=row_hash(rows.drop(columns="source_file")))


def load(shapes: gpd.GeoDataFrame, engine: Engine, as_of: date, schema: str = "public") -> dict:
    table = f"{schema}.policy_map"
    with engine.begin() as conn:
        copy_dataframe(conn, shapes, f"{table}_staging", {"geom": f"geometry(MultiPolygon, {CRS_EPSG})"}, _rows)
        changes = apply_versioned(conn, table, ["objectid"], as_of)
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS policy_map_geom_idx ON {table} USING gist (geom)"))
    return changes


ASSIGN_SQL = """
WITH hits AS (
    SELECT p.pin, m.place_type, bool_or(m.place_type_code_mismatch) AS mismatch,
           sum(ST_Area(ST_Intersection(p.geom, m.g))) / max(ST_Area(p.geom)) AS share
    FROM {s}.parcels p JOIN {s}.policy_map_pieces m ON ST_Intersects(p.geom, m.g)
    GROUP BY p.pin, m.place_type
),
ranked AS (
    SELECT *, row_number() OVER (PARTITION BY pin ORDER BY share DESC, place_type) AS n FROM hits WHERE share > 0
)
SELECT p.pin, r1.place_type, r1.share AS place_type_share,
       coalesce(r1.mismatch, false) AS place_type_code_mismatch,
       r2.place_type AS second_place_type, r2.share AS second_place_type_share,
       coalesce(r2.share >= :m, false) AS place_type_split
FROM {s}.parcels p
LEFT JOIN ranked r1 ON r1.pin = p.pin AND r1.n = 1
LEFT JOIN ranked r2 ON r2.pin = p.pin AND r2.n = 2
"""


def assign_parcels(engine: Engine, as_of: date, split_min_share: float, schema: str = "public") -> dict:
    s = schema
    with engine.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS {s}.policy_map_pieces"))
        conn.execute(text(
            f"CREATE UNLOGGED TABLE {s}.policy_map_pieces AS SELECT place_type, place_type_code_mismatch, "
            f"ST_Subdivide(geom, 256) AS g FROM {s}.policy_map"))
        conn.execute(text(f"CREATE INDEX ON {s}.policy_map_pieces USING gist (g)"))
        conn.execute(text(f"ANALYZE {s}.policy_map_pieces"))
        create_staging_from_query(conn, f"{s}.parcel_place_types_staging", ASSIGN_SQL.format(s=s),
                                  {"m": split_min_share})
        changes = apply_versioned(conn, f"{s}.parcel_place_types", ["pin"], as_of)
        conn.execute(text(f"DROP TABLE {s}.policy_map_pieces"))
    return changes


def quality_report(engine: Engine, schema: str = "public") -> dict:
    s = schema
    with engine.connect() as conn:
        shapes, mismatched = conn.execute(text(
            f"SELECT count(*), count(*) FILTER (WHERE place_type_code_mismatch) FROM {s}.policy_map")).one()
        by_type = dict(conn.execute(text(
            f"SELECT coalesce(place_type, '(outside the map)'), count(*) FROM {s}.parcel_place_types "
            f"GROUP BY 1 ORDER BY 2 DESC")).all())
        split, mismatch = conn.execute(text(
            f"SELECT count(*) FILTER (WHERE place_type_split), count(*) FILTER (WHERE place_type_code_mismatch) "
            f"FROM {s}.parcel_place_types")).one()
    return {"shapes": shapes, "shapes_code_mismatch": mismatched, "parcels_by_place_type": by_type,
            "parcels_split": split, "parcels_code_mismatch": mismatch}


def main() -> None:
    cfg = yaml.safe_load((ROOT / "config" / "ingest.yaml").read_text(encoding="utf-8"))["policy_map"]
    today = date.today()
    path = download(cfg["url"], ROOT / cfg["download_dir"], today, cfg["page_size"])
    shapes = read(path, cfg["codes"])
    engine = get_engine()
    changes = {"policy_map": load(shapes, engine, today)}
    changes["parcel_place_types"] = assign_parcels(engine, today, cfg["split_min_share"])
    report = quality_report(engine)
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO ingest_runs (source, snapshot_date, source_file, snapshot_written, report) "
                 "VALUES ('policy_map', :d, :f, :w, CAST(:r AS jsonb))"),
            {"d": today, "f": path.relative_to(ROOT).as_posix(),
             "w": any(c["added"] or c["changed"] or c["removed"] for c in changes.values()),
             "r": json.dumps({**report, "changes": changes}, default=str)})
    print(f"Policy Map: {report['shapes']:,} shapes ({report['shapes_code_mismatch']} with a code that disagrees "
          f"with the name) from {path.relative_to(ROOT).as_posix()}")
    print("  parcels by place type:")
    for t, n in report["parcels_by_place_type"].items():
        print(f"    {t:<28} {n:>8,}")
    print(f"  parcels split between place types: {report['parcels_split']:,}; "
          f"on a mismatched-code shape: {report['parcels_code_mismatch']:,}")
    for t, c in changes.items():
        print(f"  {t}: {c}")


if __name__ == "__main__":
    main()
