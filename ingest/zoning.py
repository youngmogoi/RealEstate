"""Load Charlotte and town zoning into `zoning_districts` and assign each parcel a district.

Run with `make zoning` (after `make ingest`, which loads parcels). Sources and the split
threshold live in config/ingest.yaml.

Tables (each also has a `_history` table; see ingest/versioning.py):
- zoning_districts: one row per published district shape, with `jurisdiction`.
  `geom` is the shape as published; `geom_effective` is what remains after overlaps are
  resolved (below), and is what parcels are measured against.
- parcel_zoning: one row per parcel: the district covering the largest share of it, the
  runner-up, and a `split` flag.

Overlaps (the layers are not perfectly clean; checked 2026-10-27):
- A town district beats a Charlotte district where they overlap.
- Within Charlotte, the most recent rezoning wins. On a tie (same or no rezone date), the
  smaller shape wins, since rezonings are usually small pieces cut from a large base
  district. Parcels lying mostly in such a tie area get `overlap_conflict`.

The same code can mean different things in different towns, so a district is always
identified by (jurisdiction, zone_code).
"""

import json
import os
from datetime import date, datetime
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyogrio
import shapely
import yaml
from shapely.strtree import STRtree
from sqlalchemy import Engine, text

from ingest.common import _polygonal, fix_geometries, strip_strings
from ingest.db import copy_dataframe, get_engine
from ingest.versioning import apply_versioned, create_staging_from_query, row_hash

ROOT = Path(__file__).resolve().parent.parent
CRS_EPSG = 2264
SQFT_PER_ACRE = 43_560

# The towns layer truncates names to 11 characters
MUNIC_NAMES = {"HUNTERSVILL": "HUNTERSVILLE"}

DISTRICT_COLS = [
    "district_id", "jurisdiction", "zone_code", "base_code", "conditional", "zone_class", "overlay",
    "spa", "petition", "rezone_date", "source_file", "source_row", "trimmed_acres", "overlap_conflict",
    "geom", "geom_effective", "conflict_geom",
]
GEOM_COLS = ["geom", "geom_effective", "conflict_geom"]


def _read(path: Path, layer: str | None = None) -> gpd.GeoDataFrame:
    gdf = pyogrio.read_dataframe(path, layer=layer)
    if gdf.crs is None:
        raise ValueError(f"{path} has no projection")
    if gdf.crs.to_epsg(min_confidence=20) == CRS_EPSG:
        gdf = gdf.set_crs(CRS_EPSG, allow_override=True)
    else:
        gdf = gdf.to_crs(CRS_EPSG)
    gdf["source_row"] = gdf.index.astype("int64")
    return gdf


def read_charlotte(path: Path) -> gpd.GeoDataFrame:
    gdf = strip_strings(_read(path))
    out = gpd.GeoDataFrame({
        "jurisdiction": "CHARLOTTE",
        "zone_code": gdf["ZoneDes"],
        "zone_class": gdf["ZoneClass"],
        "overlay": gdf["Overlay"].where(gdf["Overlay"].str.lower() != "none"),
        "spa": gdf["SPA"].str.lower().map({"spa": True, "yes": True, "no": False}).astype("boolean"),
        "petition": gdf["ZonePetiti"],
        "rezone_date": pd.to_datetime(gdf["RezoneDate"], utc=True).dt.tz_localize(None).dt.normalize(),
        "source_row": gdf["source_row"],
    }, geometry=gdf.geometry.values, crs=CRS_EPSG)
    out["source_file"] = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path)
    return out


def read_towns(path: Path, layer: str) -> gpd.GeoDataFrame:
    gdf = strip_strings(_read(path, layer))
    out = gpd.GeoDataFrame({
        "jurisdiction": gdf["munic"].replace(MUNIC_NAMES),
        "zone_code": gdf["zone_des"],
        "source_row": gdf["source_row"],
    }, geometry=gdf.geometry.values, crs=CRS_EPSG)
    for col in ("zone_class", "overlay", "petition"):
        out[col] = None
    out["spa"] = pd.array([pd.NA] * len(out), dtype="boolean")
    out["rezone_date"] = pd.NaT
    src = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path)
    out["source_file"] = f"{src}:{layer}"
    return out


def _multi(geom) -> shapely.MultiPolygon:
    return geom if geom.geom_type == "MultiPolygon" else _polygonal(geom)


