"""Load the county's CAMA building data ("Tax Parcel CAMA Data"): heated area and year built.

Run with `make cama` (after `make ingest`). The file is named in config/ingest.yaml (county.cama).

Table `parcel_cama` (history in parcel_cama_history; see ingest/versioning.py): one row per county
account (`account_pin`, the CAMA `parcelid`, the same key as parcel_owners.account_pin and
parcel_sales.pin), with `pin` = the parcel shape it sits on.

Notes on the file (checked 2026-10-28 against that day's download; its metadata defines no fields):
- One row per BUILDING. Account-level fields repeat on each of an account's rows and never differ
  within an account: heatedarea, the values, the last sale, land use. Per-building fields:
  bldgtype, yearbuilt, effyearblt, storyheight, grade, finisharea, totalarea, resunits, bedrooms,
  baths. On houses heatedarea is the total of the buildings (12708101: 3,089 = 2,171 + 918); on
  an apartment complex it is not (05101114: 17,148 vs 119,963 sf finished in 11 buildings), so
  per-sf prices (scoring/rlv.py comps) use one-building accounts only.
- 5,946 rows repeat another row exactly; they are dropped (counted as one building).
- An account with no building has one row with blank building fields (building_count 0).
- yearid 2027 marks ~6,800 accounts created in 2026 and not yet assessed (no building data).
- resunits is 0 or blank on most new duplexes and triplexes, so it can't tell them apart.
- The building fields below (bldg_type, year_built, ...) are the account's LARGEST building by
  finished area; year_built_newest / year_built_oldest span all of its buildings.
"""

import json
import os
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml
from sqlalchemy import Engine, text

from ingest.common import strip_strings
from ingest.county_parcels import ROOT, load_table
from ingest.db import get_engine

ACCOUNT_FIELDS = {
    "parcelid": "account_pin",
    "gispid": "pin",
    "yearid": "tax_year",
    "lusecode": "land_use_code",
    "landuse_description": "land_use",
    "vacorimprov": "vac_or_improved",
    "taxmundist": "municipality",
    "heatedarea": "heated_sf",
    "totlandval": "land_value",
    "totalbldgval": "building_value",
    "totalvalue": "total_value",
}
BUILDING_FIELDS = {
    "bldgtype": "bldg_type",
    "yearbuilt": "year_built",
    "effyearblt": "eff_year_built",
    "storyheight": "story_height",
    "grade": "grade",
    "resunits": "res_units",
    "bedrooms": "bedrooms",
    "fullbath": "full_baths",
    "halfbath": "half_baths",
    "finisharea": "finished_sf",
    "totalarea": "total_area_sf",
}
NUMERIC = ["tax_year", "heated_sf", "land_value", "building_value", "total_value", "year_built", "eff_year_built",
           "res_units", "bedrooms", "full_baths", "half_baths", "finished_sf", "total_area_sf"]
INT_COLS = ["tax_year", "year_built", "eff_year_built", "year_built_newest", "year_built_oldest", "bedrooms",
            "full_baths", "half_baths", "res_units", "building_count"]
LARGEST = ["bldg_type", "year_built", "eff_year_built", "story_height", "grade", "bedrooms", "full_baths", "half_baths"]


def read_cama(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, encoding="utf-8-sig", usecols=list(ACCOUNT_FIELDS) + list(BUILDING_FIELDS),
                       keep_default_na=False, na_values=[""])


