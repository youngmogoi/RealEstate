"""Build `parcel_features`: one row per parcel PIN, rebuilt from the source tables.

Run with `make features` (after the ingests). Settings live in config/features.yaml.
Kept with history (parcel_features_history; see ingest/versioning.py), dated by the
latest county parcel snapshot, so rebuilding from unchanged sources records no changes.

Where each column comes from:
- parcels (county Tax Parcel Boundaries + Ownership Data): pin, site_address, municipality,
  land_use, gis_acres, land/building/total value, last_sale_date/price, mail_state,
  owner_name, account_count; mail_address1 for the absentee check.
- parcel_constraints: gross_acres (the parcel shape's area), unbuildable_acres,
  net_buildable_acres, floodplain_flag_acres, slope_15_25_acres, slope_over_25_acres,
  possible_unmapped_wetland_acres.
- parcel_zoning: zone_code, zoning_jurisdiction, split_zoning, zone_share; etj_jurisdiction
  is the zoning jurisdiction of a parcel whose tax municipality is unincorporated (no ETJ
  layer is published; this is how it is inferred).
- parcel_utilities: sewer_status, sewer_confidence, sewer_source, sewer_distance_ft,
  water_status, water_distance_ft, planned_project_nearby.
- streets (city CountyData/Streets): frontage_ft, private_frontage_ft, proposed_frontage_ft,
  frontage_proposed_only, access_via_common_area, street_frontages,
  corner_lot, through_lot, flag_lot, landlocked. Frontage counts only lot-line segments
  roughly parallel to a usable street within buffer_ft of its centerline.
- parcels geometry: lot_width_ft, lot_depth_ft (sides of the minimum rotated rectangle).
- nconemap_parcels_snapshots (2026-01-07): year_built, the newest building on the parcel.
  The county files have no year built; heated square feet has no source yet.
- parcel_owners: owner_other_parcels (other PINs under the same owner name).
"""

import json
import re
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import yaml
from sqlalchemy import Engine, text

from ingest.db import get_engine
from ingest.versioning import apply_versioned, create_staging_from_query

ROOT = Path(__file__).resolve().parent.parent


def words_regex(words: list[str]) -> str:
    """PostgreSQL regex matching any of the words/phrases as whole words."""
    return r"\m(" + "|".join(re.escape(w).replace(r"\ ", r"\s+") for w in words) + r")\M"


def owner_type_sql(col: str, col2: str, rules: list[dict]) -> str:
    """CASE expression for the owner type of `col` (rules with also_owner2 also test `col2`)."""
    sql_re = lambda words: words_regex(words).replace("'", "''")
    whens = []
    for r in rules:
        names = [col, f"coalesce({col2}, '')"] if r.get("also_owner2") else [col]
        conds = []
        for n in names:
            c = f"upper({n}) ~ '{sql_re(r['words'])}'"
            if r.get("exclude"):
                c += f" AND NOT upper({n}) ~ '{sql_re(r['exclude'])}'"
            conds.append(f"({c})")
        whens.append(f"WHEN {' OR '.join(conds)} THEN '{r['type']}'")
    return f"CASE WHEN {col} IS NULL THEN NULL {' '.join(whens)} ELSE 'individual' END"


def owner_type(name: str | None, rules: list[dict], owner2: str | None = None) -> str | None:
    """Same rule as owner_type_sql, in Python (for tests and spot checks)."""
    if name is None:
        return None
    py = lambda words: re.compile(words_regex(words).replace(r"\m", r"\b").replace(r"\M", r"\b"))
    for r in rules:
        for n in [name, owner2 or ""] if r.get("also_owner2") else [name]:
            if py(r["words"]).search(n.upper()) and not (r.get("exclude") and py(r["exclude"]).search(n.upper())):
                return r["type"]
    return "individual"