def resolve_overlaps(districts: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Add geom_effective: each shape minus the parts claimed by higher-precedence shapes."""
    df = districts.copy()
    df["geometry"], _ = fix_geometries(df.geometry)
    df["geometry"] = [_multi(g) for g in df.geometry]

    df = df.reset_index(drop=True)
    is_town = df["jurisdiction"] != "CHARLOTTE"
    keys = pd.DataFrame({"charlotte": ~is_town, "rezone_date": df["rezone_date"], "area": df.geometry.area})
    order = keys.sort_values(
        ["charlotte", "rezone_date", "area"], ascending=[True, False, True], na_position="last"
    ).index
    rank = pd.Series(range(len(df)), index=order).reindex(df.index).to_numpy()  # 0 = highest precedence

    geoms = df.geometry.values
    tree = STRtree(geoms)
    dates = df["rezone_date"]
    effective, trimmed, conflict = [], [], []
    for i in range(len(df)):
        g = geoms[i]
        higher = [j for j in tree.query(g, predicate="intersects") if rank[j] < rank[i]]
        eff, lost, tie_area = g, 0.0, shapely.MultiPolygon()
        if higher:
            eff = _multi(g.difference(shapely.union_all([geoms[j] for j in higher])))
            lost = g.area - eff.area
            # Same-date overlaps within one jurisdiction: decided by the size tie-break
            ties = [
                geoms[j] for j in higher
                if is_town.iloc[j] == is_town.iloc[i]
                and (dates.iloc[j] == dates.iloc[i] or (pd.isna(dates.iloc[j]) and pd.isna(dates.iloc[i])))
            ]
            if ties and lost > 0:
                tie_area = _multi(g.intersection(shapely.union_all(ties)))
        effective.append(eff)
        trimmed.append(lost / SQFT_PER_ACRE)
        conflict.append(tie_area)

    df["geom_effective"] = gpd.GeoSeries(effective, index=df.index, crs=CRS_EPSG)
    df["trimmed_acres"] = trimmed
    df["conflict_geom"] = gpd.GeoSeries(conflict, index=df.index, crs=CRS_EPSG)
    df["overlap_conflict"] = ~df["conflict_geom"].is_empty & (df["conflict_geom"].area > 0)
    return df


def build_districts(charlotte: gpd.GeoDataFrame, towns: gpd.GeoDataFrame) -> pd.DataFrame:
    df = resolve_overlaps(pd.concat([towns, charlotte], ignore_index=True))
    df["conditional"] = df["zone_code"].str.endswith("(CD)")
    df["base_code"] = df["zone_code"].str.removesuffix("(CD)")
    df = df.rename_geometry("geom")
    ident = pd.DataFrame({
        "j": df["jurisdiction"], "c": df["zone_code"],
        "g": shapely.to_wkb(df["geom"].values, hex=True),
    })
    df["district_id"] = row_hash(ident)
    return df[DISTRICT_COLS]


def _wkb(geoms) -> list[str]:
    return shapely.to_wkb(shapely.set_srid(geoms, CRS_EPSG), hex=True, include_srid=True)


def _district_rows(chunk: pd.DataFrame) -> pd.DataFrame:
    rows = pd.DataFrame(chunk.drop(columns=GEOM_COLS)).assign(
        **{c: _wkb(chunk[c].values) for c in GEOM_COLS}
    )
    return rows.assign(row_hash=row_hash(rows))


def load_districts(districts: pd.DataFrame, engine: Engine, as_of: date, schema: str = "public") -> dict:
    table = f"{schema}.zoning_districts"
    geom_type = f"geometry(MultiPolygon, {CRS_EPSG})"
    with engine.begin() as conn:
        copy_dataframe(conn, districts, f"{table}_staging", dict.fromkeys(GEOM_COLS, geom_type), _district_rows)
        changes = apply_versioned(conn, table, ["district_id"], as_of)
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS zoning_districts_geom_idx ON {table} USING gist (geom_effective)"))
    return changes


# Step 1: every parcel/district-piece overlap, computed once.
HITS_SQL = """
CREATE UNLOGGED TABLE {s}.zoning_hits AS
SELECT p.pin, z.jurisdiction, z.zone_code, z.overlay, ST_Area(p.geom) AS pa,
       CASE WHEN ST_CoveredBy(p.geom, z.geom) THEN ST_Area(p.geom)
            ELSE ST_Area(ST_Intersection(p.geom, z.geom)) END AS a
FROM {s}.parcels p JOIN {s}.zoning_pieces z ON ST_Intersects(p.geom, z.geom)
"""

# Step 2: one row per parcel from those overlaps.
ASSIGN_SQL = """
WITH by_zone AS (
    SELECT pin, jurisdiction, zone_code, sum(a) / max(pa) AS share
    FROM {s}.zoning_hits GROUP BY 1, 2, 3 HAVING sum(a) > 0
),
ranked AS (
    SELECT *, row_number() OVER (PARTITION BY pin ORDER BY share DESC, jurisdiction, zone_code) AS n
    FROM by_zone
),
totals AS (
    SELECT pin, count(*) FILTER (WHERE share >= :m) AS n_sig, sum(share) AS covered
    FROM ranked GROUP BY pin
),
overlays AS (
    SELECT pin, string_agg(overlay, ',' ORDER BY overlay) AS overlays
    FROM (SELECT pin, overlay FROM {s}.zoning_hits WHERE overlay IS NOT NULL
          GROUP BY pin, overlay HAVING sum(a) / max(pa) >= :m) o
    GROUP BY pin
),
ties AS (
    SELECT p.pin, least(1, sum(ST_Area(ST_Intersection(p.geom, c.geom))) / max(ST_Area(p.geom))) AS share
    FROM {s}.parcels p JOIN {s}.zoning_conflict_pieces c ON ST_Intersects(p.geom, c.geom) GROUP BY p.pin
)
SELECT p.pin,
       r1.jurisdiction, r1.zone_code,
       regexp_replace(r1.zone_code, '\\(CD\\)$', '') AS base_code,
       r1.zone_code LIKE '%(CD)' AS conditional,
       r1.share AS zone_share,
       r2.jurisdiction AS second_jurisdiction, r2.zone_code AS second_zone_code, r2.share AS second_share,
       coalesce(t.n_sig, 0) AS district_count,
       coalesce(t.n_sig, 0) >= 2 AS split,
       coalesce(r1.jurisdiction <> r2.jurisdiction AND r2.share >= :m, false) AS crosses_jurisdiction,
       greatest(0, 1 - coalesce(t.covered, 0)) AS unzoned_share,
       coalesce(ti.share, 0) AS tie_share,
       coalesce(ti.share, 0) >= :m AS overlap_conflict,
       o.overlays
FROM {s}.parcels p
LEFT JOIN ranked r1 ON r1.pin = p.pin AND r1.n = 1
LEFT JOIN ranked r2 ON r2.pin = p.pin AND r2.n = 2
LEFT JOIN totals t ON t.pin = p.pin
LEFT JOIN overlays o ON o.pin = p.pin
LEFT JOIN ties ti ON ti.pin = p.pin
"""


def assign_parcels(engine: Engine, as_of: date, split_min_share: float, schema: str = "public") -> dict:
    """Measure every current parcel against current districts. Returns change counts.

    A parcel gets overlap_conflict when at least split_min_share of it lies where two same-date
    districts overlapped and the size tie-break decided.
    """
    s = schema
    with engine.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS {s}.zoning_pieces"))
        conn.execute(text(
            f"CREATE UNLOGGED TABLE {s}.zoning_pieces AS SELECT jurisdiction, zone_code, overlay, "
            f"ST_Subdivide(geom_effective, 256) AS geom FROM {s}.zoning_districts WHERE NOT ST_IsEmpty(geom_effective)"
        ))
        conn.execute(text(f"CREATE INDEX ON {s}.zoning_pieces USING gist (geom)"))
        conn.execute(text(f"ANALYZE {s}.zoning_pieces"))
        conn.execute(text(f"DROP TABLE IF EXISTS {s}.zoning_conflict_pieces"))
        conn.execute(text(
            f"CREATE UNLOGGED TABLE {s}.zoning_conflict_pieces AS SELECT ST_Subdivide(conflict_geom, 256) AS geom "
            f"FROM {s}.zoning_districts WHERE NOT ST_IsEmpty(conflict_geom)"
        ))
        conn.execute(text(f"CREATE INDEX ON {s}.zoning_conflict_pieces USING gist (geom)"))
        conn.execute(text(f"DROP TABLE IF EXISTS {s}.zoning_hits"))
        conn.execute(text(HITS_SQL.format(s=s)))
        create_staging_from_query(conn, f"{s}.parcel_zoning_staging", ASSIGN_SQL.format(s=s), {"m": split_min_share})
        conn.execute(text(f"DROP TABLE {s}.zoning_hits"))
        changes = apply_versioned(conn, f"{s}.parcel_zoning", ["pin"], as_of)
        conn.execute(text(f"DROP TABLE {s}.zoning_pieces"))
        conn.execute(text(f"DROP TABLE {s}.zoning_conflict_pieces"))
    return changes


def district_counts(engine: Engine, schema: str = "public") -> pd.DataFrame:
    """Every (jurisdiction, zone_code) in the districts, with how many parcels it is the main district for."""
    sql = f"""
        SELECT d.jurisdiction, d.zone_code, d.zone_class, coalesce(p.parcels, 0) AS parcels,
               coalesce(p.split_parcels, 0) AS split_parcels
        FROM (SELECT jurisdiction, zone_code, max(zone_class) AS zone_class
              FROM {schema}.zoning_districts GROUP BY 1, 2) d
        LEFT JOIN (SELECT jurisdiction, zone_code, count(*) AS parcels, count(*) FILTER (WHERE split) AS split_parcels
                   FROM {schema}.parcel_zoning GROUP BY 1, 2) p USING (jurisdiction, zone_code)
        ORDER BY d.jurisdiction, parcels DESC, d.zone_code
    """
    with engine.connect() as conn:
        return pd.read_sql(text(sql), conn)


def quality_report(engine: Engine, split_min_share: float, check_path: Path | None = None, schema: str = "public") -> dict:
    s = schema
    with engine.connect() as conn:
        d = conn.execute(text(
            f"SELECT count(*), count(DISTINCT (jurisdiction, zone_code)), count(*) FILTER (WHERE trimmed_acres > 0), "
            f"coalesce(sum(trimmed_acres), 0), count(*) FILTER (WHERE overlap_conflict) FROM {s}.zoning_districts"
        )).one()
        by_j = dict(conn.execute(text(
            f"SELECT jurisdiction, count(*) FROM {s}.parcel_zoning WHERE zone_code IS NOT NULL GROUP BY 1 ORDER BY 2 DESC"
        )).all())
        p = conn.execute(text(f"""
            SELECT count(*), count(*) FILTER (WHERE zone_code IS NULL),
                   count(*) FILTER (WHERE split),
                   count(*) FILTER (WHERE second_share >= 0.01), count(*) FILTER (WHERE second_share >= 0.10),
                   count(*) FILTER (WHERE crosses_jurisdiction),
                   count(*) FILTER (WHERE unzoned_share >= :m AND zone_code IS NOT NULL),
                   count(*) FILTER (WHERE overlap_conflict), count(*) FILTER (WHERE conditional),
                   count(*) FILTER (WHERE overlays IS NOT NULL)
            FROM {s}.parcel_zoning"""), {"m": split_min_share}).one()
        unzoned_sample = [r[0] for r in conn.execute(text(
            f"SELECT pin FROM {s}.parcel_zoning WHERE zone_code IS NULL ORDER BY pin LIMIT 20")).all()]
        mine = pd.read_sql(text(f"SELECT pin, zone_code FROM {s}.parcel_zoning WHERE zone_code IS NOT NULL"), conn)

    report = {
        "districts": d[0], "district_codes": d[1], "districts_trimmed_by_overlap": d[2],
        "overlap_acres_trimmed": round(float(d[3]), 1), "districts_overlap_conflict": d[4],
        "parcels": p[0], "parcels_unzoned": p[1], "parcels_unzoned_sample": unzoned_sample,
        "split_min_share": split_min_share, "parcels_split": p[2],
        "parcels_split_at_1pct": p[3], "parcels_split_at_10pct": p[4],
        "parcels_crossing_jurisdictions": p[5], "parcels_partly_unzoned": p[6],
        "parcels_overlap_conflict": p[7], "parcels_conditional": p[8], "parcels_with_overlay": p[9],
        "parcels_by_jurisdiction": by_j,
    }
    if check_path is not None and check_path.exists():
        report |= cross_check(mine, check_path)
    return report


def cross_check(mine: pd.DataFrame, check_path: Path) -> dict:
    """Is our main district among the districts the county lists for that parcel?"""
    county = pd.read_csv(check_path, dtype=str, encoding="utf-8-sig", usecols=["pid", "zone_class"])
    county["zone_class"] = county["zone_class"].str.strip()
    listed = county.groupby("pid")["zone_class"].agg(set)
    m = mine.join(listed, on="pin")
    has = m["zone_class"].notna()
    agree = [z in s for z, s in zip(m.loc[has, "zone_code"], m.loc[has, "zone_class"])]
    disagree = m.loc[has][[not a for a in agree]]
    return {
        "check_parcels_compared": int(has.sum()),
        "check_agree": int(sum(agree)),
        "check_disagree": int(len(disagree)),
        "check_disagree_sample": [
            {"pin": r.pin, "ours": r.zone_code, "county": sorted(r.zone_class)} for r in disagree.head(15).itertuples()
        ],
        "check_parcels_missing_from_county": int((~has).sum()),
    }


def format_report(r: dict, changes: dict) -> str:
    lines = [
        "Zoning quality report",
        f"  district shapes                {r['districts']:>8,}  ({r['district_codes']:,} distinct jurisdiction+code)",
        f"  shapes trimmed by overlaps     {r['districts_trimmed_by_overlap']:>8,}  ({r['overlap_acres_trimmed']:,} acres;"
        f" {r['districts_overlap_conflict']:,} same-date ties)",
        f"  parcels                        {r['parcels']:>8,}",
        f"  parcels with no zoning         {r['parcels_unzoned']:>8,}",
        f"  split parcels (2nd >= {r['split_min_share']:.0%})     {r['parcels_split']:>8,}"
        f"  (at 1%: {r['parcels_split_at_1pct']:,}; at 10%: {r['parcels_split_at_10pct']:,})",
        f"  split across jurisdictions     {r['parcels_crossing_jurisdictions']:>8,}",
        f"  partly outside any district    {r['parcels_partly_unzoned']:>8,}",
        f"  in a same-date overlap area    {r['parcels_overlap_conflict']:>8,}",
        f"  conditional (CD) main district {r['parcels_conditional']:>8,}",
        f"  with an overlay                {r['parcels_with_overlay']:>8,}",
        "  parcels by jurisdiction:",
        *[f"    {j:<14} {n:>8,}" for j, n in r["parcels_by_jurisdiction"].items()],
    ]
    if "check_agree" in r:
        lines += [
            f"  county cross-check: ours is one of the county's listed districts for "
            f"{r['check_agree']:,} of {r['check_parcels_compared']:,} parcels "
            f"({r['check_agree'] / max(r['check_parcels_compared'], 1):.2%}); {r['check_disagree']:,} disagree",
        ]
    lines.append("  changes since last load:")
    for table, c in changes.items():
        lines.append(f"    {table:<17} +{c['added']:,} new, {c['changed']:,} changed, -{c['removed']:,} removed"
                     f"  -> {c['current']:,} current")
    return "\n".join(lines)


def log_run(engine: Engine, as_of: date, sources: dict, report: dict, changes: dict) -> None:
    wrote = any(c["added"] or c["changed"] or c["removed"] for c in changes.values())
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO ingest_runs (source, snapshot_date, source_file, snapshot_written, report) "
                 "VALUES ('zoning', :d, :f, :w, CAST(:r AS jsonb))"),
            {"d": as_of, "f": json.dumps(sources), "w": wrote,
             "r": json.dumps({**report, "changes": changes}, default=str)},
        )


def main() -> None:
    cfg = yaml.safe_load((ROOT / "config" / "ingest.yaml").read_text())["zoning"]
    paths = {"charlotte": ROOT / cfg["charlotte"], "towns": ROOT / cfg["towns"]}
    missing = [str(p) for p in paths.values() if not p.exists()]
    if missing:
        raise FileNotFoundError(f"Missing zoning downloads: {missing}. See config/ingest.yaml.")
    mtimes = {k: datetime.fromtimestamp(os.path.getmtime(p)) for k, p in paths.items()}
    as_of = max(mtimes.values()).date()
    sources = {k: f"{p.relative_to(ROOT).as_posix()} (downloaded {mtimes[k]:%Y-%m-%d %H:%M})" for k, p in paths.items()}
    print(f"As-of date {as_of}")

    districts = build_districts(read_charlotte(paths["charlotte"]), read_towns(paths["towns"], cfg["towns_layer"]))
    engine = get_engine()
    changes = {"zoning_districts": load_districts(districts, engine, as_of)}
    print(f"  zoning_districts loaded: {changes['zoning_districts']}")
    changes["parcel_zoning"] = assign_parcels(engine, as_of, cfg["split_min_share"])

    report = quality_report(engine, cfg["split_min_share"], ROOT / cfg["county_check"])
    log_run(engine, as_of, sources, report, changes)

    counts = district_counts(engine)
    out = ROOT / "data" / "reports" / "zoning_district_counts.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    counts.to_csv(out, index=False)
    print()
    print(format_report(report, changes))
    print(f"\nDistrict list ({len(counts)} codes) written to {out.relative_to(ROOT).as_posix()}")


if __name__ == "__main__":
    main()
