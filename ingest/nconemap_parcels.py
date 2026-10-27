"""Load NC OneMap county parcels into `nconemap_parcels` and keep a dated copy in `nconemap_parcels_snapshots`.

Run with `make ingest-nconemap`. Settings live in config/ingest.yaml.

Notes on the Mecklenburg file (checked 2026-10-26 against the 2026-01-07 download):
- A parcel with several buildings appears once per building, with identical geometry and
  the parcel's full land/building values repeated on every row. Rows are kept as-is and
  `pin_row_count` says how many share the PIN, so values must not be summed across rows.
- There is no deeded acreage. RECAREANO is filled for 139 rows and holds square feet or
  lot counts, so `deeded_acres` is null until another source supplies it.
- There is no deed date. `sale_date` comes from SALEDATE; `deed_ref` is the deed book/page.
"""

import glob
import json
import re
from datetime import date, datetime
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import yaml
from sqlalchemy import Date, Engine, text

from ingest.common import acres_mismatch, fix_geometries
from ingest.db import get_engine

ROOT = Path(__file__).resolve().parent.parent
CRS_EPSG = 2264

# NC OneMap standard field -> parcels column
FIELDS = {
    "PARNO": "pin",
    "ALTPARNO": "alt_pin",
    "OWNNAME": "owner_name",
    "OWNNAME2": "owner_name2",
    "MAILADD": "mail_address",
    "MCITY": "mail_city",
    "MSTATE": "mail_state",
    "MZIP": "mail_zip",
    "SITEADD": "site_address",
    "SCITY": "site_city",
    "SZIP": "site_zip",
    "GISACRES": "gis_acres",
    "LANDVAL": "land_value",
    "IMPROVVAL": "building_value",
    "PARVAL": "total_value",
    "PARUSECODE": "parcel_use_code",
    "PARUSEDESC": "parcel_use",
    "PARUSEDSC2": "parcel_use_detail",
    "SALEDATE": "sale_date",
    "SOURCEREF": "deed_ref",
    "SUBDIVISIO": "subdivision",
    "STRUCTYEAR": "year_built",
}

REPORT_FIELDS = list(FIELDS.values()) + ["deeded_acres"]


def read_publication_date(shp_path: Path) -> date:
    """Publication_Date from the NC OneMap metadata file next to the shapefile."""
    for meta in shp_path.parent.glob("*_metadata.txt"):
        match = re.search(r"Publication_Date:\s*(\d{8})", meta.read_text(errors="replace"))
        if match:
            return datetime.strptime(match.group(1), "%Y%m%d").date()
    raise ValueError(f"No Publication_Date found in metadata next to {shp_path}")


def find_source(pattern: str) -> tuple[Path, date]:
    """Newest download matching `pattern`, by publication date."""
    paths = [Path(p) for p in glob.glob(str(ROOT / pattern))]
    if not paths:
        raise FileNotFoundError(
            f"No parcel file matches {pattern}. Download the county from NC OneMap "
            "and unzip it into its own folder under data/raw/."
        )
    dated = [(p, read_publication_date(p)) for p in paths]
    return max(dated, key=lambda pd_: pd_[1])


def read_source(path: Path) -> gpd.GeoDataFrame:
    gdf = pyogrio.read_dataframe(path, columns=list(FIELDS), fid_as_index=True)
    if gdf.crs is None:
        raise ValueError(f"{path} has no projection (.prj)")
    # The NC OneMap .prj is ESRI-style NC State Plane ftUS with rounded parameters, so it
    # only matches EPSG:2264 at relaxed confidence. Label it rather than reproject.
    if gdf.crs.to_epsg(min_confidence=20) == CRS_EPSG:
        gdf = gdf.set_crs(CRS_EPSG, allow_override=True)
    else:
        gdf = gdf.to_crs(CRS_EPSG)
    gdf["source_row"] = gdf.index.astype("int64")
    return gdf.reset_index(drop=True)


def clean(
    raw: gpd.GeoDataFrame, placeholder_sale_dates: list[str], acres_mismatch_threshold: float
) -> gpd.GeoDataFrame:
    df = raw.rename(columns=FIELDS).rename_geometry("geom")

    for col in df.select_dtypes(include=["object", "str"]).columns:
        df[col] = df[col].str.strip().replace("", None)

    df["sale_date"] = pd.to_datetime(df["sale_date"])
    placeholders = pd.to_datetime(placeholder_sale_dates)
    df["sale_date_placeholder"] = df["sale_date"].isin(placeholders)
    df.loc[df["sale_date_placeholder"], "sale_date"] = pd.NaT

    # 0 means no structure on the parcel
    df["year_built"] = df["year_built"].astype("Int64").replace(0, pd.NA)

    df["deeded_acres"] = np.nan
    df["acres_mismatch"] = acres_mismatch(df["gis_acres"], df["deeded_acres"], acres_mismatch_threshold)

    df["pin_row_count"] = df.groupby("pin")["pin"].transform("size").astype("int64")

    df["geom"], df["geom_fixed"] = fix_geometries(df.geometry)

    cols = ["source_row"] + list(FIELDS.values()) + [
        "deeded_acres", "pin_row_count", "sale_date_placeholder", "acres_mismatch", "geom_fixed", "geom",
    ]
    return df[cols]