# Frontage, in three steps:
# 1. usable street lines (config filters), subdivided; and their buffers
# 2. lot-line segments inside a street's buffer
# 3. keep only segments roughly parallel to that street: both ends about the same distance
#    from it (|d_start - d_end| <= parallel_ratio * length). Side lot lines run away from the
#    street and drop out, so they don't inflate frontage or fake a corner.
FRONTAGE_SQL = """
CREATE UNLOGGED TABLE {s}.feature_street_lines AS
SELECT street_code, street_class = ANY(:private) AS private, coalesce(lifecycle, '') = ANY(:proposed) AS proposed,
       CASE WHEN coalesce(thoroughfare, '') = ANY(:major) THEN :buf_major ELSE :buf END AS reach,
       ST_Subdivide(geom, 64) AS g
FROM {s}.streets
WHERE NOT (coalesce(lifecycle, '') = ANY(:ex_life)) AND NOT (coalesce(thoroughfare, '') = ANY(:ex_thru));
CREATE INDEX ON {s}.feature_street_lines USING gist (g);
CREATE UNLOGGED TABLE {s}.feature_street_buffers AS
SELECT street_code, private, proposed, ST_Subdivide(ST_Buffer(g, reach), 256) AS g FROM {s}.feature_street_lines;
CREATE INDEX ON {s}.feature_street_buffers USING gist (g);
ANALYZE {s}.feature_street_lines; ANALYZE {s}.feature_street_buffers;
"""

FRONTAGE_HITS_SQL = """
CREATE UNLOGGED TABLE {s}.feature_frontage_hits AS
WITH segs AS (
    SELECT p.pin, b.street_code, b.private, b.proposed, (ST_DumpSegments(
               ST_CollectionExtract(ST_Intersection(ST_Boundary(p.geom), b.g), 2))).geom AS seg
    FROM {s}.parcels p JOIN {s}.feature_street_buffers b ON ST_Intersects(p.geom, b.g)
),
dist AS (
    SELECT sg.*, ST_Length(sg.seg) AS len,
           (SELECT min(ST_Distance(ST_StartPoint(sg.seg), l.g)) FROM {s}.feature_street_lines l
            WHERE l.street_code IS NOT DISTINCT FROM sg.street_code AND ST_DWithin(l.g, sg.seg, :reach_max + 1)) AS d0,
           (SELECT min(ST_Distance(ST_EndPoint(sg.seg), l.g)) FROM {s}.feature_street_lines l
            WHERE l.street_code IS NOT DISTINCT FROM sg.street_code AND ST_DWithin(l.g, sg.seg, :reach_max + 1)) AS d1
    FROM segs sg
)
SELECT pin, street_code, private, proposed, seg AS l FROM dist
WHERE len > 0 AND abs(d0 - d1) <= :parallel * len
"""

FRONTAGE_SUMMARY_SQL = """
CREATE UNLOGGED TABLE {s}.feature_frontage AS
WITH frontage AS (
    SELECT pin,
           ST_Length(ST_UnaryUnion(ST_Collect(l ORDER BY ST_XMin(l), ST_YMin(l), ST_Length(l)), 0.01)) AS frontage_ft,
           coalesce(ST_Length(ST_UnaryUnion(ST_Collect(l ORDER BY ST_XMin(l), ST_YMin(l), ST_Length(l))
               FILTER (WHERE private), 0.01)), 0) AS private_frontage_ft,
           coalesce(ST_Length(ST_UnaryUnion(ST_Collect(l ORDER BY ST_XMin(l), ST_YMin(l), ST_Length(l))
               FILTER (WHERE proposed), 0.01)), 0) AS proposed_frontage_ft,
           ST_Length(ST_UnaryUnion(ST_Collect(l ORDER BY ST_XMin(l), ST_YMin(l), ST_Length(l))
               FILTER (WHERE NOT proposed), 0.01)) AS built_frontage_ft
    FROM {s}.feature_frontage_hits GROUP BY pin
),
per_street AS (
    SELECT pin, street_code, ST_UnaryUnion(ST_Collect(l ORDER BY ST_XMin(l), ST_YMin(l), ST_Length(l)), 0.01) AS g
    FROM {s}.feature_frontage_hits GROUP BY pin, street_code
),
real_street AS (SELECT * FROM per_street WHERE ST_Length(g) >= :min_street),
streets_n AS (
    SELECT pin, count(*) AS street_frontages FROM real_street GROUP BY pin
),
corners AS (
    -- two streets' frontages meet (within corner_max_gap_ft): a corner, not a through lot
    SELECT a.pin, bool_or(ST_DWithin(a.g, b.g, :corner_gap)) AS corner
    FROM real_street a JOIN real_street b ON a.pin = b.pin AND a.street_code < b.street_code GROUP BY a.pin
)
SELECT f.pin, f.frontage_ft, f.private_frontage_ft, f.proposed_frontage_ft,
       coalesce(f.built_frontage_ft, 0) = 0 AS proposed_only, coalesce(n.street_frontages, 0) AS street_frontages,
       coalesce(c.corner, false) AS corner
FROM frontage f LEFT JOIN streets_n n USING (pin) LEFT JOIN corners c USING (pin)
"""

