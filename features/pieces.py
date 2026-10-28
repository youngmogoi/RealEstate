"""Buildable pieces: where creeks and other unbuildable areas cut a parcel's land apart.

Run with `make pieces` (after `make constraints` and `make streets`). Settings: `pieces` in
config/features.yaml; the layers that cut are the vector `unbuildable` layers of
config/constraints.yaml (stream and lake buffers, floodway, encroachment area).

parcel_pieces (keyed by pin, piece_no; history in parcel_pieces_history), only for parcels an
unbuildable layer touches:
- the parcel's land outside the unbuildable layers, split into separate pieces (smaller than
  min_piece_sf: dropped as slivers), numbered largest first;
- slope per piece from the slope band raster, so net_sf = area_sf - steep (unbuildable band) sf
  is right for each piece;
- street_contact_ft: the piece's lot line inside any usable street's frontage reach (the
  streets/frontage settings of parcel features, no parallel test); owner_contact_ft: its lot
  line touching another parcel with the same owner name. touches_street / touches_same_owner
  when these reach min_contact_ft;
- creek: a mapped stream runs through the parcel's unbuildable area (else the cut is another
  constraint, e.g. a lake buffer).

Which pieces count is decided in scoring/yield.py, which knows each district's minimum lot.
"""

import json
from datetime import date
from pathlib import Path

import geopandas as gpd
import shapely
import yaml
from sqlalchemy import Engine, text

from ingest.constraints import slope_acres
from ingest.db import copy_dataframe, get_engine
from ingest.versioning import apply_versioned, create_staging_from_query

ROOT = Path(__file__).resolve().parent.parent
CRS_EPSG = 2264
SQFT_PER_ACRE = 43_560
TEMP = ["piece_unb_src", "piece_unb", "piece_geoms", "piece_edges", "piece_street_buffers", "piece_slope"]

# 1. per parcel: the union of unbuildable shapes inside it, and whether a stream runs through it
# 2. the land outside it, one row per piece (snapped to 0.01 ft so reruns match exactly)
PIECES_SQL = """
CREATE UNLOGGED TABLE {s}.piece_unb_src AS
SELECT ST_Subdivide(geom, 256) AS g FROM {s}.constraint_features
WHERE class = 'unbuildable' AND GeometryType(geom) LIKE '%POLYGON';
CREATE INDEX ON {s}.piece_unb_src USING gist (g);
ANALYZE {s}.piece_unb_src;
CREATE UNLOGGED TABLE {s}.piece_unb AS
WITH hits AS (
    SELECT p.pin, CASE WHEN ST_CoveredBy(p.geom, u.g) THEN p.geom
                       ELSE ST_CollectionExtract(ST_Intersection(p.geom, u.g), 3) END AS g
    FROM {s}.parcels p JOIN {s}.piece_unb_src u ON ST_Intersects(p.geom, u.g)
)
SELECT pin, ST_UnaryUnion(ST_Collect(g ORDER BY ST_XMin(g), ST_YMin(g), ST_Area(g)), 0.01) AS g
FROM hits WHERE NOT ST_IsEmpty(g) GROUP BY pin;
CREATE INDEX ON {s}.piece_unb (pin);
CREATE UNLOGGED TABLE {s}.piece_geoms AS
WITH dumped AS (
    SELECT p.pin, EXISTS (SELECT 1 FROM {s}.constraint_features f
                          WHERE f.layer = 'streams' AND ST_Intersects(f.geom, u.g)) AS creek,
           (ST_Dump(ST_CollectionExtract(ST_Difference(p.geom, u.g, 0.01), 3))).geom AS g
    FROM {s}.parcels p JOIN {s}.piece_unb u USING (pin)
)
SELECT pin, row_number() OVER (PARTITION BY pin ORDER BY ST_Area(g) DESC, ST_XMin(g), ST_YMin(g)) AS piece_no,
       creek, g
FROM dumped WHERE ST_Area(g) >= :min_piece;
CREATE INDEX ON {s}.piece_geoms (pin);
CREATE INDEX ON {s}.piece_geoms USING gist (g);
ANALYZE {s}.piece_geoms;
CREATE UNLOGGED TABLE {s}.piece_edges AS
SELECT pg.pin, pg.piece_no,
       ST_CollectionExtract(ST_Intersection(ST_Boundary(pg.g), ST_Buffer(ST_Boundary(p.geom), 0.5)), 2) AS e
FROM {s}.piece_geoms pg JOIN {s}.parcels p USING (pin);
CREATE INDEX ON {s}.piece_edges USING gist (e);
ANALYZE {s}.piece_edges;
CREATE UNLOGGED TABLE {s}.piece_street_buffers AS
WITH lines AS (
    SELECT CASE WHEN coalesce(thoroughfare, '') = ANY(:major) THEN :buf_major ELSE :buf END AS reach,
           ST_Subdivide(geom, 64) AS g
    FROM {s}.streets
    WHERE NOT (coalesce(lifecycle, '') = ANY(:ex_life)) AND NOT (coalesce(thoroughfare, '') = ANY(:ex_thru))
)
SELECT ST_Subdivide(ST_Buffer(g, reach), 256) AS g FROM lines;
CREATE INDEX ON {s}.piece_street_buffers USING gist (g);
ANALYZE {s}.piece_street_buffers
"""

