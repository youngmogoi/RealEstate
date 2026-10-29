"""Tests for ingest/addresses.py and the address rules in scoring/rlv.py, on real Master Address
rows (tests/fixtures/meck_addresses/addresses.csv, from the county download of 2026-10-28)."""

from pathlib import Path

import scoring.rlv as R
from ingest.addresses import read_addresses, report
from tests.test_rlv import ECON, run

FIXTURE = Path(__file__).parent / "fixtures" / "meck_addresses" / "addresses.csv"


def summary(df, pin):
    """The per-parcel address summary run_rlv reads (INPUT_SQL), from the fixture rows."""
    a = df[df["pin"] == pin]
    assets = a.loc[a["address_model"] == "ASSET", "address_use"]
    return {"addresses": len(a), "occupiable_addresses": int((a["address_model"] != "ASSET").sum()),
            "asset_address_uses": "; ".join(sorted(set(assets))) or None}


def test_read_addresses():
    df = read_addresses(FIXTURE)
    assert report(df) == {"addresses": 5, "parcels": 3, "without_parcel": 0, "duplicate_ids": 0,
                          "asset_addresses": 3, "status": {"A": 5}}
    a = df[df["address_id"] == "707167"].iloc[0]
    assert (a["pin"], a["address_use"], a["address_model"], a["full_address"]) == (
        "18924303", "UTILITY", "ASSET", "1475 PATIMBY RD CHARLOTTE NC 28211")


def test_utility_site_is_not_scored():
    # 18924303's only county address is UTILITY / ASSET (unoccupiable): a utility site. Shown here on
    # 02708315's yield, which is otherwise scored.
    s = summary(read_addresses(FIXTURE), "18924303")
    assert s == {"addresses": 1, "occupiable_addresses": 0, "asset_address_uses": "UTILITY"}
    row, _ = run("02708315", **s)
    assert (row["exclusion_category"], row["score"], row["utility_address"]) == ("utility_site", None, True)
    assert row["best_rlv"] is not None


def test_utility_address_beside_a_house_is_only_flagged():
    # 00101108 (Hagers Ferry Rd): a house, plus UTILITY and CATV asset addresses -> flagged, scored
    s = summary(read_addresses(FIXTURE), "00101108")
    assert s == {"addresses": 3, "occupiable_addresses": 1, "asset_address_uses": "CATV; UTILITY"}
    row, _ = run("02708315", **s)
    assert row["exclusion_category"] is None and row["score"] is not None and row["utility_address"]
    # a house address only: nothing
    row, _ = run("02708315", **summary(read_addresses(FIXTURE), "02708315"))
    assert not row["utility_address"] and row["exclusion_category"] is None
    # no county address at all is not a utility site
    row, _ = run("02708315", addresses=0, occupiable_addresses=0, asset_address_uses=None)
    assert row["exclusion_category"] is None