def build_accounts(raw: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """One row per account from the per-building rows. Returns (accounts, report fields)."""
    before = len(raw)
    df = strip_strings(raw.drop_duplicates().rename(columns=ACCOUNT_FIELDS | BUILDING_FIELDS))
    for c in NUMERIC:
        df[c] = pd.to_numeric(df[c])
    acct = list(ACCOUNT_FIELDS.values())
    by = df.groupby("account_pin")
    inconsistent = int((by[acct[1:]].nunique(dropna=False) > 1).any(axis=1).sum())

    is_bldg = df["bldg_type"].notna() | df["year_built"].notna()
    bldgs = df[is_bldg]
    # largest building first; ties broken by newest, then type, so the pick never depends on file order
    largest = bldgs.sort_values(["account_pin", "finished_sf", "year_built", "bldg_type"],
                                ascending=[True, False, False, True], na_position="last").drop_duplicates("account_pin")
    b = bldgs.groupby("account_pin")
    agg = pd.DataFrame({
        "building_count": b.size(),
        "year_built_newest": b["year_built"].max(),
        "year_built_oldest": b["year_built"].min(),
        "bldg_types": b["bldg_type"].agg(lambda s: "; ".join(sorted(set(s.dropna()))) or None),
        "res_units": b["res_units"].sum(min_count=1),
        "finished_sf": b["finished_sf"].sum(min_count=1),
        "total_area_sf": b["total_area_sf"].sum(min_count=1),
    })
    out = by[acct[1:]].first().join(largest.set_index("account_pin")[LARGEST]).join(agg).reset_index()
    out["building_count"] = out["building_count"].fillna(0)
    for c in INT_COLS:
        out[c] = out[c].round().astype("Int64")
    out = out[acct + LARGEST + ["year_built_newest", "year_built_oldest", "building_count", "bldg_types", "res_units",
                                "finished_sf", "total_area_sf"]].sort_values("account_pin").reset_index(drop=True)
    report = {
        "rows": before,
        "exact_duplicate_rows_dropped": before - len(df),
        "accounts": len(out),
        "parcel_shapes": int(out["pin"].nunique()),
        "accounts_inconsistent_across_rows": inconsistent,
        "multi_building_accounts": int((out["building_count"] > 1).sum()),
        "accounts_without_building": int((out["building_count"] == 0).sum()),
        "accounts_with_heated_sf": int((out["heated_sf"] > 0).sum()),
        "tax_years": {str(k): int(v) for k, v in out["tax_year"].value_counts().sort_index().items()},
    }
    return out, report


def format_report(r: dict, changes: dict | None = None) -> str:
    lines = [
        "County CAMA data",
        f"  building rows                  {r['rows']:>10,}  ({r['exact_duplicate_rows_dropped']:,} exact repeats dropped)",
        f"  accounts                       {r['accounts']:>10,}  on {r['parcel_shapes']:,} parcel shapes",
        f"  accounts with heated sf > 0    {r['accounts_with_heated_sf']:>10,}",
        f"  multi-building accounts        {r['multi_building_accounts']:>10,}",
        f"  accounts with no building      {r['accounts_without_building']:>10,}",
        f"  account fields that differ     {r['accounts_inconsistent_across_rows']:>10,}  (expected 0)",
        f"  tax years                      {r['tax_years']}",
    ]
    if changes:
        lines.append(f"  parcel_cama: +{changes['added']:,} new, {changes['changed']:,} changed, -{changes['removed']:,} "
                     f"removed -> {changes['current']:,} current" +
                     (f"; columns added {changes['columns_added']}" if changes.get("columns_added") else ""))
    return "\n".join(lines)


def log_run(engine: Engine, as_of, source: str, report: dict, changes: dict) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO ingest_runs (source, snapshot_date, source_file, snapshot_written, report) "
                 "VALUES ('county_cama', :d, :f, :w, CAST(:r AS jsonb))"),
            {"d": as_of, "f": source, "w": bool(changes["added"] or changes["changed"] or changes["removed"]),
             "r": json.dumps({**report, "changes": changes})})


def main() -> None:
    cfg = yaml.safe_load((ROOT / "config" / "ingest.yaml").read_text())["county"]
    path = ROOT / cfg["cama"]
    if not path.exists():
        raise FileNotFoundError(f"Missing {path}: download 'Tax Parcel CAMA Data' (CSV) from Open Mapping.")
    mtime = datetime.fromtimestamp(os.path.getmtime(path))
    as_of, source = mtime.date(), f"{path.relative_to(ROOT).as_posix()} (downloaded {mtime:%Y-%m-%d %H:%M})"
    print(f"As-of date {as_of}\n  {source}")
    accounts, report = build_accounts(read_cama(path))
    engine = get_engine()
    changes = load_table(accounts, "parcel_cama", ["account_pin"], engine, as_of)
    log_run(engine, as_of, source, report, changes)
    print(format_report(report, changes))


if __name__ == "__main__":
    main()
