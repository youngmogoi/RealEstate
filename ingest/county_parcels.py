"""Load Mecklenburg County's own parcel downloads: boundaries, ownership, and sales.

Run with `make ingest`. Source files and rules live in config/ingest.yaml.

Tables (each also has a `_history` table; see ingest/versioning.py):
- parcels: one row per parcel shape (PIN), with owner/value fields joined in.
- parcel_owners: one row per county account (`account_pin`). Condo buildings have one
  shape but one account per unit, all sharing the building's PIN.
- parcel_sales: every recorded sale.

Notes on the files (checked 2026-10-26 against that day's download):
- num_totalac is in the unit named by txt_landunittype: acres for ACRE, square feet for
  SQUARE FEET, a lot count for LOT. `assessor_acres` converts the first two and is null
  otherwise. It is the assessor's figure, not a surveyed deed acreage.
- The ownership file has no year built.
- The sales file repeats ~73k rows exactly; they are dropped.
"""

import json
import os
from datetime import date, datetime
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import shapely
import yaml
from sqlalchemy import Engine, text

from ingest.common import acres_mismatch, clean_dates, fix_geometries, strip_strings
from ingest.db import copy_dataframe, get_engine
from ingest.versioning import apply_versioned, row_hash

ROOT = Path(__file__).resolve().parent.parent
CRS_EPSG = 2264
DATE_FMT = "%m/%d/%Y %H:%M:%S.%f"
SQFT_PER_ACRE = 43_560

BOUNDARY_FIELDS = {
    "pid": "pin",
    "nc_pin": "nc_pin",
    "map_book": "map_book",
    "map_page": "map_page",
    "map_block": "map_block",
    "lot_num": "lot_num",
    "condo_town_flag": "condo_town_flag",
    "parcel_type": "parcel_type",
    "legal_from": "legal_changed_on",
    "gisacres": "gis_acres",
}

OWNER_FIELDS = {
    "camapid": "account_pin",
    "pid": "pin",
    "propertyid": "property_id",
    "full_owner_name": "owner_name",
    "txt_mailaddr1": "mail_address1",
    "txt_mailaddr2": "mail_address2",
    "txt_city": "mail_city",
    "txt_state": "mail_state",
    "txt_zipcode": "mail_zip",
    "situsaddress1": "site_address",
    "municipality_desc": "municipality",
    "amt_landvalue": "land_value",
    "amt_netbldgvalue": "building_value",
    "amt_extrafeaturevalue": "extra_feature_value",
    "amt_totalvalue": "total_value",
    "txt_landunittype": "land_unit_type",
    "num_totalac": "land_units",
    "txt_legaldesc": "legal_description",
    "txt_propertyuse_desc": "parcel_use",
    "dte_dateofsale": "last_sale_date",
    "amt_price": "last_sale_price",
    "txt_deedbook": "deed_book",
    "txt_deedpage": "deed_page",
}
VALUE_FIELDS = ["land_value", "building_value", "extra_feature_value", "total_value"]

SALES_FIELDS = {
    "parcelid": "pin",
    "transferid": "transfer_id",
    "propertyid": "property_id",
    "saleprice": "sale_price",
    "saledate": "sale_date",
    "soldasvacantflag": "sold_as_vacant",
    "landusefulldescription": "land_use_at_sale",
    "landuse": "land_use_code_at_sale",
    "deeddescription": "deed_type",
    "legalreference": "deed_ref",
    "salesvalidity": "validity_code",
    "naldesc": "validity_desc",
    "grantor": "seller",
    "grantee": "buyer",
}
SALES_KEY = ["pin", "transfer_id", "property_id"]

# Owner fields copied onto a parcel only when the parcel has exactly one account
SINGLE_ACCOUNT_FIELDS = [
    "owner_name", "owner2_name", "mail_address1", "mail_address2", "mail_city", "mail_state", "mail_zip",
    "site_address", "municipality", "land_unit_type", "assessor_acres", "last_sale_date", "last_sale_price",
    "deed_ref",
]


def read_boundaries(path: Path) -> gpd.GeoDataFrame:
    gdf = pyogrio.read_dataframe(path, columns=list(BOUNDARY_FIELDS))
    if gdf.crs is None:
        raise ValueError(f"{path} has no projection")
    if gdf.crs.to_epsg(min_confidence=20) == CRS_EPSG:
        return gdf.set_crs(CRS_EPSG, allow_override=True)
    return gdf.to_crs(CRS_EPSG)