FEATURES_SQL = """
WITH frontage AS (SELECT * FROM {s}.feature_frontage),
rect AS (
    SELECT pin, ST_ExteriorRing(ST_OrientedEnvelope(geom)) AS r FROM {s}.parcels
),
dims AS (
    SELECT pin, ST_Distance(ST_PointN(r, 1), ST_PointN(r, 2)) AS a, ST_Distance(ST_PointN(r, 2), ST_PointN(r, 3)) AS b
    FROM rect
),
built AS (
    SELECT pin, max(year_built) AS year_built FROM {s}.nconemap_parcels_snapshots WHERE year_built > 1800 GROUP BY pin
),
owner_counts AS (
    SELECT owner_name, count(DISTINCT pin) AS parcels FROM {s}.parcel_owners WHERE owner_name IS NOT NULL GROUP BY owner_name
),
base AS (
    SELECT p.pin, p.site_address,
           p.municipality,
           p.municipality IS NOT NULL AND NOT (p.municipality = ANY(:outside)) AS in_municipal_limit,
           CASE WHEN p.municipality = ANY(:outside) THEN z.jurisdiction END AS etj_jurisdiction,
           p.parcel_use AS land_use, c.gross_acres, p.gis_acres,
           z.zone_code, z.jurisdiction AS zoning_jurisdiction, z.split AS split_zoning, z.zone_share,
           u.sewer_status, u.sewer_confidence, u.sewer_source, u.sewer_distance_ft,
           u.water_status, u.water_distance_ft, u.planned_project_nearby,
           c.unbuildable_acres, c.net_buildable_acres, c.floodplain_flag_acres,
           c.slope_15_25_acres, c.slope_over_25_acres, c.possible_unmapped_wetland_acres,
           coalesce(f.frontage_ft, 0) AS frontage_ft, coalesce(f.private_frontage_ft, 0) AS private_frontage_ft,
           coalesce(f.proposed_frontage_ft, 0) AS proposed_frontage_ft, coalesce(f.proposed_only, false) AS proposed_only,
           coalesce(upper(p.parcel_use) ~ :common_uses, false) AS common_use,
           coalesce(f.street_frontages, 0) AS street_frontages, coalesce(f.corner, false) AS corner,
           least(d.a, d.b) AS lot_width_ft, greatest(d.a, d.b) AS lot_depth_ft,
           p.land_value, p.building_value, p.total_value, b.year_built,
           p.last_sale_date, p.last_sale_price, p.mail_state, p.owner_name, p.owner2_name, p.mail_address1,
           p.mail_address2, p.account_count,
           oc.parcels AS owner_parcels
    FROM {s}.parcels p
    LEFT JOIN {s}.parcel_constraints c USING (pin)
    LEFT JOIN {s}.parcel_zoning z USING (pin)
    LEFT JOIN {s}.parcel_utilities u USING (pin)
    LEFT JOIN frontage f USING (pin)
    LEFT JOIN dims d USING (pin)
    LEFT JOIN built b USING (pin)
    LEFT JOIN owner_counts oc ON oc.owner_name = p.owner_name
)
SELECT pin, site_address, municipality, in_municipal_limit, etj_jurisdiction, land_use, gross_acres, gis_acres,
       zone_code, zoning_jurisdiction, split_zoning, zone_share,
       sewer_status, sewer_confidence, sewer_source, sewer_distance_ft, water_status, water_distance_ft,
       planned_project_nearby,
       unbuildable_acres, net_buildable_acres, floodplain_flag_acres, slope_15_25_acres, slope_over_25_acres,
       possible_unmapped_wetland_acres,
       frontage_ft, private_frontage_ft, proposed_frontage_ft, street_frontages,
       frontage_ft > 0 AND proposed_only AS frontage_proposed_only,
       corner AS corner_lot,
       street_frontages >= 2 AND NOT corner AS through_lot,
       frontage_ft > 0 AND frontage_ft < :flag_max AND frontage_ft < :flag_ratio * lot_width_ft AS flag_lot,
       frontage_ft = 0 AND common_use AS access_via_common_area,
       frontage_ft = 0 AND NOT common_use AS landlocked,
       lot_width_ft, lot_depth_ft,
       land_value, building_value, total_value, year_built,
       CASE WHEN total_value > 0 THEN building_value / total_value END AS building_value_ratio,
       coalesce(building_value > 0 AND total_value > 0 AND building_value / total_value < :td_ratio
                AND year_built < :td_year, false) AS teardown_candidate,
       coalesce(building_value, 0) = 0 AS vacant,
       last_sale_price,
       round(((CAST(:as_of AS date) - last_sale_date) / 365.25)::numeric, 1)::float8 AS years_since_last_sale,
       mail_state,
       CASE WHEN mail_state IS NOT NULL THEN mail_state <> 'NC' END AS out_of_state,
       -- absentee: house number and first word of the street differ between mailing and site address
       CASE WHEN substring(mail_address1 from '^\\d+\\s+\\S+') IS NOT NULL
                 AND substring(site_address from '^\\d+\\s+\\S+') IS NOT NULL
            THEN substring(mail_address1 from '^\\d+\\s+\\S+') <> substring(site_address from '^\\d+\\s+\\S+') END
           AS absentee,
       {owner_type} AS owner_type,
       -- in care of: C/O or % in the owner names or mailing address (an estate, attorney, or manager)
       CASE WHEN owner_name IS NOT NULL THEN concat_ws(' ', owner_name, owner2_name, mail_address1, mail_address2)
            ~ '(C/O|%)' END AS in_care_of,
       CASE WHEN owner_parcels IS NOT NULL THEN owner_parcels - 1 END AS owner_other_parcels,
       account_count
FROM base
"""


