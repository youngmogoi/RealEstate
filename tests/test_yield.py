"""Tests for scoring/yield.py on six real parcels (inputs in tests/fixtures/yield/parcels.csv,
exported from parcel_features 2026-10-28; their buildable pieces in pieces.csv, from
features/pieces.py: only 21311502 has any). Expected values are worked by hand below.

Settings used (config/scoring.yaml, draft): usable_ratio 0.70; townhome unit 22 x 40 ft, so a
150 ft building holds 6 units and a unit's footprint is 880 sf. 1 acre = 43,560 sf.
"""

import importlib
import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
import yaml
from sqlalchemy import text

from ingest.db import get_engine
from scoring.zoning_rules import load_rules

Y = importlib.import_module("scoring.yield")
ROOT = Path(__file__).resolve().parent.parent
CFG = yaml.safe_load((ROOT / "config" / "scoring.yaml").read_text(encoding="utf-8"))
RULES = load_rules()
FIXTURE = Path(__file__).parent / "fixtures" / "yield" / "parcels.csv"
PIECES = Path(__file__).parent / "fixtures" / "yield" / "pieces.csv"


def _pieces() -> pd.DataFrame:
    df = pd.read_csv(PIECES, dtype={"pin": str})
    for c in ("touches_street", "touches_same_owner", "creek"):
        df[c] = df[c].map({"t": True, "f": False})
    return df[Y.PIECE_COLS]


def _parcels() -> dict[str, dict]:
    df = pd.read_csv(FIXTURE, dtype={"pin": str})
    for c in ("split_zoning", "acres_mismatch", "through_lot", "arterial_frontage", "vacant"):
        df[c] = df[c].map({"t": True, "f": False})
    pieces = {pin: g.drop(columns="pin").to_dict("records") for pin, g in _pieces().groupby("pin")}
    return {r["pin"]: {k: (None if pd.isna(v) else v) for k, v in r.items()} | {"pieces": pieces.get(r["pin"])}
            for r in df.to_dict("records")}


P = _parcels()


def y(pin):
    return Y.compute_yield(P[pin], RULES, CFG)


def test_settings_used_by_these_hand_calculations():
    assert CFG["yield"]["usable_ratio"] == 0.70
    assert (CFG["townhome"]["unit_width_ft"], CFG["townhome"]["unit_depth_ft"]) == (22, 40)


def test_undersized_n1a_lot_02522236():
    # 9630 Nettle Dr, N1-A: 0.18593 ac = 8,099 sf < 10,000; width 64.9 < 70: a nonconforming lot
    # (county record: 1 LOT, house built 1996). UDO 38.8.A: usable for any permitted type if the
    # building meets the setbacks: envelope 64.9 - 2 x 5 = 54.9 ft wide, 129.5 - 27 - 40 = 62.5 ft
    # deep, both >= 20 ft. Quadraplex still needs an arterial.
    r = y("02522236")
    assert [r[f"cur_{t}"] for t in Y.TYPES] == [1, 2, 3, 0]
    assert r["nonconforming_lot"] and not r["combine_required"]
    assert [r[f"split_{t}"] for t in Y.TYPES] == [0, 0, 0, 0]   # new lots must conform
    assert r["split_lots"] == 0 and r["sub_lots"] == 0          # 8,099 x 0.70 / 10,000 = 0.57
    # Townhome needs rezoning; Neighborhood 1 place type -> N1-F. Site = 8,099 x 0.70 = 5,669 sf;
    # coverage 50% (under 10,000 sf): 5,669 x 0.5 / 880 = 3.2 -> 3; open space 250/unit:
    # 5,669 / 1,130 = 5.0 -> 5; 2 buildings x 6 units per 3,000 sf lot: 1 lot -> 12. Least: 3.
    assert (r["townhome_units"], r["townhome_needs_rezoning"], r["townhome_target_district"]) == (3, True, "N1-F")
    assert r["townhome_speculative"]
    assert r["adu_allowed"]                                       # accessory to the house it may have
    assert r["setback_varies"] and not r["reduced_depth_possible"] and not r["multi_dwelling_possible"]
    assert not r["no_frontage"] and r["obligations"] is None