def read_table(path: Path, columns: list[str]) -> pd.DataFrame:
    return pd.read_csv(
        path, dtype=str, encoding="utf-8-sig", usecols=columns, keep_default_na=False, na_values=[""]
    )


def assessor_acres(unit_type: pd.Series, units: pd.Series) -> pd.Series:
    acres = np.select(
        [unit_type == "ACRE", unit_type == "SQUARE FEET"], [units, units / SQFT_PER_ACRE], default=np.nan
    )
    return pd.Series(acres, index=units.index, dtype="float64")


def clean_owners(raw: pd.DataFrame, placeholder_sale_dates: list[str], as_of: date) -> pd.DataFrame:
    df = strip_strings(raw.copy())
    owner2 = (df["secownerlastname"].fillna("") + " " + df["secownerfirstname"].fillna("")).str.strip()
    df = df[list(OWNER_FIELDS)].rename(columns=OWNER_FIELDS)
    df.insert(df.columns.get_loc("owner_name") + 1, "owner2_name", owner2.replace("", None))

    for col in VALUE_FIELDS + ["land_units", "last_sale_price"]:
        df[col] = pd.to_numeric(df[col])
    df["assessor_acres"] = assessor_acres(df["land_unit_type"], df["land_units"])

    df["last_sale_date"], df["last_sale_date_placeholder"], df["last_sale_date_after_as_of"] = clean_dates(
        df["last_sale_date"], placeholder_sale_dates, as_of, DATE_FMT
    )
    has_ref = df["deed_book"].notna() & df["deed_page"].notna()
    df["deed_ref"] = (df["deed_book"] + "/" + df["deed_page"]).where(has_ref)
    return df


def clean_sales_rows(raw: pd.DataFrame, placeholder_sale_dates: list[str], as_of: date) -> pd.DataFrame:
    """Row-by-row cleaning only, so it can run on chunks of the file."""
    df = strip_strings(raw[list(SALES_FIELDS)].rename(columns=SALES_FIELDS))
    df["sale_price"] = pd.to_numeric(df["sale_price"]).astype("float64")
    df["sold_as_vacant"] = df["sold_as_vacant"].map({"Yes": True, "No": False}).astype("boolean")
    df["sale_date"], df["sale_date_placeholder"], df["sale_date_after_as_of"] = clean_dates(
        df["sale_date"], placeholder_sale_dates, as_of, DATE_FMT
    )
    return df


def clean_sales(raw: pd.DataFrame, placeholder_sale_dates: list[str], as_of: date) -> tuple[pd.DataFrame, dict]:
    """Clean and de-duplicate in memory. For the full file use load_sales, which de-duplicates in the database."""
    df = clean_sales_rows(raw, placeholder_sale_dates, as_of)
    before = len(df)
    df = df.drop_duplicates()
    exact = before - len(df)
    df = df.drop_duplicates(SALES_KEY)
    return df.reset_index(drop=True), {"exact_duplicates_dropped": exact, "duplicate_keys_dropped": before - exact - len(df)}


def build_parcels(
    boundaries: gpd.GeoDataFrame, owners: pd.DataFrame, acres_mismatch_threshold: float
) -> gpd.GeoDataFrame:
    b = strip_strings(boundaries.rename(columns=BOUNDARY_FIELDS).rename_geometry("geom"))
    for col in ("condo_town_flag", "parcel_type"):
        b[col] = b[col].astype("Int64")
    b["legal_changed_on"] = pd.to_datetime(b["legal_changed_on"], utc=True).dt.tz_localize(None).dt.normalize()

    by_pin = owners.groupby("pin")
    agg = by_pin[VALUE_FIELDS].sum(min_count=1)
    agg.insert(0, "account_count", by_pin.size())
    agg["parcel_use"] = by_pin["parcel_use"].first()
    single = owners[owners["pin"].map(by_pin.size()) == 1].set_index("pin")[SINGLE_ACCOUNT_FIELDS]

    df = b.merge(agg, left_on="pin", right_index=True, how="left")
    df = df.merge(single, left_on="pin", right_index=True, how="left")
    df["account_count"] = df["account_count"].fillna(0).astype("int64")
    df["has_owner_record"] = df["account_count"] > 0
    df["acres_mismatch"] = acres_mismatch(df["gis_acres"], df["assessor_acres"], acres_mismatch_threshold)

    geoms, df["geom_fixed"] = fix_geometries(df.geometry)
    df["geom"] = [g if g.geom_type == "MultiPolygon" else shapely.MultiPolygon([g]) for g in geoms]

    cols = [c for c in df.columns if c != "geom"] + ["geom"]
    return gpd.GeoDataFrame(df[cols], geometry="geom", crs=CRS_EPSG)