def as_of_date(engine: Engine, schema: str = "public") -> date:
    """Latest county parcel snapshot date: features are dated by their source data."""
    with engine.connect() as conn:
        d = conn.execute(text(f"SELECT max(valid_from) FROM {schema}.parcels")).scalar_one()
    if d is None:
        raise SystemExit("parcels is empty: run `make ingest` first")
    return d


def build_frontage(conn, cfg: dict, s: str = "public") -> None:
    """Create {s}.feature_frontage: pin, frontage_ft, private/proposed frontage, street_frontages, corner."""
    st, fr = cfg["streets"], cfg["frontage"]
    temp = f"{s}.feature_street_lines, {s}.feature_street_buffers, {s}.feature_frontage_hits"
    conn.execute(text(f"DROP TABLE IF EXISTS {temp}, {s}.feature_frontage"))
    params = {"private": st["private_class"], "proposed": st["proposed_lifecycle"], "buf": fr["buffer_ft"],
              "buf_major": fr["buffer_major_ft"], "major": fr["major_thoroughfare"],
              "ex_life": st["exclude_lifecycle"], "ex_thru": st["exclude_thoroughfare"]}
    for stmt in FRONTAGE_SQL.format(s=s).split(";"):
        if stmt.strip():
            conn.execute(text(stmt), {k: v for k, v in params.items() if f":{k}" in stmt})
    conn.execute(text(FRONTAGE_HITS_SQL.format(s=s)),
                 {"reach_max": max(fr["buffer_ft"], fr["buffer_major_ft"]), "parallel": fr["parallel_ratio"]})
    conn.execute(text(f"CREATE INDEX ON {s}.feature_frontage_hits (pin)"))
    conn.execute(text(FRONTAGE_SUMMARY_SQL.format(s=s)),
                 {"min_street": fr["min_per_street_ft"], "corner_gap": fr["corner_max_gap_ft"]})
    conn.execute(text(f"CREATE INDEX ON {s}.feature_frontage (pin)"))
    conn.execute(text(f"DROP TABLE {temp}"))


def build(engine: Engine, cfg: dict, as_of: date, outside_municipality: list[str], schema: str = "public") -> dict:
    s = schema
    st, fr, td = cfg["streets"], cfg["frontage"], cfg["teardown"]
    with engine.begin() as conn:
        build_frontage(conn, cfg, s)
        sql = FEATURES_SQL.format(s=s, owner_type=owner_type_sql("owner_name", "owner2_name", cfg["owner_type_rules"]))
        create_staging_from_query(conn, f"{s}.parcel_features_staging", sql, {
            "flag_max": fr["flag_lot_max_frontage_ft"], "flag_ratio": fr["flag_lot_max_frontage_to_width"],
            "common_uses": "(" + "|".join(re.escape(u) for u in fr["common_access_land_uses"]) + ")",
            "td_ratio": td["max_building_value_ratio"], "td_year": td["built_before"], "as_of": as_of,
            "outside": outside_municipality})
        conn.execute(text(f"DROP TABLE {s}.feature_frontage"))
        return apply_versioned(conn, f"{s}.parcel_features", ["pin"], as_of)


