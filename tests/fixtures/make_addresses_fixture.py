"""Rebuild tests/fixtures/meck_addresses/addresses.csv from the county Master Address Table.

Run: PYTHONPATH=. uv run python tests/fixtures/make_addresses_fixture.py

Source: the file named in config/ingest.yaml (county.addresses), downloaded 2026-10-28. Rows are
copied unchanged for the parcels below.
"""

from pathlib import Path

import pandas as pd
import yaml

from ingest.county_parcels import ROOT

OUT = Path(__file__).with_name("meck_addresses")

PINS = {
    "18924303": "Thermal Rd, 11 ac vacant: one address, UTILITY (ASSET) -> utility site",
    "02708315": "12607 Cardinal Point Rd: a house address only",
    "18903439": "6109 Wheeler Dr, Rama Swim Club Inc: one CLUB-LODGE address on single-family land use -> club",
    "16307180": "1713 Rama Rd, a person's house: OTHER INSTITUTIONAL address only (stale) -> flagged",
    "04922122": "8616 Knollwood Cr: a house address and a POOL address -> flagged",
    "13321114": "7619 Idlewild Rd, New Fellowship Worship Center: use code 71 (blank name, churches) -> church",
}
# plus the first parcel (by pin) with both a single-family and a UTILITY address
MIXED = "first parcel with SINGLE FAMILY RESIDENTIAL and UTILITY addresses"

if __name__ == "__main__":
    cfg = yaml.safe_load((ROOT / "config" / "ingest.yaml").read_text())["county"]
    df = pd.read_csv(ROOT / cfg["addresses"], dtype=str, encoding="utf-8-sig", keep_default_na=False)
    uses = df.groupby("num_parent_parcel")["txt_cdeuse"].agg(set)
    mixed = sorted(p for p, u in uses.items() if p and {"SINGLE FAMILY RESIDENTIAL", "UTILITY"} <= u)[0]
    OUT.mkdir(exist_ok=True)
    rows = df[df["num_parent_parcel"].isin([*PINS, mixed])]
    rows.to_csv(OUT / "addresses.csv", index=False)
    print(f"Wrote {len(rows)} rows ({mixed} is the mixed parcel) to {OUT}")