def quality_report(
    parcels: gpd.GeoDataFrame, owners: pd.DataFrame, sales: pd.DataFrame, sales_dupes: dict, threshold: float
) -> dict:
    return {**parcels_report(parcels, owners, threshold), **sales_report(sales, sales_dupes)}


def sales_report(sales: pd.DataFrame, sales_dupes: dict) -> dict:
    return {
        "sales": len(sales),
        "sale_dates_placeholder": int(sales["sale_date_placeholder"].sum()),
        "sale_dates_after_as_of": int(sales["sale_date_after_as_of"].sum()),
        "latest_sale_date": str(sales["sale_date"].max().date()),
        **{f"sales_{k}": v for k, v in sales_dupes.items()},
    }


def parcels_report(parcels: gpd.GeoDataFrame, owners: pd.DataFrame, threshold: float) -> dict:
    no_owner = parcels.loc[~parcels["has_owner_record"], "pin"]
    mismatch = parcels.loc[parcels["acres_mismatch"], "pin"]
    owner_future = owners.loc[owners["last_sale_date_after_as_of"], "account_pin"]
    single = parcels["account_count"] == 1
    null_fields = [c for c in parcels.columns if c not in ("geom",) and parcels[c].dtype != bool]
    return {
        "parcels": len(parcels),
        "owner_accounts": len(owners),
        "parcels_without_owner_record": int(no_owner.size),
        "parcels_without_owner_record_sample": no_owner.head(10).tolist(),
        "owner_accounts_without_parcel": int((~owners["pin"].isin(parcels["pin"])).sum()),
        "multi_account_parcels": int((parcels["account_count"] > 1).sum()),
        "accounts_on_multi_account_parcels": int(parcels.loc[parcels["account_count"] > 1, "account_count"].sum()),
        "single_account_parcels_with_assessor_acres": int((single & parcels["assessor_acres"].notna()).sum()),
        "acres_mismatch_threshold": threshold,
        "acres_mismatch_count": int(mismatch.size),
        "acres_mismatch_pins_sample": mismatch.head(20).tolist(),
        "invalid_geometries_fixed": int(parcels["geom_fixed"].sum()),
        "owner_sale_dates_placeholder": int(owners["last_sale_date_placeholder"].sum()),
        "owner_sale_dates_after_as_of": int(owner_future.size),
        "owner_sale_dates_after_as_of_accounts": owner_future.tolist()[:20],
        "parcel_null_rates": {c: round(float(parcels[c].isna().mean()), 4) for c in null_fields},
    }


def format_report(r: dict, changes: dict | None = None) -> str:
    lines = [
        "County parcel data quality report",
        f"  parcels (shapes)               {r['parcels']:>10,}",
        f"  owner accounts                 {r['owner_accounts']:>10,}",
        f"  sales                          {r['sales']:>10,}  latest {r['latest_sale_date']}",
        f"  parcels with no owner record   {r['parcels_without_owner_record']:>10,}",
        f"  owner accounts with no shape   {r['owner_accounts_without_parcel']:>10,}",
        f"  multi-account parcels          {r['multi_account_parcels']:>10,}  holding "
        f"{r['accounts_on_multi_account_parcels']:,} accounts (condos etc.)",
        f"  GIS vs assessor acres >{r['acres_mismatch_threshold']:.0%}    {r['acres_mismatch_count']:>10,}  of "
        f"{r['single_account_parcels_with_assessor_acres']:,} with assessor acres",
        f"  invalid geometries fixed       {r['invalid_geometries_fixed']:>10,}",
        f"  placeholder sale dates nulled  {r['owner_sale_dates_placeholder']:>10,} owner / "
        f"{r['sale_dates_placeholder']:,} sales",
        f"  future sale dates nulled       {r['owner_sale_dates_after_as_of']:>10,} owner / "
        f"{r['sale_dates_after_as_of']:,} sales",
        f"  duplicate sales dropped        {r['sales_exact_duplicates_dropped']:>10,} exact / "
        f"{r['sales_duplicate_keys_dropped']:,} same key",
    ]
    if changes:
        lines.append("  changes since last load:")
        for table, c in changes.items():
            lines.append(
                f"    {table:<15} +{c['added']:,} new, {c['changed']:,} changed, -{c['removed']:,} removed"
                f"  -> {c['current']:,} current"
            )
    lines.append("  parcel null rate per field:")
    lines += [f"    {c:<26} {rate:>7.1%}" for c, rate in r["parcel_null_rates"].items()]
    return "\n".join(lines)