def test_conforming_n1a_lot_02708315():
    # 12607 Cardinal Point Rd, N1-A: 12,141 sf >= 10,000; width 80.2 >= 70; local street
    r = y("02708315")
    assert [r[f"cur_{t}"] for t in Y.TYPES] == [1, 2, 3, 0]     # quadraplex needs an arterial
    assert r["split_lots"] == 1                                   # 12,141/10,000 = 1; 80.2/70 = 1
    assert [r[f"split_{t}"] for t in Y.TYPES] == [1, 2, 3, 0]
    assert r["sub_lots"] == 0                                     # 12,141 x 0.70 / 10,000 = 0.85
    # N1-F rezoning: site 8,499 sf; coverage 50%: 8,499 x 0.5 / 880 = 4.8 -> 4; open space 7; lots 2 -> 24
    assert (r["townhome_units"], r["townhome_target_district"]) == (4, "N1-F")
    assert r["adu_allowed"] and r["setback_varies"] and not r["multi_dwelling_possible"]


def test_n1a_lot_on_arterial_02763166():
    # 5128 Hucks Rd (major thoroughfare), N1-A: 15,414 sf; width 104.3
    r = y("02763166")
    assert [r[f"cur_{t}"] for t in Y.TYPES] == [1, 2, 3, 4]
    assert r["split_lots"] == 1 and [r[f"split_{t}"] for t in Y.TYPES] == [1, 2, 3, 4]   # 104.3/70 = 1
    assert json.loads(r["obligations"]) == {"quadraplex": {"affordable_units_per_building": 1, "ami_pct": 80, "years": 15}}
    assert r["sub_lots"] == 1                                     # 15,414 x 0.70 / 10,000 = 1.08
    # N1-F rezoning: site 10,789 sf (>= 10,000: coverage 40%): 10,789 x 0.4 / 880 = 4.9 -> 4
    assert r["townhome_units"] == 4
    assert r["reduced_depth_possible"] and r["arterial_frontage"] and r["setback_varies"]
    assert not r["multi_dwelling_possible"]                       # 15,414 < 2 x 10,000


def test_n1b_lot_split_on_arterial_03508109():
    # 6124 Old Plank Rd (thoroughfare), N1-B: 19,228 sf; width 139.1
    r = y("03508109")
    assert [r[f"cur_{t}"] for t in Y.TYPES] == [1, 2, 3, 4]
    # split: 19,228 / 8,000 = 2.4 -> 2; 139.1 / 60 = 2.3 -> 2 lots
    assert r["split_lots"] == 2 and [r[f"split_{t}"] for t in Y.TYPES] == [2, 4, 6, 8]
    assert r["sub_lots"] == 1                                     # 19,228 x 0.70 / 8,000 = 1.68
    assert r["sub_conservation_lots"] is None and r["sub_compact_lots"] is None   # 0.44 ac: too small
    assert r["sub_mixed_income_lots"] is None                     # needs 1 acre
    # N1-F rezoning: site 13,460 sf (coverage 40%): 13,460 x 0.4 / 880 = 6.1 -> 6; open space 11
    assert r["townhome_units"] == 6
    assert r["multi_dwelling_possible"]                           # 19,228 >= 2 x 8,000
    assert r["reduced_depth_possible"] and r["adu_allowed"]