STAGE_SQL = """
WITH street AS (
    SELECT e.pin, e.piece_no,
           ST_Length(ST_UnaryUnion(ST_Collect(ST_Intersection(e.e, b.g) ORDER BY ST_XMin(b.g), ST_YMin(b.g)), 0.01)) AS ft
    FROM {s}.piece_edges e JOIN {s}.piece_street_buffers b ON ST_Intersects(e.e, b.g)
    GROUP BY e.pin, e.piece_no
),
owner AS (
    SELECT e.pin, e.piece_no,
           ST_Length(ST_UnaryUnion(ST_Collect(ST_Intersection(e.e, ST_Buffer(n.geom, 1)) ORDER BY n.pin), 0.01)) AS ft
    FROM {s}.piece_edges e
    JOIN {s}.parcels p ON p.pin = e.pin AND coalesce(p.owner_name, '') <> ''
    JOIN {s}.parcels n ON n.pin <> p.pin AND n.owner_name = p.owner_name AND ST_DWithin(e.e, n.geom, 1)
    GROUP BY e.pin, e.piece_no
)
SELECT pg.pin, pg.piece_no,
       round(ST_Area(pg.g)::numeric, 1)::float8 AS area_sf,
       round((coalesce(sl.slope_15_25_acres, 0) * {acre})::numeric, 1)::float8 AS slope_15_25_sf,
       round((coalesce(sl.unbuildable_slope_acres, 0) * {acre})::numeric, 1)::float8 AS slope_unbuildable_sf,
       round(greatest(ST_Area(pg.g) - coalesce(sl.unbuildable_slope_acres, 0) * {acre}, 0)::numeric, 1)::float8 AS net_sf,
       round(coalesce(st.ft, 0)::numeric, 1)::float8 AS street_contact_ft,
       round(coalesce(o.ft, 0)::numeric, 1)::float8 AS owner_contact_ft,
       coalesce(st.ft, 0) >= :min_contact AS touches_street,
       coalesce(o.ft, 0) >= :min_contact AS touches_same_owner,
       pg.creek,
       ST_Multi(pg.g) AS geom
FROM {s}.piece_geoms pg
LEFT JOIN {s}.piece_slope sl USING (pin, piece_no)
LEFT JOIN street st USING (pin, piece_no)
LEFT JOIN owner o USING (pin, piece_no)
"""


def _pieces_in(engine: Engine, s: str):
    def pieces_in(b):
        with engine.connect() as conn:
            rows = conn.execute(text(
                f"SELECT pin, piece_no, ST_AsBinary(g) FROM {s}.piece_geoms WHERE g && ST_MakeEnvelope(:a, :b, :c, :d, 2264)"
            ), dict(zip("abcd", b))).all()
        return gpd.GeoDataFrame({"pin": [r[0] for r in rows], "piece_no": [r[1] for r in rows]},
                                geometry=shapely.from_wkb([bytes(r[2]) for r in rows]), crs=CRS_EPSG)
    return pieces_in


def _no_vectors(b):
    # pieces already lie outside the vector unbuildable layers
    return gpd.GeoDataFrame(geometry=[], crs=CRS_EPSG)