def load(
    parcels: gpd.GeoDataFrame,
    owners: pd.DataFrame,
    sales: pd.DataFrame,
    engine: Engine,
    as_of: date,
    sources: dict,
    report: dict,
    schema: str = "public",
) -> dict:
    """Merge this load into parcels / parcel_owners / parcel_sales, keeping history. Returns change counts."""
    changes = {
        "parcels": load_parcels(parcels, engine, as_of, schema),
        "parcel_owners": load_table(owners, "parcel_owners", ["account_pin"], engine, as_of, schema),
        "parcel_sales": load_table(sales, "parcel_sales", SALES_KEY, engine, as_of, schema),
    }
    log_run(engine, as_of, sources, report, changes, schema)
    return changes


def _with_hash(chunk: pd.DataFrame) -> pd.DataFrame:
    return chunk.assign(row_hash=row_hash(chunk))


def _parcel_rows(chunk: gpd.GeoDataFrame) -> pd.DataFrame:
    geoms = shapely.set_srid(chunk.geometry.values, CRS_EPSG)
    rows = pd.DataFrame(chunk.drop(columns="geom")).assign(geom=shapely.to_wkb(geoms, hex=True, include_srid=True))
    return _with_hash(rows)


INDEXES = {
    "parcels": "USING gist (geom)",
    "parcel_owners": "(pin)",
    "parcel_sales": "(sale_date)",
}


def load_table(
    df: pd.DataFrame, name: str, key: list[str], engine: Engine, as_of: date, schema: str = "public",
    types: dict | None = None, prepare=_with_hash,
) -> dict:
    """Merge one table in its own transaction. Returns change counts."""
    table = f"{schema}.{name}"
    with engine.begin() as conn:
        copy_dataframe(conn, df, f"{table}_staging", types, prepare)
        changes = apply_versioned(conn, table, key, as_of)
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS {name}_extra_idx ON {table} {INDEXES[name]}"))
    return changes


def load_sales(
    path: Path, placeholder_sale_dates: list[str], as_of: date, engine: Engine, schema: str = "public",
    chunk_rows: int = 50_000,
) -> tuple[dict, dict]:
    """Stream the sales file into staging in chunks, de-duplicate in SQL, then merge.

    Of rows sharing a key, the one with the lowest row_hash is kept; exact duplicates are
    identical, so which one is kept doesn't matter. Returns (change counts, report fields).
    """
    table, staging = f"{schema}.parcel_sales", f"{schema}.parcel_sales_staging"
    keycols = ", ".join(SALES_KEY)
    reader = pd.read_csv(
        path, dtype=str, encoding="utf-8-sig", usecols=list(SALES_FIELDS), keep_default_na=False,
        na_values=[""], chunksize=chunk_rows,
    )
    with engine.begin() as conn:
        for i, raw in enumerate(reader):
            copy_dataframe(conn, clean_sales_rows(raw, placeholder_sale_dates, as_of), staging,
                           prepare=_with_hash, append=i > 0)

        total, distinct_rows, distinct_keys = conn.execute(text(
            f"SELECT count(*), count(DISTINCT ({keycols}, row_hash)), count(DISTINCT ({keycols})) FROM {staging}"
        )).one()
        # In place, so the disk never holds a second copy of the sales
        conn.execute(text(
            f"DELETE FROM {staging} s USING (SELECT ctid, row_number() OVER "
            f"(PARTITION BY {keycols} ORDER BY row_hash) AS n FROM {staging}) d "
            "WHERE s.ctid = d.ctid AND d.n > 1"
        ))

        placeholder, future, latest = conn.execute(text(
            f"SELECT count(*) FILTER (WHERE sale_date_placeholder), count(*) FILTER (WHERE sale_date_after_as_of), "
            f"max(sale_date) FROM {staging}"
        )).one()
        changes = apply_versioned(conn, table, SALES_KEY, as_of)
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS parcel_sales_extra_idx ON {table} {INDEXES['parcel_sales']}"))

    report = {
        "sales": distinct_keys,
        "sale_dates_placeholder": placeholder,
        "sale_dates_after_as_of": future,
        "latest_sale_date": str(latest),
        "sales_exact_duplicates_dropped": total - distinct_rows,
        "sales_duplicate_keys_dropped": distinct_rows - distinct_keys,
    }
    return changes, report


