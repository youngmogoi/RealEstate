"""Estimate sewer and water status per parcel into `parcel_utilities`.

Run with `make utilities` (after `make ingest`, which loads parcels and owners). Settings live
under `utilities` in config/ingest.yaml.

Charlotte Water does not publish its line GIS, so status is estimated from these sources, in
priority order, and every estimate records its confidence and evidence:

1. Charlotte Water files under their security agreement (high). Not implemented: if anything
   appears in `restricted_dir`, the run stops and asks. That data must never reach deal
   sheets, maps, or anything shared.
2. Rezoning petition comments (medium). TODO(phase 3): deferred 2026-10-27 to share the
   minutes watcher's scraper and extractor; findings for building it are in CLAUDE.md.
3. City capital projects: water and sewer project areas, stored in `cip_projects`. They set
   `planned_project_*`, not the status.
4. Proxies (low), recorded in `<kind>_source`:
   - plat_first_home: the parcel is on a recorded plat (the "M<book>-<page>" reference in the
     county legal description) whose first home was built after `plat_after_year`. Year built
     comes from the NC OneMap snapshot of 2026-01-07; the county ownership file has none.
   - plat_age_fallback: the plat has no home in that data yet, its book number is recent,
     the parcel is inside a municipal limit (county tax municipality), and the plat's median
     lot is small. Thresholds under `plat_fallback`.

Status is "served" (low) when a proxy holds, else "unknown". Water uses the sewer proxies
until there is water-specific data. "nearby" and "unserved" need
sources 1-2 or septic records, which we don't have yet; distances stay null until then.
Scoring must treat low confidence as a risk, not a fact. In a county that publishes line
layers, replace this module's sources with distances to those lines and set confidence high,
keeping the same output columns.
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

from ingest.common import fix_geometries, strip_strings
from ingest.db import copy_dataframe, get_engine
from ingest.versioning import apply_versioned, create_staging_from_query, row_hash

ROOT = Path(__file__).resolve().parent.parent
CRS_EPSG = 2264
PAGE = 1000

CIP_FIELDS = {
    "Project_ID": "project_id",
    "Project_Name": "name",
    "Project_Type": "project_type",
    "Project_Phase": "phase",
    "Public_Project_Description": "description",
    "Location_Description": "location",
    "Anticipated_Start_Date_Year": "start_year",
    "Anticipated_Compl_Date_Year": "completion_year",
    "Controlling_Entity": "controlling_entity",
    "OBJECTID": "source_objectid",
}


def check_restricted(restricted_dir: Path) -> None:
    """Stop if Charlotte Water files have appeared: they need handling rules agreed first."""
    if restricted_dir.exists() and any(p.is_file() for p in restricted_dir.rglob("*")):
        raise SystemExit(
            f"Found files in {restricted_dir} (Charlotte Water, restricted). The utilities ingest does not "
            "read them yet: ask before wiring them in, since they must never appear on anything shared."
        )


def download_cip(url: str, out_dir: Path, today: date) -> Path:
    """Save the full capital-project polygon layer as GeoJSON (WGS84), paging through results."""
    features, offset = [], 0
    while True:
        query = urllib.parse.urlencode({
            "where": "1=1", "outFields": "*", "outSR": 4326, "f": "geojson",
            "resultOffset": offset, "resultRecordCount": PAGE, "orderByFields": "OBJECTID",
        })
        with urllib.request.urlopen(f"{url}/query?{query}", timeout=120) as r:
            page = json.load(r)
        if "error" in page:
            raise RuntimeError(f"{url}: {page['error']}")
        features += page["features"]
        if len(page["features"]) < PAGE:
            break
        offset += PAGE
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"charlotte_cip_polygons_{today:%Y-%m-%d}.geojson"
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    return path


def read_cip(path: Path, types: list[str], planned_phases: list[str]) -> gpd.GeoDataFrame:
    gdf = gpd.read_file(path).to_crs(CRS_EPSG)
    gdf = strip_strings(gdf[gdf["Project_Type"].isin(types)].copy())
    df = gdf[list(CIP_FIELDS)].rename(columns=CIP_FIELDS)
    for col in ("start_year", "completion_year"):
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
    df["source_objectid"] = df["source_objectid"].astype("int64")
    df["planned"] = df["phase"].isin(planned_phases)
    df["source_file"] = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else path.name
    geoms, _ = fix_geometries(gdf.geometry)
    df["geom"] = [g if g.geom_type == "MultiPolygon" else shapely.MultiPolygon([g]) for g in geoms]
    return gpd.GeoDataFrame(df.reset_index(drop=True), geometry="geom", crs=CRS_EPSG)


def _cip_rows(chunk: pd.DataFrame) -> pd.DataFrame:
    geoms = shapely.set_srid(chunk["geom"].values, CRS_EPSG)
    rows = pd.DataFrame(chunk.drop(columns="geom")).assign(geom=shapely.to_wkb(geoms, hex=True, include_srid=True))
    return rows.assign(row_hash=row_hash(rows))


def load_cip(cip: gpd.GeoDataFrame, engine: Engine, as_of: date, schema: str = "public") -> dict:
    table = f"{schema}.cip_projects"
    with engine.begin() as conn:
        copy_dataframe(conn, cip, f"{table}_staging", {"geom": f"geometry(MultiPolygon, {CRS_EPSG})"}, _cip_rows)
        changes = apply_versioned(conn, table, ["source_objectid"], as_of)
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS cip_projects_geom_idx ON {table} USING gist (geom)"))
    return changes


ASSIGN_SQL = """
WITH refs AS (
    SELECT pin, mode() WITHIN GROUP (ORDER BY ref) AS plat_ref
    FROM (SELECT pin, substring(legal_description from 'M(\\d+-\\d+)') AS ref FROM {s}.parcel_owners) o
    WHERE ref IS NOT NULL GROUP BY pin
),
muni AS (
    SELECT pin, mode() WITHIN GROUP (ORDER BY municipality) AS municipality FROM {s}.parcel_owners GROUP BY pin
),
built AS (
    SELECT pin, min(year_built) AS year_built FROM {s}.nconemap_parcels_snapshots
    WHERE year_built > 1800 GROUP BY pin
),
plats AS (
    SELECT r.plat_ref, min(b.year_built) AS plat_year, count(*) AS plat_homes,
           (array_agg(r.pin ORDER BY b.year_built, r.pin))[1] AS plat_first_home_pin
    FROM refs r JOIN built b USING (pin) GROUP BY r.plat_ref
),
lots AS (
    SELECT r.plat_ref, count(*) AS plat_lots,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY ST_Area(p.geom) / 43560) AS plat_median_lot_acres
    FROM refs r JOIN {s}.parcels p USING (pin) GROUP BY r.plat_ref
),
base AS (
    SELECT p.pin, r.plat_ref, split_part(r.plat_ref, '-', 1)::int AS plat_book,
           pl.plat_year, pl.plat_homes, pl.plat_first_home_pin, l.plat_lots, l.plat_median_lot_acres,
           m.municipality,
           coalesce(m.municipality IS NOT NULL AND NOT (m.municipality = ANY(:outside)), false) AS in_municipal_limit
    FROM {s}.parcels p LEFT JOIN refs r USING (pin) LEFT JOIN plats pl USING (plat_ref)
    LEFT JOIN lots l USING (plat_ref) LEFT JOIN muni m USING (pin)
),
proxy AS (
    SELECT b.*,
           CASE WHEN b.plat_year > :after THEN 'plat_first_home'
                WHEN b.plat_year IS NULL AND b.plat_book BETWEEN :book_min AND :book_max
                     AND b.in_municipal_limit AND b.plat_median_lot_acres < :max_lot THEN 'plat_age_fallback'
           END AS proxy_rule
    FROM base b
)
SELECT x.pin,
{status_cols},
       x.plat_ref, x.plat_book, x.plat_year, x.plat_homes, x.plat_first_home_pin, x.plat_lots, x.plat_median_lot_acres,
       x.municipality, x.in_municipal_limit,
       pr.project_id IS NOT NULL AS planned_project_nearby,
       pr.project_id AS planned_project_id, pr.name AS planned_project_name,
       pr.project_type AS planned_project_type, pr.phase AS planned_project_phase,
       pr.start_year AS planned_project_start_year, pr.completion_year AS planned_project_completion_year,
       pr.distance_ft AS planned_project_distance_ft
