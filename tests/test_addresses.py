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
    occ = a[a["address_model"] != "ASSET"]
    occ_uses = occ["address_use"].where(occ["address_use"].notna(), "USE CODE " + occ["use_code"])
    return {"addresses": len(a), "occupiable_addresses": len(occ),
            "asset_address_uses": "; ".join(sorted(set(assets))) or None,
            "occupiable_address_uses": "; ".join(sorted(set(occ_uses))) or None}


def test_read_addresses():
    df = read_addresses(FIXTURE)
    assert report(df) == {"addresses": 12, "parcels": 7, "without_parcel": 0, "duplicate_ids": 0,
                          "asset_addresses": 5, "status": {"A": 12}}
    a = df[df["address_id"] == "707167"].iloc[0]
    assert (a["pin"], a["address_use"], a["address_model"], a["full_address"]) == (
        "18924303", "UTILITY", "ASSET", "1475 PATIMBY RD CHARLOTTE NC 28211")


def test_utility_site_is_not_scored():
    # 18924303's only county address is UTILITY / ASSET (unoccupiable): a utility site. Shown here on
    # 02708315's yield, which is otherwise scored.
    s = summary(read_addresses(FIXTURE), "18924303")
    assert s == {"addresses": 1, "occupiable_addresses": 0, "asset_address_uses": "UTILITY",
                 "occupiable_address_uses": None}
    row, _ = run("02708315", **s)
    assert (row["exclusion_category"], row["score"], row["utility_address"]) == ("utility_site", None, True)
    assert row["best_rlv"] is not None


def test_utility_address_beside_a_house_is_only_flagged():
    # 00101108 (Hagers Ferry Rd): a house, plus UTILITY and CATV asset addresses -> flagged, scored
    s = summary(read_addresses(FIXTURE), "00101108")
    assert s == {"addresses": 3, "occupiable_addresses": 1, "asset_address_uses": "CATV; UTILITY",
                 "occupiable_address_uses": "SINGLE FAMILY RESIDENTIAL"}
    row, _ = run("02708315", **s)
    assert row["exclusion_category"] is None and row["score"] is not None and row["utility_address"]
    # a house address only: nothing
    row, _ = run("02708315", **summary(read_addresses(FIXTURE), "02708315"))
    assert not row["utility_address"] and row["exclusion_category"] is None
    # no county address at all is not a utility site
    row, _ = run("02708315", addresses=0, occupiable_addresses=0, asset_address_uses=None)
    assert row["exclusion_category"] is None


# Owners and land uses below are the county's (parcel_owners, parcel_features) on 2026-10-28; each is
# applied to 02708315's yield, which is otherwise scored.
SFR, SFR_ACRE = "SINGLE FAMILY RESIDENTIAL", "SINGLE FAMILY RESIDENTIAL - ACREAGE"


def test_club_address_only_is_excluded_6109_wheeler_dr():
    # 18903439: land use says single-family acreage, but its one occupiable address is CLUB-LODGE
    # and the owner is RAMA SWIM CLUB INC -> club, on the redevelopment watch list
    s = summary(read_addresses(FIXTURE), "18903439")
    assert s["occupiable_address_uses"] == "CLUB-LODGE" and s["asset_address_uses"] == "METER"
    row, _ = run("02708315", **s, land_use=SFR_ACRE, owner_type="llc_corporate", owner_name="RAMA SWIM CLUB INC")
    assert (row["exclusion_category"], row["score"], row["redevelopment_watch"]) == ("club", None, True)
    assert not row["institutional_address"]


def test_church_use_code_71_is_excluded():
    # 13321114 (7619 Idlewild Rd): use code 71 has no description; owner NEW FELLOWSHIP WORSHIP CENTER
    # is "individual" by owner type, but WORSHIP is an institution word
    s = summary(read_addresses(FIXTURE), "13321114")
    assert s["occupiable_address_uses"] == "USE CODE 71"
    row, _ = run("02708315", **s, land_use=SFR, owner_type="individual", owner_name="NEW FELLOWSHIP WORSHIP CENTER")
    assert row["exclusion_category"] == "church_nonprofit" and row["score"] is None


def test_stale_institutional_address_on_a_house_is_only_flagged():
    # 16307180 (1713 Rama Rd): a new house owned by a person; its address still says OTHER INSTITUTIONAL
    s = summary(read_addresses(FIXTURE), "16307180")
    assert s["occupiable_address_uses"] == "OTHER INSTITUTIONAL"
    row, _ = run("02708315", **s, land_use=SFR, owner_type="individual", owner_name="HEINLY QUINTEN MARC")
    assert row["exclusion_category"] is None and row["score"] is not None and row["institutional_address"]
    # the same address on a non-residential land use is excluded whoever owns it
    row, _ = run("02708315", **s, land_use="COMMERCIAL", owner_type="individual", owner_name="HEINLY QUINTEN MARC")
    assert row["exclusion_category"] == "institutional_other"


def test_institutional_address_beside_a_house_is_only_flagged():
    # 04922122 (8616 Knollwood Cr): a house address and a POOL address
    s = summary(read_addresses(FIXTURE), "04922122")
    assert s["occupiable_address_uses"] == "POOL; SINGLE FAMILY RESIDENTIAL"
    row, _ = run("02708315", **s, land_use=SFR, owner_type="individual", owner_name="RAINEY-GLOVER TAMARA D")
    assert row["exclusion_category"] is None and row["institutional_address"]
    # a house address only: no flag
    row, _ = run("02708315", **summary(read_addresses(FIXTURE), "02708315"))
    assert not row["institutional_address"]