def load_parcels(parcels: gpd.GeoDataFrame, engine: Engine, as_of: date, schema: str = "public") -> dict:
    return load_table(
        parcels, "parcels", ["pin"], engine, as_of, schema,
        types={"geom": f"geometry(MultiPolygon, {CRS_EPSG})"}, prepare=_parcel_rows,
    )


def log_run(engine: Engine, as_of: date, sources: dict, report: dict, changes: dict, schema: str = "public") -> None:
    with engine.begin() as conn:
        conn.execute(text(
            f"CREATE TABLE IF NOT EXISTS {schema}.ingest_runs ("
            "id serial PRIMARY KEY, source text, snapshot_date date, source_file text, "
            "loaded_at timestamptz DEFAULT now(), snapshot_written boolean, report jsonb)"
        ))
        wrote = any(c["added"] or c["changed"] or c["removed"] for c in changes.values())
        conn.execute(
            text(
                f"INSERT INTO {schema}.ingest_runs (source, snapshot_date, source_file, snapshot_written, report) "
                "VALUES ('county_parcels', :d, :f, :w, CAST(:r AS jsonb))"
            ),
            {"d": as_of, "f": json.dumps(sources), "w": wrote,
             "r": json.dumps({**report, "changes": changes})},
        )


def source_files(cfg: dict) -> tuple[dict[str, Path], dict[str, str], date]:
    """Paths from config, their download times, and the as-of date (newest download day)."""
    paths = {name: ROOT / cfg[name] for name in ("boundaries", "owners", "sales")}
    missing = [str(p) for p in paths.values() if not p.exists()]
    if missing:
        raise FileNotFoundError(f"Missing county downloads: {missing}. See config/ingest.yaml.")
    mtimes = {name: datetime.fromtimestamp(os.path.getmtime(p)) for name, p in paths.items()}
    sources = {name: f"{p.relative_to(ROOT).as_posix()} (downloaded {mtimes[name]:%Y-%m-%d %H:%M})"
               for name, p in paths.items()}
    return paths, sources, max(mtimes.values()).date()


def main() -> None:
    cfg = yaml.safe_load((ROOT / "config" / "ingest.yaml").read_text())
    paths, sources, as_of = source_files(cfg["county"])
    placeholders, threshold = cfg["placeholder_sale_dates"], cfg["acres_mismatch_threshold"]
    print(f"As-of date {as_of}")
    for s in sources.values():
        print(f"  {s}")

    # One table at a time, freeing each before the next, to fit in a small machine's memory.
    engine, changes = get_engine(), {}
    owners = clean_owners(
        read_table(paths["owners"], list(OWNER_FIELDS) + ["secownerlastname", "secownerfirstname"]),
        placeholders, as_of,
    )
    parcels = build_parcels(read_boundaries(paths["boundaries"]), owners, threshold)
    report = parcels_report(parcels, owners, threshold)
    changes["parcels"] = load_parcels(parcels, engine, as_of)
    print(f"  parcels loaded: {changes['parcels']}")
    del parcels
    changes["parcel_owners"] = load_table(owners, "parcel_owners", ["account_pin"], engine, as_of)
    print(f"  parcel_owners loaded: {changes['parcel_owners']}")
    del owners

    changes["parcel_sales"], sales_fields = load_sales(paths["sales"], placeholders, as_of, engine)
    report |= sales_fields

    log_run(engine, as_of, sources, report, changes)
    print()
    print(format_report(report, changes))


if __name__ == "__main__":
    main()