def test_n1a_tract_options_21311502():
    # 8625 Sardis Rd (thoroughfare), N1-A, vacant: 6.0154 ac = 262,032 sf gross; width 263.4,
    # frontage 147.1. Sardis Branch crosses it ~1,000 ft back (pieces.csv): front piece 134,808 sf
    # less 31,981 sf over 25% slope = 102,827 sf net, on Sardis Rd; back piece 44,634 - 2,205 =
    # 42,429 sf net, reaching no street and no same-owner land, >= 10,000 sf: cut off.
    # Confirmed 2026-10-28. Usable (front only) = 102,827 x 0.70 = 71,979 sf.
    r = y("21311502")
    assert not r["verify_acreage"]                                # assessor 5.98 ac: within 20%
    assert [r[f"cur_{t}"] for t in Y.TYPES] == [1, 2, 3, 4]
    # split: 262,032 / 10,000 = 26; smaller of frontage 147.1 and width 263.4: 147.1 / 70 = 2.1 -> 2
    assert r["split_lots"] == 2 and [r[f"split_{t}"] for t in Y.TYPES] == [2, 4, 6, 8]
    assert r["net_sf"] == 102826.9 and r["cutoff_acres"] == pytest.approx(42428.9 / 43560, abs=1e-4)
    assert r["creek_divides"] and not r["constraint_divides"] and not r["assemblage_access"]
    assert r["sub_lots"] == 7                                     # 71,979 / 10,000 = 7.2
    # conservation (>= 5 ac): least of gross 262,032 / 10,000 = 26 and usable 71,979 / 5,000 = 14.4
    assert r["sub_conservation_lots"] == 14
    # compact (>= 2 ac): 71,979 x 0.90 / 5,000 = 12.96 -> 12
    assert r["sub_compact_lots"] == 12
    # mixed income as N1-C: 71,979 / 6,000 = 11.996 -> 11 lots; bonus 11 - 7 = 4; affordable max(2, 2) = 2
    assert (r["sub_mixed_income_lots"], r["sub_mixed_income_affordable_units"]) == (11, 2)
    ob = json.loads(r["obligations"])
    assert ob["mixed_income"]["bonus_lots"] == 4 and ob["mixed_income"]["affordable_units"] == 2
    assert ob["conservation"]["conservation_area_pct"] == 40 and ob["compact"]["common_open_space_pct"] == 10
    # Townhome is not allowed in N1-A: this is the speculative N1-F rezoning yield (Neighborhood 1).
    # coverage 40%: 71,979 x 0.4 / 880 = 32.7 -> 32; open space 71,979 / 1,130 = 63
    assert (r["townhome_units"], r["townhome_needs_rezoning"], r["townhome_target_district"]) == (32, True, "N1-F")
    assert r["townhome_speculative"]
    # If the creek were crossed: 102,826.9 + 42,428.9 = 145,255.8 sf net; usable 101,679
    assert r["if_crossed_net_sf"] == 145255.8
    assert [r[f"if_crossed_{c}"] for c in Y.NET_OPTIONS] == [10, 20, 18, 16, 46]
    assert r["multi_dwelling_possible"] and r["reduced_depth_possible"] and r["setback_varies"]
    assert "compact: check airport 65 DNL" in r["notes"]


def test_pieces_below_min_lot_get_no_yield_21311502():
    # Raise the minimum lot past the back piece (42,429 sf): it is no longer cut off, and with the
    # land in 2 pieces neither piece below the minimum gets yield. 50,000 sf: front 102,827 counts.
    pieces = P["21311502"]["pieces"]
    land = Y.piece_land(pieces, 50_000, True)
    assert land == {"reachable_sf": 102826.9, "cutoff_sf": 0, "creek": True}
    # 110,000 sf: neither piece counts
    assert Y.piece_land(pieces, 110_000, True)["reachable_sf"] == 0
    # no frontage and nothing reaches it: not divided, every counted piece stays (no_frontage)
    unreached = [x | {"touches_street": False} for x in pieces]
    assert Y.piece_land(unreached, 10_000, False) == {"reachable_sf": 145255.8, "cutoff_sf": 0, "creek": True}


def test_parcels_without_pieces_keep_their_own_net():
    for pin in ("02522236", "02708315", "02763166", "02918135", "03508109"):
        r = y(pin)
        assert r["net_sf"] == round(min(P[pin]["net_buildable_acres"] * 43_560, r["area_sf"]), 1)
        assert r["cutoff_acres"] is None and r["if_crossed_sub_lots"] is None and not r["creek_divides"]