FROM proxy x
JOIN {s}.parcels p USING (pin)
LEFT JOIN LATERAL (
    SELECT c.project_id, c.name, c.project_type, c.phase, c.start_year, c.completion_year,
           ST_Distance(c.geom, p.geom) AS distance_ft
    FROM {s}.cip_projects c
    WHERE c.planned AND ST_DWithin(c.geom, p.geom, :within)
    ORDER BY c.geom <-> p.geom, c.project_id LIMIT 1
) pr ON true
"""


def _status_cols(kind: str) -> str:
    """Status from the proxy rule, with its confidence, source, and the date of its evidence.

    Water uses the sewer proxy for now (see CLAUDE.md).
    """
    return f"""       CASE WHEN x.proxy_rule IS NOT NULL THEN 'served' ELSE 'unknown' END AS {kind}_status,
       NULL::double precision AS {kind}_distance_ft,
       CASE WHEN x.proxy_rule IS NOT NULL THEN 'low' END AS {kind}_confidence,
       x.proxy_rule AS {kind}_source,
       CASE x.proxy_rule WHEN 'plat_first_home' THEN CAST(:yb_date AS date)
                         WHEN 'plat_age_fallback' THEN CAST(:owners_date AS date) END AS {kind}_source_date"""


def assign_parcels(engine: Engine, as_of: date, cfg: dict, schema: str = "public") -> dict:
    """cfg is the `utilities` section of config/ingest.yaml."""
    s = schema
    fb = cfg["plat_fallback"]
    with engine.begin() as conn:
        yb_date = conn.execute(text(f"SELECT max(snapshot_date) FROM {s}.nconemap_parcels_snapshots")).scalar_one()
        owners_date = conn.execute(text(f"SELECT max(valid_from) FROM {s}.parcel_owners")).scalar_one()
        sql = ASSIGN_SQL.format(s=s, status_cols=",\n".join(_status_cols(k) for k in ("sewer", "water")))
        params = {
            "after": cfg["plat_after_year"], "within": cfg["planned_project_within_ft"],
            "book_min": fb["book_min"], "book_max": fb["book_max"], "max_lot": fb["max_median_lot_acres"],
            "outside": list(fb["outside_municipality"]), "yb_date": yb_date, "owners_date": owners_date,
        }
        create_staging_from_query(conn, f"{s}.parcel_utilities_staging", sql, params)
        return apply_versioned(conn, f"{s}.parcel_utilities", ["pin"], as_of)


def quality_report(engine: Engine, schema: str = "public") -> dict:
    s = schema
    with engine.connect() as conn:
        rows = lambda sql: [tuple(r) for r in conn.execute(text(sql)).all()]
        return {
            "sewer_by_status_confidence": rows(
                f"SELECT sewer_status, coalesce(sewer_confidence, '-'), count(*) FROM {s}.parcel_utilities "
                "GROUP BY 1, 2 ORDER BY 3 DESC"),
            "water_by_status_confidence": rows(
                f"SELECT water_status, coalesce(water_confidence, '-'), count(*) FROM {s}.parcel_utilities "
                "GROUP BY 1, 2 ORDER BY 3 DESC"),
            "parcels_with_plat_ref": conn.execute(text(
                f"SELECT count(*) FILTER (WHERE plat_ref IS NOT NULL) AS with_ref, "
                f"count(*) FILTER (WHERE plat_year IS NOT NULL) AS with_year "
                f"FROM {s}.parcel_utilities")).one()._asdict(),
            "planned_projects": rows(
                f"SELECT project_type, planned, count(*) FROM {s}.cip_projects GROUP BY 1, 2 ORDER BY 1, 2"),
            "by_source": rows(
                f"SELECT coalesce(sewer_source, '-'), count(*) FROM {s}.parcel_utilities GROUP BY 1 ORDER BY 2 DESC"),
            "parcels_near_planned_project": rows(
                f"SELECT planned_project_type, count(*) FROM {s}.parcel_utilities WHERE planned_project_nearby "
                "GROUP BY 1 ORDER BY 1"),
        }


def format_report(r: dict, changes: dict) -> str:
    lines = ["Utilities quality report"]
    for kind in ("sewer", "water"):
        lines.append(f"  {kind} status / confidence:")
        lines += [f"    {st:<8} {conf:<5} {n:>9,}" for st, conf, n in r[f"{kind}_by_status_confidence"]]
    lines.append("  sewer source:")
    lines += [f"    {src:<18} {n:>9,}" for src, n in r["by_source"]]
    p = r["parcels_with_plat_ref"]
    lines.append(f"  parcels with a plat reference   {p['with_ref']:>9,}  ({p['with_year']:,} with a plat year estimate)")
    lines.append("  capital projects (type, planned, count):")
    lines += [f"    {t:<6} {'planned' if pl else 'other':<8} {n:>5,}" for t, pl, n in r["planned_projects"]]
    lines.append("  parcels near a planned project:")
    lines += [f"    {t:<6} {n:>9,}" for t, n in r["parcels_near_planned_project"]]
    lines.append("  changes since last load:")
    for table, c in changes.items():
        lines.append(f"    {table:<17} +{c['added']:,} new, {c['changed']:,} changed, -{c['removed']:,} removed"
                     f"  -> {c['current']:,} current")
    return "\n".join(lines)


def main() -> None:
    cfg = yaml.safe_load((ROOT / "config" / "ingest.yaml").read_text())["utilities"]
    check_restricted(ROOT / cfg["restricted_dir"])
    today = date.today()
    path = download_cip(cfg["cip_url"], ROOT / cfg["cip_download_dir"], today)
    print(f"As-of date {today}; capital projects saved to {path.relative_to(ROOT).as_posix()}")

    engine = get_engine()
    cip = read_cip(path, cfg["cip_types"], cfg["cip_planned_phases"])
    changes = {"cip_projects": load_cip(cip, engine, today)}
    changes["parcel_utilities"] = assign_parcels(engine, today, cfg)
    report = quality_report(engine)
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO ingest_runs (source, snapshot_date, source_file, snapshot_written, report) "
                 "VALUES ('utilities', :d, :f, :w, CAST(:r AS jsonb))"),
            {"d": today, "f": json.dumps({"cip": path.relative_to(ROOT).as_posix()}),
             "w": any(c["added"] or c["changed"] or c["removed"] for c in changes.values()),
             "r": json.dumps({**report, "changes": changes}, default=str)},
        )
    print()
    print(format_report(report, changes))


if __name__ == "__main__":
    main()