def build(engine: Engine, cfg: dict, ccfg: dict, raster: Path, as_of: date, schema: str = "public", log=print) -> dict:
    """cfg is config/features.yaml, ccfg config/constraints.yaml; raster the slope band raster."""
    s = schema
    st, fr, pc = cfg["streets"], cfg["frontage"], cfg["pieces"]
    bands = ccfg["slope"]["bands"]
    unb_bands = [n for n, b in bands.items() if b["class"] == "unbuildable"]
    params = {"min_piece": pc["min_piece_sf"], "buf": fr["buffer_ft"], "buf_major": fr["buffer_major_ft"],
              "major": fr["major_thoroughfare"], "ex_life": st["exclude_lifecycle"], "ex_thru": st["exclude_thoroughfare"]}
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS " + ", ".join(f"{s}.{t}" for t in TEMP)))
        for stmt in PIECES_SQL.format(s=s).split(";"):
            if stmt.strip():
                conn.execute(text(stmt), {k: v for k, v in params.items() if f":{k}" in stmt})
    log("  pieces cut; measuring slope per piece ...")
    slope = slope_acres(raster, _pieces_in(engine, s), _no_vectors, bands, log=lambda *a: None, keys=("pin", "piece_no"))
    slope["unbuildable_slope_acres"] = slope[[f"{n}_acres" for n in unb_bands]].sum(axis=1) if unb_bands else 0.0
    if "slope_15_25_acres" not in slope:
        slope["slope_15_25_acres"] = 0.0
    with engine.begin() as conn:
        copy_dataframe(conn, slope[["pin", "piece_no", "slope_15_25_acres", "unbuildable_slope_acres"]], f"{s}.piece_slope")
        conn.execute(text(f"CREATE INDEX ON {s}.piece_slope (pin, piece_no)"))
        create_staging_from_query(conn, f"{s}.parcel_pieces_staging", STAGE_SQL.format(s=s, acre=SQFT_PER_ACRE),
                                  {"min_contact": pc["min_contact_ft"]})
        conn.execute(text("DROP TABLE " + ", ".join(f"{s}.{t}" for t in TEMP)))
        changes = apply_versioned(conn, f"{s}.parcel_pieces", ["pin", "piece_no"], as_of)
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS parcel_pieces_pin_idx ON {s}.parcel_pieces (pin)"))
    return changes


def quality_report(engine: Engine, schema: str = "public") -> dict:
    with engine.connect() as conn:
        r = conn.execute(text(f"""
            WITH per AS (
                SELECT pin, count(*) AS n, bool_or(touches_street) AS street,
                       bool_or(NOT touches_street AND NOT touches_same_owner) AS unreached, bool_or(creek) AS creek
                FROM {schema}.parcel_pieces GROUP BY pin)
            SELECT count(*) AS parcels, sum(n) AS pieces, count(*) FILTER (WHERE n >= 2) AS multi_piece,
                   count(*) FILTER (WHERE unreached) AS with_unreached_piece,
                   count(*) FILTER (WHERE unreached AND creek) AS unreached_creek,
                   count(*) FILTER (WHERE NOT street) AS no_piece_on_street
            FROM per""")).one()._asdict()
    return {k: int(v or 0) for k, v in r.items()}


def main() -> None:
    cfg = yaml.safe_load((ROOT / "config" / "features.yaml").read_text())
    ccfg = yaml.safe_load((ROOT / "config" / "constraints.yaml").read_text())
    raster = ROOT / ccfg["derived_dir"] / f"slope_bands_{int(ccfg['slope']['resolution_ft'])}ft.tif"
    if not raster.exists():
        raise SystemExit(f"{raster} is missing: run `make constraints` first")
    engine = get_engine()
    with engine.connect() as conn:
        as_of = conn.execute(text("SELECT max(valid_from) FROM parcel_constraints")).scalar_one()
    changes = build(engine, cfg, ccfg, raster, as_of)
    report = quality_report(engine)
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO ingest_runs (source, snapshot_date, source_file, snapshot_written, report) "
                 "VALUES ('parcel_pieces', :d, :f, :w, CAST(:r AS jsonb))"),
            {"d": as_of, "f": f"parcels, constraint_features, streets, {raster.relative_to(ROOT).as_posix()}",
             "w": bool(changes["added"] or changes["changed"] or changes["removed"]),
             "r": json.dumps({**report, "changes": changes}, default=str)})
    print("Parcel pieces: " + ", ".join(f"{k} {v:,}" for k, v in report.items()))
    print(f"  parcel_pieces: {changes}")


if __name__ == "__main__":
    main()
