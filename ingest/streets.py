"""Street centerlines (Charlotte/Mecklenburg) into `streets`, for lot frontage.

Run with `make streets`. Source and filters live under `streets` in config/features.yaml.
Every segment is stored (with history); which ones give frontage is decided in
features/build.py from the config filters.
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

from ingest.db import copy_dataframe, get_engine
from ingest.versioning import apply_versioned, row_hash

ROOT = Path(__file__).resolve().parent.parent
CRS_EPSG = 2264
PAGE = 4000

FIELDS = {
    "OBJECTID": "objectid",
    "LSTREETCODE": "street_code",
    "WHOLESTNAME": "name",
    "STREETCLASS": "street_class",
    "THOROUGHFARETYPE": "thoroughfare",
    "LIFECYCLESTATUS": "lifecycle",
    "L_JURIS": "jurisdiction",
}


def download(url: str, out_dir: Path, today: date) -> Path:
    """Save the full centerline layer as GeoJSON (WGS84), paging through results."""
    features, offset = [], 0
    while True:
        q = urllib.parse.urlencode({"where": "1=1", "outFields": ",".join(FIELDS), "outSR": 4326, "f": "geojson",
                                    "resultOffset": offset, "resultRecordCount": PAGE, "orderByFields": "OBJECTID"})
        with urllib.request.urlopen(f"{url}/query?{q}", timeout=300) as r:
            page = json.load(r)
        if "error" in page:
            raise RuntimeError(f"{url}: {page['error']}")
        features += page["features"]
        if len(page["features"]) < PAGE:
            break
        offset += PAGE
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"streets_{today:%Y-%m-%d}.geojson"
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    return path


def read(path: Path) -> gpd.GeoDataFrame:
    g = gpd.read_file(path).to_crs(CRS_EPSG)
    df = g[list(FIELDS)].rename(columns=FIELDS)
    df["objectid"] = df["objectid"].astype("int64")
    df["street_code"] = pd.to_numeric(df["street_code"], errors="coerce").astype("Int64")
    df["source_file"] = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else path.name
    geoms = shapely.force_2d(g.geometry.values)
    df["geom"] = [x if x.geom_type == "MultiLineString" else shapely.MultiLineString([x]) for x in geoms]
    df = df[~df["geom"].apply(lambda x: x.is_empty)]
    return gpd.GeoDataFrame(df.reset_index(drop=True), geometry="geom", crs=CRS_EPSG)


def _rows(chunk: pd.DataFrame) -> pd.DataFrame:
    geoms = shapely.set_srid(chunk["geom"].values, CRS_EPSG)
    rows = pd.DataFrame(chunk.drop(columns="geom")).assign(geom=shapely.to_wkb(geoms, hex=True, include_srid=True))
    return rows.assign(row_hash=row_hash(rows.drop(columns="source_file")))


def load(streets: gpd.GeoDataFrame, engine: Engine, as_of: date, schema: str = "public") -> dict:
    table = f"{schema}.streets"
    with engine.begin() as conn:
        copy_dataframe(conn, streets, f"{table}_staging", {"geom": f"geometry(MultiLineString, {CRS_EPSG})"}, _rows)
        changes = apply_versioned(conn, table, ["objectid"], as_of)
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS streets_geom_idx ON {table} USING gist (geom)"))
    return changes


def main() -> None:
    cfg = yaml.safe_load((ROOT / "config" / "features.yaml").read_text())["streets"]
    today = date.today()
    path = download(cfg["url"], ROOT / cfg["download_dir"], today)
    streets = read(path)
    changes = load(streets, get_engine(), today)
    with get_engine().begin() as conn:
        conn.execute(
            text("INSERT INTO ingest_runs (source, snapshot_date, source_file, snapshot_written, report) "
                 "VALUES ('streets', :d, :f, :w, CAST(:r AS jsonb))"),
            {"d": today, "f": path.relative_to(ROOT).as_posix(), "w": bool(changes["added"] or changes["changed"]
                                                                          or changes["removed"]),
             "r": json.dumps({"segments": len(streets), "changes": changes})},
        )
    print(f"Streets: {len(streets):,} segments from {path.relative_to(ROOT).as_posix()}; {changes}")


if __name__ == "__main__":
    main()