def test_n2b_townhome_by_right_02918135():
    # 8435 Portsmouth Dr, N2-B: 43,554 sf gross; net 41,995 sf; width 208.6
    r = y("02918135")
    # SF to quadraplex built to N1-E (3,000 sf, 30 ft); no arterial condition outside N1
    assert [r[f"cur_{t}"] for t in Y.TYPES] == [1, 2, 3, 4]
    # split at N1-E: 43,554 / 3,000 = 14; 208.6 / 30 = 6.95 -> 6 lots
    assert r["split_lots"] == 6 and [r[f"split_{t}"] for t in Y.TYPES] == [6, 12, 18, 24]
    assert r["sub_lots"] == 9                                     # 41,995 x 0.70 / 3,000 = 9.8
    # townhome by right: site 29,396 sf; coverage 60%: 29,396 x 0.6 / 880 = 20.04 -> 20;
    # open space 150/unit: 29,396 / 1,030 = 28; 3,000 sf per building: 9 x 6 = 54. Least: 20.
    assert (r["townhome_units"], r["townhome_needs_rezoning"], r["townhome_target_district"]) == (20, False, None)
    assert r["apartments_possible"] and r["multi_dwelling_possible"] and r["adu_allowed"]
    assert not r["setback_varies"] and not r["reduced_depth_possible"]
    assert r["rule_status"] == "draft"                            # N2-B awaits your PDF check


def test_nonconforming_sliver_and_combinable_vacant_lot():
    base = dict(P["02522236"])
    # a 25 ft wide strip: 25 - 2 x 5 = 15 ft < 20 ft envelope -> no house fits
    r = Y.compute_yield({**base, "lot_width_ft": 25.0}, RULES, CFG)
    assert [r[f"cur_{t}"] for t in Y.TYPES] == [0, 0, 0, 0] and not r["nonconforming_lot"]
    # vacant, and the same owner's adjoining vacant land (2,500 sf) makes 8,099 + 2,500 >= 10,000
    r = Y.compute_yield({**base, "vacant": True, "adjoining_vacant_sf": 2500.0}, RULES, CFG)
    assert [r[f"cur_{t}"] for t in Y.TYPES] == [0, 0, 0, 0] and r["combine_required"] and r["nonconforming_lot"]
    # vacant but the neighbor is too small to reach 10,000 sf together: stands alone
    r = Y.compute_yield({**base, "vacant": True, "adjoining_vacant_sf": 1000.0}, RULES, CFG)
    assert r["cur_sfh"] == 1 and not r["combine_required"]


def test_site_plan_and_unsupported_get_no_yield():
    base = dict(P["02708315"])
    for code, flag in [("MX-2", "site_plan_governs"), ("R-4(CD)", "site_plan_governs"),
                       ("R-3", "no_zoning_rule"), ("I-1", "no_zoning_rule")]:
        r = Y.compute_yield({**base, "zone_code": code}, RULES, CFG)
        assert r[flag] and r["yield_basis"] == flag and r["cur_sfh"] is None and r["townhome_units"] is None


def test_store_in_parcel_yield():
    s = "test_yield"
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {s} CASCADE; CREATE SCHEMA {s}"))
    try:
        df = Y.build_frame(pd.DataFrame([{k: v for k, v in p.items() if k != "pieces"} for p in P.values()]),
                           _pieces(), RULES, CFG)
        assert Y.store(df, engine, date(2026, 10, 28), s)["added"] == 6
        assert Y.store(df, engine, date(2026, 11, 4), s) == {"added": 0, "changed": 0, "removed": 0, "current": 6}
        with engine.connect() as conn:
            row = conn.execute(text(f"SELECT cur_triplex, townhome_units, obligations->'quadraplex'->>'ami_pct' "
                                    f"FROM {s}.parcel_yield WHERE pin = '02763166'")).one()
        assert row == (3, 4, "80")
    finally:
        with engine.begin() as conn:
            conn.execute(text(f"DROP SCHEMA {s} CASCADE"))