def quality_report(engine: Engine, schema: str = "public") -> dict:
    s = schema
    with engine.connect() as conn:
        features, parcels = conn.execute(text(
            f"SELECT (SELECT count(*) FROM {s}.parcel_features), (SELECT count(*) FROM {s}.parcels)")).one()
        df_cols = [r[0] for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = :s AND table_name = 'parcel_features' "
            "AND column_name NOT IN ('row_hash', 'valid_from') ORDER BY ordinal_position"), {"s": s}).all()]
        nulls = conn.execute(text(
            "SELECT " + ", ".join(f"avg(({c} IS NULL)::int)" for c in df_cols) + f" FROM {s}.parcel_features")).one()
        flags = conn.execute(text(
            f"SELECT count(*) FILTER (WHERE corner_lot), count(*) FILTER (WHERE through_lot), count(*) FILTER (WHERE flag_lot), count(*) FILTER (WHERE access_via_common_area), count(*) FILTER (WHERE frontage_proposed_only), "
            f"count(*) FILTER (WHERE landlocked), "
            f"count(*) FILTER (WHERE teardown_candidate), count(*) FILTER (WHERE vacant), count(*) FILTER (WHERE absentee), "
            f"count(*) FILTER (WHERE out_of_state) FROM {s}.parcel_features")).one()
        owner_types = dict(conn.execute(text(
            f"SELECT coalesce(owner_type, '(multi-account)'), count(*) FROM {s}.parcel_features GROUP BY 1 ORDER BY 2 DESC")).all())
    return {
        "rows": features, "parcels": parcels,
        "null_rates": {c: round(float(v), 4) for c, v in zip(df_cols, nulls)},
        "flags": dict(zip(["corner_lot", "through_lot", "flag_lot", "access_via_common_area", "frontage_proposed_only", "landlocked", "teardown_candidate", "vacant", "absentee",
                           "out_of_state"], flags)),
        "owner_types": owner_types,
    }


def format_report(r: dict, changes: dict) -> str:
    lines = ["Parcel features quality report",
             f"  rows {r['rows']:,} vs parcels {r['parcels']:,}: {'OK' if r['rows'] == r['parcels'] else 'MISMATCH'}",
             "  flags: " + ", ".join(f"{k} {v:,}" for k, v in r["flags"].items()),
             "  owner types: " + ", ".join(f"{k} {v:,}" for k, v in r["owner_types"].items()),
             f"  changes: +{changes['added']:,} new, {changes['changed']:,} changed, -{changes['removed']:,} removed "
             f"-> {changes['current']:,}",
             "  null rate per column:"]
    lines += [f"    {c:<32} {v:>7.1%}" for c, v in r["null_rates"].items()]
    return "\n".join(lines)


def main() -> None:
    cfg = yaml.safe_load((ROOT / "config" / "features.yaml").read_text())
    outside = yaml.safe_load((ROOT / "config" / "ingest.yaml").read_text())["utilities"]["plat_fallback"]["outside_municipality"]
    engine = get_engine()
    as_of = as_of_date(engine)
    changes = build(engine, cfg, as_of, outside)
    report = quality_report(engine)
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO ingest_runs (source, snapshot_date, source_file, snapshot_written, report) "
                 "VALUES ('parcel_features', :d, :f, :w, CAST(:r AS jsonb))"),
            {"d": as_of, "f": "parcels, parcel_constraints, parcel_zoning, parcel_utilities, streets, "
                              "nconemap_parcels_snapshots, parcel_owners",
             "w": bool(changes["added"] or changes["changed"] or changes["removed"]),
             "r": json.dumps({**report, "changes": changes}, default=str)})
    print(format_report(report, changes))
    if report["rows"] != report["parcels"]:
        sys.exit(f"parcel_features has {report['rows']:,} rows but parcels has {report['parcels']:,}")


if __name__ == "__main__":
    main()
