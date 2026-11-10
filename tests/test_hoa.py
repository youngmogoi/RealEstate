"""Tests for features/hoa.py (likely HOA parcels) and the likely_hoa hold in scoring/route.py."""

import pandas as pd
import yaml

import scoring.route as T
from features.build import ROOT, owner_type
from features.hoa import flag_hoa

CFG = yaml.safe_load((ROOT / "config" / "features.yaml").read_text(encoding="utf-8"))
HOA = CFG["hoa"]


def test_hoa_owner_names():
    rules = CFG["owner_type_rules"]
    for name in ["VERMILLION COMMUNITY ASSOCIATION INC", "BAILEY`S GLEN MASTER HOMEOWNER`S ASSOCIATION INC",
                 "STONEYBROOK STATION HOMEOWNRS ASSOCIATION INC", "RIVER RUN PROPERTY OWNER`S ASSOCIATION INC",
                 "OLD STONE CROSSING AT CALDWELL CREEK P/O ASSOC INC", "KINGSTREE RESIDENTIAL ASSOC INC THE"]:
        assert owner_type(name, rules) == "hoa", name
    # companies and churches keep their types
    assert owner_type("DAVIS LAKE ASSOCIATES LLC", rules) == "llc_corporate"
    assert owner_type("HOA HOLDINGS LLC", rules) == "llc_corporate"
    assert owner_type("FIRST BAPTIST CHURCH", rules) == "church_nonprofit"


def _parcels():
    # plat M50-1: an HOA common area (A), two lots (B, C); plat M50-2 (next phase): lot D touches A in
    # the same subdivision, lot E touches A in another named subdivision; F: a builder-owned common
    # area on plat M60-5 with lot G; H: an old lot with no HOA anywhere
    return pd.DataFrame({
        "pin": list("ABCDEFGH"),
        "plat_ref": ["50-1", "50-1", "50-1", "50-2", "50-3", "60-5", "60-5", "20-9"],
        "plat_year": [None, 2004, 2005, 2007, 2007, None, 2019, 1962],
        "owner_type": ["hoa", "individual", "trust", "individual", "individual", "llc_corporate", "individual",
                       "individual"],
        "land_use": ["SINGLE FAMILY RESIDENTIAL - COMMON", "SINGLE FAMILY RESIDENTIAL", "SINGLE FAMILY RESIDENTIAL",
                     "SINGLE FAMILY RESIDENTIAL", "SINGLE FAMILY RESIDENTIAL", "COS NO AV10",
                     "SINGLE FAMILY RESIDENTIAL", "SINGLE FAMILY RESIDENTIAL"],
        "owner_name": ["HIGHLAND CREEK COMMUNITY ASSOCIATION INC", "SMITH JOHN", "DOE TRUST", "LEE ANN", "KIM BO",
                       "BIG BUILDER LLC", "PARK AL", "OLD OWNER"],
        "subdivision": ["HIGHLAND CREEK", "HIGHLAND CREEK", "HIGHLAND CREEK", "HIGHLAND CREEK", "MALLARD WOODS",
                        "MF - NORTHWEST SUBMARKET", "MF - NORTHWEST SUBMARKET", "MYERS PARK"]})


def test_flag_hoa_bases_and_evidence():
    touches = pd.DataFrame({"hoa_pin": ["A", "A", "F"], "pin": ["D", "E", "H"]})
    r = flag_hoa(_parcels(), touches, HOA).set_index("pin")
    assert r.loc["A", "hoa_basis"] == "hoa_land" and r.loc["A", "hoa_land"]
    assert (r.loc["B", "hoa_basis"], r.loc["B", "hoa_name"], r.loc["B", "hoa_pin"]) == (
        "same_plat", "HIGHLAND CREEK COMMUNITY ASSOCIATION INC", "A")
    assert r.loc["D", "hoa_basis"] == "touches_same_subdivision" and r.loc["D", "hoa_pin"] == "A"
    # touching HOA land in a different named subdivision is not enough
    assert not r.loc["E", "likely_hoa"]
    # a common-area land use counts as HOA land, with its owner as evidence
    assert (r.loc["G", "hoa_basis"], r.loc["G", "hoa_name"]) == ("same_plat", "BIG BUILDER LLC")
    # submarket buckets are not subdivisions: H touches F but shares no real name
    assert not r.loc["H", "likely_hoa"] and pd.isna(r.loc["F", "subdivision"])
    assert r.loc["H", "pre_1980_subdivision"] and not r.loc["B", "pre_1980_subdivision"]
    assert not r.loc["A", "pre_1980_subdivision"]          # no plat year: not flagged
    assert int(r["likely_hoa"].sum()) == 6


def test_hoa_owned_land_before_common_area_as_evidence():
    p = _parcels()
    p.loc[p["pin"] == "C", ["owner_type", "land_use", "owner_name"]] = ["llc_corporate", "TOWN HOUSE COMMON AREA",
                                                                        "AAA BUILDER LLC"]
    r = flag_hoa(p, pd.DataFrame({"hoa_pin": [], "pin": []}), HOA).set_index("pin")
    assert r.loc["B", "hoa_pin"] == "A"                     # the HOA's own parcel, not the builder's


def test_likely_hoa_never_builds():
    row = {"pin": "X", "action": "build", "reason": "build: cur_sfh", "likely_hoa": True, "hoa_basis": "same_plat",
           "hoa_name": "HIGHLAND CREEK COMMUNITY ASSOCIATION INC", "hoa_pin": "A"}
    held = T.hoa_hold(row, {"exclude_likely_hoa": True})
    assert (held["action"], held["held_action"]) == ("likely_hoa", "build")
    assert held["reason"] == ("likely HOA: same_plat (HIGHLAND CREEK COMMUNITY ASSOCIATION INC, PIN A); "
                              "would be build: cur_sfh")
    assert T.hoa_hold(row, {"exclude_likely_hoa": False}) == row
    assert T.hoa_hold(row | {"action": "route"}, {"exclude_likely_hoa": True})["action"] == "route"
    assert T.hoa_hold(row | {"likely_hoa": False}, {"exclude_likely_hoa": True})["action"] == "build"
