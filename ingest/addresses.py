"""Load the county's Master Address Table ("Master Address - Active/Temporary Status").

Run with `make addresses`. The file is named in config/ingest.yaml (county.addresses).

Table `parcel_addresses` (history in parcel_addresses_history): one row per address
(`address_id` = num_addr), with `pin` = num_parent_parcel, the parcel it sits on.

Notes on the file (checked 2026-10-28 against that day's download; its metadata defines no fields):
- 679,097 addresses on 422,384 parcels; 4,014 have no parcel. Status A (active) on all but 325.
- txt_cdeuse is the address use the county's property page shows ("Address Use: Utility").
- ind_occup is blank on every row. The "unoccupiable" the property page shows is txt_model
  ASSET: 25,875 addresses for things nobody occupies (meters, signs, bus stops, cable boxes,
  utility, cell towers, lift stations, also ADUs). Most sit beside a parcel's real addresses.
- 18924303 (Thermal Rd, 11 ac vacant) has one address: 1475 Patimby Rd, use UTILITY, model ASSET.
"""

import json
import os
from datetime import datetime

import pandas as pd
import yaml
from sqlalchemy import text

from ingest.common import strip_strings
from ingest.county_parcels import ROOT, load_table
from ingest.db import get_engine

FIELDS = {
    "num_addr": "address_id",
    "num_parent_parcel": "pin",
    "full_address": "full_address",
    "cde_use": "use_code",
    "txt_cdeuse": "address_use",
    "txt_model": "address_model",
    "cde_status": "status",
    "num_units": "units",
    "dte_upd": "updated_on",
}


def read_addresses(path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str, encoding="utf-8-sig", usecols=list(FIELDS), keep_default_na=False, na_values=[""])
    df = strip_strings(df.rename(columns=FIELDS))[list(FIELDS.values())]
    df["units"] = pd.to_numeric(df["units"]).round().astype("Int64")
    df["updated_on"] = pd.to_datetime(df["updated_on"], errors="coerce")
    return df.sort_values("address_id").reset_index(drop=True)


def report(df: pd.DataFrame) -> dict:
    asset = df["address_model"] == "ASSET"
    return {"addresses": len(df), "parcels": int(df["pin"].nunique()), "without_parcel": int(df["pin"].isna().sum()),
            "duplicate_ids": int(df["address_id"].duplicated().sum()), "asset_addresses": int(asset.sum()),
            "status": {k: int(v) for k, v in df["status"].value_counts().items()}}


def main() -> None:
    cfg = yaml.safe_load((ROOT / "config" / "ingest.yaml").read_text())["county"]
    path = ROOT / cfg["addresses"]
    if not path.exists():
        raise FileNotFoundError(f"Missing {path}: download 'Master Address - Active/Temporary Status' (CSV) from Open Mapping.")
    mtime = datetime.fromtimestamp(os.path.getmtime(path))
    as_of, source = mtime.date(), f"{path.relative_to(ROOT).as_posix()} (downloaded {mtime:%Y-%m-%d %H:%M})"
    print(f"As-of date {as_of}\n  {source}")
    df = read_addresses(path)
    r = report(df)
    if r["duplicate_ids"]:
        raise ValueError(f"{r['duplicate_ids']} repeated address ids in {path}")
    engine = get_engine()
    changes = load_table(df, "parcel_addresses", ["address_id"], engine, as_of)
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO ingest_runs (source, snapshot_date, source_file, snapshot_written, report) "
                 "VALUES ('county_addresses', :d, :f, :w, CAST(:r AS jsonb))"),
            {"d": as_of, "f": source, "w": bool(changes["added"] or changes["changed"] or changes["removed"]),
             "r": json.dumps({**r, "changes": changes})})
    print(f"Master addresses: {r}\n  parcel_addresses: {changes}")


if __name__ == "__main__":
    main()