def quality_report(df: gpd.GeoDataFrame, acres_mismatch_threshold: float) -> dict:
    dup = df["pin_row_count"] > 1
    mismatch = df.loc[df["acres_mismatch"], "pin"]
    return {
        "rows": len(df),
        "unique_pins": int(df["pin"].nunique()),
        "null_rates": {c: round(float(df[c].isna().mean()), 4) for c in REPORT_FIELDS},
        "duplicate_pin_groups": int(df.loc[dup, "pin"].nunique()),
        "duplicate_pin_rows": int(dup.sum()),
        "placeholder_sale_dates_nulled": int(df["sale_date_placeholder"].sum()),
        "parcels_with_deeded_acres": int(df["deeded_acres"].notna().sum()),
        "acres_mismatch_threshold": acres_mismatch_threshold,
        "acres_mismatch_count": int(mismatch.size),
        "acres_mismatch_pins_sample": mismatch.head(20).tolist(),
        "invalid_geometries_fixed": int(df["geom_fixed"].sum()),
    }


def format_report(r: dict) -> str:
    lines = [
        "Parcel data quality report",
        f"  rows                      {r['rows']:>10,}",
        f"  unique PINs               {r['unique_pins']:>10,}",
        f"  duplicate-PIN rows        {r['duplicate_pin_rows']:>10,}  in {r['duplicate_pin_groups']:,} PINs"
        " (one row per building; values repeat, don't sum)",
        f"  placeholder sale dates    {r['placeholder_sale_dates_nulled']:>10,}  set to null",
        f"  invalid geometries fixed  {r['invalid_geometries_fixed']:>10,}",
    ]
    if r["parcels_with_deeded_acres"] == 0:
        lines.append("  GIS vs deeded acres       not checked: no parcel has deeded acres in this source")
    else:
        lines.append(
            f"  GIS vs deeded acres >{r['acres_mismatch_threshold']:.0%}  {r['acres_mismatch_count']:>8,}"
            f"  of {r['parcels_with_deeded_acres']:,} with deeded acres"
        )
    lines.append("  null rate per field:")
    lines += [f"    {c:<20} {rate:>7.1%}" for c, rate in r["null_rates"].items()]
    return "\n".join(lines)


def load(
    df: gpd.GeoDataFrame,
    engine: Engine,
    snapshot_date: date,
    source_file: str,
    report: dict,
    schema: str = "public",
) -> bool:
    """Replace `nconemap_parcels` with this load; add it to `nconemap_parcels_snapshots` unless that date is already there.

    Returns True if a new snapshot was written.
    """
    df = df.assign(snapshot_date=snapshot_date, source_file=source_file)
    df.to_postgis(
        "nconemap_parcels_staging", engine, schema=schema, if_exists="replace", index=False, chunksize=10_000,
        dtype={"sale_date": Date(), "snapshot_date": Date()},
    )
    s = schema
    with engine.begin() as conn:
        conn.execute(text(
            f"ALTER TABLE {s}.nconemap_parcels_staging "
            "ALTER COLUMN geom TYPE geometry(MultiPolygon, 2264) USING ST_Multi(geom)"
        ))
        conn.execute(text(f"CREATE TABLE IF NOT EXISTS {s}.nconemap_parcels_snapshots (LIKE {s}.nconemap_parcels_staging)"))
        conn.execute(text(
            f"CREATE INDEX IF NOT EXISTS nconemap_parcels_snapshots_date_pin_idx ON {s}.nconemap_parcels_snapshots (snapshot_date, pin)"
        ))
        exists = conn.execute(
            text(f"SELECT EXISTS (SELECT 1 FROM {s}.nconemap_parcels_snapshots WHERE snapshot_date = :d)"),
            {"d": snapshot_date},
        ).scalar_one()
        if not exists:
            conn.execute(text(f"INSERT INTO {s}.nconemap_parcels_snapshots SELECT * FROM {s}.nconemap_parcels_staging"))

        conn.execute(text(f"DROP TABLE IF EXISTS {s}.nconemap_parcels"))
        conn.execute(text(f"ALTER TABLE {s}.nconemap_parcels_staging RENAME TO nconemap_parcels"))
        conn.execute(text(f"ALTER INDEX IF EXISTS {s}.idx_nconemap_parcels_staging_geom RENAME TO nconemap_parcels_geom_idx"))
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS nconemap_parcels_geom_idx ON {s}.nconemap_parcels USING gist (geom)"))
        conn.execute(text(f"CREATE INDEX nconemap_parcels_pin_idx ON {s}.nconemap_parcels (pin)"))

        conn.execute(text(
            f"CREATE TABLE IF NOT EXISTS {s}.ingest_runs ("
            "id serial PRIMARY KEY, source text, snapshot_date date, source_file text, "
            "loaded_at timestamptz DEFAULT now(), snapshot_written boolean, report jsonb)"
        ))
        conn.execute(
            text(
                f"INSERT INTO {s}.ingest_runs (source, snapshot_date, source_file, snapshot_written, report) "
                "VALUES ('nconemap_parcels', :d, :f, :w, CAST(:r AS jsonb))"
            ),
            {"d": snapshot_date, "f": source_file, "w": not exists, "r": json.dumps(report)},
        )
    return not exists


def main() -> None:
    cfg = yaml.safe_load((ROOT / "config" / "ingest.yaml").read_text())
    path, published = find_source(cfg["nconemap"]["source_glob"])
    source_file = path.relative_to(ROOT).as_posix()
    print(f"Reading {source_file} (published {published})")

    threshold = cfg["acres_mismatch_threshold"]
    df = clean(read_source(path), cfg["placeholder_sale_dates"], threshold)
    report = quality_report(df, threshold)
    written = load(df, get_engine(), published, source_file, report)

    print(f"Loaded {len(df):,} rows into parcels.")
    print(f"Snapshot {published}: {'written' if written else 'already existed, left unchanged'}.")
    print()
    print(format_report(report))


if __name__ == "__main__":
    main()
