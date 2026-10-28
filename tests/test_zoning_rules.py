"""Tests for scoring/zoning_rules.py: base-district parsing on real zone codes from the
2026-10 zoning layers, and the coverage check."""

import pytest

from scoring.zoning_rules import check_rule, load_rules, parse_code, validate

RULES = load_rules()
P = RULES["code_parsing"]


@pytest.mark.parametrize("j, code, base, kinds", [
    ("CHARLOTTE", "N1-A", "N1-A", []),
    ("CHARLOTTE", "R-3(CD)(ANDO)", "R-3", ["site_plan", "overlay"]),
    ("CHARLOTTE", "R-17MF(CD)ANDO", "R-17MF", ["site_plan", "overlay"]),  # no parentheses on ANDO
    ("CHARLOTTE", "N2-B(CD0", "N2-B", ["site_plan"]),                     # typo in the layer
    ("CHARLOTTE", "N2-A ", "N2-A", []),                                   # trailing space
    ("CHARLOTTE", "MX-2(INNOV) SPA", "MX-2", ["site_plan", "site_plan"]),
    ("CHARLOTTE", "NC(EX) HDO", "NC", ["site_plan", "overlay"]),
    ("CHARLOTTE", "TOD-M(O)", "TOD-M", ["site_plan"]),
    ("CHARLOTTE", "MUDD-O", "MUDD-O", []),                                # MUDD-O is its own district
    ("CHARLOTTE", "B-1SCD", "B-1SCD", []),                                # shopping center, not (CD)
    ("CHARLOTTE", "RR-CD", "RR", ["site_plan"]),                          # alias
    ("CHARLOTTE", "TOC-NC", "TOD-NC", []),                                # alias
    ("MATTHEWS", "B-1(CD)(DO)(HO", "B-1", ["site_plan", "overlay", "overlay"]),  # truncated
    ("MATTHEWS", "O(CD)(DO)", "O", ["site_plan", "overlay"]),             # district "O" survives
    ("MINT HILL", "O-A DO-A", "O-A", ["overlay"]),
    ("HUNTERSVILLE", "NR(TND-O)", "NR", ["overlay"]),
    ("DAVIDSON", "VCO", "VCO", []),
])
def test_parse_code(j, code, base, kinds):
    b, mods = parse_code(j, code, P)
    assert b == base and [k for _, k in mods] == kinds


SRC = {"source": "UDO 1.1"}
TYPES = {"sfh": {"allowed": True, "units_per_building": 1, **SRC},          # bare yes in YAML
         "duplex": {"allowed": "conditional", "units_per_building": 2, **SRC},
         "triplex": {"allowed": "conditional", "units_per_building": 3, **SRC},
         "quadraplex": {"allowed": "conditional", "units_per_building": 4, "requires": {"arterial_frontage": True}, **SRC},
         "townhome": {"allowed": False, "units_per_building": 0, **SRC},
         "adu": {"allowed": "conditional", "units_per_building": 1, **SRC}}
BY_RIGHT = {"status": "confirmed", "housing": "by_right", "min_lot_sf": {"value": 8000, **SRC},
            "min_lot_width_ft": {"value": 60, **SRC}, "setbacks_ft": {"front": 30, **SRC},
            "density": {"max_upa": None, **SRC}, "types": TYPES}


def test_check_rule():
    assert check_rule(BY_RIGHT) == []
    no_triplex = {k: v for k, v in TYPES.items() if k != "triplex"}
    assert check_rule({**BY_RIGHT, "types": no_triplex}) == ["types.triplex missing"]
    assert check_rule({**BY_RIGHT, "types": {**TYPES, "duplex": {"allowed": "conditional", **SRC}}}) == [
        "types.duplex has no units_per_building or max_building_length_ft"]
    by_length = {"allowed": "conditional", "units_per_building": None, "requires": {"max_building_length_ft": 150}, **SRC}
    assert check_rule({**BY_RIGHT, "types": {**TYPES, "townhome": by_length}}) == []
    assert check_rule({**BY_RIGHT, "types": {**TYPES, "townhome": {"allowed": "maybe", **SRC}}}) == [
        "types.townhome.allowed must be one of ['conditional', 'no', 'yes']"]
    assert check_rule({**BY_RIGHT, "setbacks_ft": {"front": "NEEDS_REVIEW", **SRC}}) == [
        "NEEDS_REVIEW: setbacks_ft.front"]
    assert check_rule(None) == ["no rule"]
    assert check_rule({**BY_RIGHT, "status": "draft"}) == ["status is 'draft', not confirmed"]
    assert check_rule({**BY_RIGHT, "min_lot_sf": {"value": 8000}}) == ["min_lot_sf has no source"]
    assert check_rule({"status": "confirmed", "housing": "none", "source": "UDO 2.2"}) == []
    assert check_rule({"status": "confirmed", "housing": "site_plan"}) == ["no source"]


def test_validate_reports_districts_without_rules():
    rules = {"code_parsing": P, "districts": {"CHARLOTTE": {"N1-A": BY_RIGHT}}}
    used = {("CHARLOTTE", "N1-A"): 100, ("CHARLOTTE", "N1-A(HDO)"): 5, ("CHARLOTTE", "R-4(CD)"): 7,
            ("CHARLOTTE", "R-4(CD)(ANDO)"): 3}
    assert validate(rules, used) == [("CHARLOTTE", "R-4", 10, ["no rule"])]
    rules["districts"]["CHARLOTTE"]["R-4"] = {"status": "confirmed", "housing": "site_plan", "source": "prior ord."}
    assert validate(rules, used) == []


def test_validate_needs_rezoning_target():
    townhome = {**TYPES["townhome"], "rezoning": {"needs_rezoning": True, "target": "by_place_type"}}
    rules = {"code_parsing": P, "districts": {"CHARLOTTE": {"N1-A": {**BY_RIGHT, "types": {**TYPES, "townhome": townhome}}}}}
    used = {("CHARLOTTE", "N1-A"): 100}
    assert validate(rules, used) == [("CHARLOTTE", "N1-A", 100, ["types.townhome: no rezoning_targets.CHARLOTTE.townhome"])]
    rules["rezoning_targets"] = {"CHARLOTTE": {"townhome": {"by_place_type": {"Neighborhood 2": "N2-A"}}}}
    assert validate(rules, used) == []
