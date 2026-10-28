"""Tests for scoring/rlv.py on the yield test parcels (tests/fixtures/yield/), with their RLV
inputs from parcel_features / parcel_constraints in tests/fixtures/rlv/parcels.csv (exported
2026-10-28). Comps and costs are fixed test values (below), not the county's or config's, so the
hand calculations don't move when you edit config/economics.yaml.

Per house (sfh comps $250/sf, cap 3,000 sf): revenue 3,000 x 250 = 750,000; build 3,000 x 150 =
450,000; soft 10% = 45,000; profit 15% of revenue = 112,500; margin 142,500.
Per townhome (comps $230/sf, cap 2,200 sf): 506,000 - 330,000 - 33,000 - 75,900 = 67,100.
Per plex building ($220/sf, cap 3,200 sf): 704,000 - 480,000 - 48,000 - 105,600 = 70,400.
Floors: max height 48 or 40 ft / 12 ft = 4 or 3, at most 3.
"""

import copy
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import text

import scoring.rlv as R
from ingest.db import get_engine
from tests.test_yield import CFG, P, RULES, Y

FEATURES = Path(__file__).parent / "fixtures" / "rlv" / "parcels.csv"
ECON = copy.deepcopy(R.load_config()[0]) | {
    "costs": {"build_cost_per_sf": 150, "soft_cost_pct": 10, "target_profit_pct": 15},
    "site_work": {"sewer_per_ft": 100, "road_per_ft": 200, "stormwater_per_unit": 5000, "slope_per_acre": 20000,
                  "floodplain_per_unit": 15000, "creek_crossing": 250000, "road_and_stormwater_on_lot_as_is": False},
    "obligations": {"affordable_max_price": 250000},
    "adu_premium": 40000,
}
ECON["house_size"] = {"story_height_ft": 12, "max_stories": 3, "market_cap_percentile": 0.9}
ECON["sewer"] = {"septic_min_lot_sf": 43560, "low_confidence_score_cap": 100000}
COMPS = {
    "sfh": {"radius_mi": 1, "comp_count": 12, "median_price_per_sf": 250.0, "size_cap_sf": 3000.0, "thin_comps": False},
    "townhome": {"radius_mi": 2, "comp_count": 8, "median_price_per_sf": 230.0, "size_cap_sf": 2200.0, "thin_comps": False},
    "plex": {"radius_mi": 3, "comp_count": 3, "median_price_per_sf": 220.0, "size_cap_sf": 3200.0, "thin_comps": True},
}
# 25 Charlotte improved sales at 1.2 and 3 vacant ones at 2.0: vacant groups are too small (< 20),
# so vacant parcels fall back to countywide all (28 sales, median 1.2).
RATIOS = R.ratio_table(pd.DataFrame({"municipality": ["CHARLOTTE"] * 28, "land_class": ["improved"] * 25 + ["vacant"] * 3,
                                     "ratio": [1.2] * 25 + [2.0] * 3}), 20)


def _features() -> dict[str, dict]:
    df = pd.read_csv(FEATURES, dtype={"pin": str})
    for c in ("in_municipal_limit", "vacant", "access_via_common_area"):
        df[c] = df[c].map({"t": True, "f": False})
    return {r["pin"]: {k: (None if pd.isna(v) else v) for k, v in r.items()} for r in df.to_dict("records")}


F = _features()


def parcel(pin: str, **override) -> dict:
    return Y.compute_yield(P[pin], RULES, CFG) | F[pin] | {"lot_depth_ft": P[pin]["lot_depth_ft"]} | override


def run(pin: str, econ: dict = ECON, comps: dict = COMPS, **override) -> tuple[dict, dict]:
    row, opts = R.score_parcel(parcel(pin, **override), comps, RULES, econ, CFG, RATIOS)
    return row, {(o["option"], o["sewer"]): o for o in opts}


def test_core_costs_must_be_filled():
    assert R.blank_core(ECON) == []
    assert R.blank_core({"costs": {"build_cost_per_sf": None, "soft_cost_pct": 8, "target_profit_pct": None}}) == [
        "build_cost_per_sf", "target_profit_pct"]


def test_house_on_its_lot_02708315():
    # 12607 Cardinal Point Rd, N1-A, 0.27871 ac = 12,140.8 sf, 80.15 x 193.56 ft; sewer by plat proxy
    row, o = run("02708315")
    h = o[("cur_sfh", "public")]
    # envelope (80.15 - 2 x 5) x (193.56 - 27 - 40) = 70.15 x 126.56 = 8,878.5; coverage 40% x 12,140.8
    # = 4,856.3 -> footprint 4,856.3 x 3 floors = 14,568.9 zoning max; market cap 3,000 -> 3,000 sf
    assert h["envelope_sf"] == pytest.approx(8878.5, abs=0.1) and h["coverage_sf"] == pytest.approx(4856.3, abs=0.1)
    assert (h["stories"], h["zoning_max_sf"], h["size_sf"]) == (3, pytest.approx(14568.9, abs=0.1), 3000)
    # 142,500 + ADU 40,000 (allowed on the lot) - slope 0.05180 ac x 20,000 = 1,036.03; no road or
    # stormwater on the lot as it is
    assert (h["adu_premium"], h["road_cost"], h["stormwater_cost"]) == (40000, 0, 0)
    assert h["slope_cost"] == pytest.approx(1036.03, abs=0.01)
    assert h["rlv"] == pytest.approx(181463.97, abs=0.01) and row["best_option"] == "cur_sfh"
    # Townhome needs rezoning to N1-F (4 units): 4 x 67,100 - road 80.48 ft x 200 = 16,096.56 -
    # stormwater 4 x 5,000 - slope 1,036.03 = 231,267.41. Higher, but never the best use.
    assert (row["rezoning_option"], row["rezoning_rlv"]) == ("townhome", pytest.approx(231267.41, abs=0.01))
    assert o[("townhome", "public")]["kind"] == "rezoning"
    # Price: improved Charlotte 328,200 x 1.2 = 393,840; score 181,463.97 - 393,840
    assert (row["ratio_group"], row["ratio_sales"], row["price"]) == ("CHARLOTTE improved", 25, pytest.approx(393840))
    assert row["score"] == pytest.approx(-212376.03, abs=0.01)
    assert row["sewer_basis"] == "proxy_served" and row["sewer_low_confidence"] and not row["verify_sewer"]
    assert not row["sewer_unverified_high_score"]          # score below the 100,000 cap


def test_plex_options_share_one_building_and_are_thin_02708315():
    _, o = run("02708315")
    d, t = o[("cur_duplex", "public")], o[("cur_triplex", "public")]
    # one 3,200 sf building: duplex 70,400 + ADU 40,000 - 1,036.03; triplex has no ADU
    assert d["unit_price"] == pytest.approx(352000) and d["rlv"] == pytest.approx(109363.97, abs=0.01)
    assert t["unit_price"] == pytest.approx(704000 / 3) and t["rlv"] == pytest.approx(69363.97, abs=0.01)
    assert d["thin_comps"] and "plex comps pool" in d["notes"]


def test_tract_options_21311502():
    # 8625 Sardis Rd, N1-A, vacant, front piece 102,826.9 sf net on Sardis Rd (frontage 147.1 ft);
    # slope 15-25%: 1.17747 ac x 20,000 = 23,549.34; floodplain flag 0.25896 ac = 11,280.2 sf =
    # 10.97% of the net land.
    row, o = run("21311502")
    road, slope = 147.1012 * 200, 23549.34
    assert o[("sub_standard", "public")]["road_cost"] == pytest.approx(road, abs=0.01)
    # standard: 7 lots of 10,000 sf; floodplain floor(7 x 0.1097) = 0
    s = o[("sub_standard", "public")]
    assert (s["units"], s["floodplain_units"]) == (7, 0)
    assert s["rlv"] == pytest.approx(7 * 142500 - road - 35000 - slope, abs=0.01)                # 909,530.42
    # conservation: 14 lots of 5,000 sf, 35 ft wide, 142.86 deep: envelope 25 x 75.86 = 1,896.4 sf
    # x 3 floors > 3,000 cap; floodplain floor(14 x 0.1097) = 1 unit
    c = o[("sub_conservation", "public")]
    assert c["envelope_sf"] == pytest.approx(1896.43, abs=0.01) and c["floodplain_units"] == 1
    assert c["rlv"] == pytest.approx(14 * 142500 - road - 70000 - slope - 15000, abs=0.01)       # 1,857,030.42
    # mixed income: 11 lots, 2 affordable at 250,000 instead of 750,000
    m = o[("sub_mixed_income", "public")]
    assert (m["affordable_units"], m["obligation_cost"]) == (2, 1000000)
    assert m["rlv"] == pytest.approx(11 * 142500 - road - 55000 - slope - 15000 - 1000000, abs=0.01)
    # best use = conservation; rezoning townhome 32 units: 2,147,200 - road - 160,000 - slope -
    # 3 floodplain units 45,000 = 1,889,230.42; if crossed, 46 units less the 250,000 crossing
    assert (row["best_option"], row["best_rlv"]) == ("sub_conservation", pytest.approx(1857030.42, abs=0.01))
    assert row["rezoning_rlv"] == pytest.approx(32 * 67100 - road - 160000 - slope - 45000, abs=0.01)
    assert row["if_crossed_option"] == "if_crossed_townhome"
    assert row["if_crossed_rlv"] == pytest.approx(46 * 67100 - road - 230000 - slope - 5 * 15000 - 250000, abs=0.01)
    # one house: its 1,000 sf footprint fits in the 91,547 sf outside the floodplain flag
    h = o[("cur_sfh", "public")]
    assert h["floodplain_units"] == 0 and h["rlv"] == pytest.approx(142500 + 40000 - slope, abs=0.01)
    # vacant: no usable vacant group (3 sales), so countywide all (28 sales, 1.2): 424,400 x 1.2
    assert (row["land_class"], row["ratio_group"], row["ratio_sales"]) == ("vacant", "ALL ALL", 28)
    assert row["score"] == pytest.approx(1857030.42 - 509280, abs=0.01)
    assert row["sewer_unverified_high_score"]               # plat proxy sewer, score over 100,000


def test_blank_costs_are_left_out_and_flagged_21311502():
    econ = copy.deepcopy(ECON)
    econ["site_work"]["road_per_ft"] = None
    econ["obligations"]["affordable_max_price"] = None
    row, o = run("21311502", econ)
    c = o[("sub_conservation", "public")]
    assert c["road_cost"] is None and c["blank_inputs"] == "road_per_ft"
    assert c["rlv"] == pytest.approx(1857030.42 + 147.1012 * 200, abs=0.01)                    # upper bound
    m = o[("sub_mixed_income", "public")]
    assert m["obligation_cost"] is None and m["blank_inputs"] == "road_per_ft, affordable_max_price"
    h = o[("cur_sfh", "public")]
    assert h["blank_inputs"] is None and h["road_cost"] == 0   # no road on the lot as it is: nothing missing
    assert row["rlv_incomplete"] and row["blank_inputs"] == "road_per_ft"


def test_unknown_sewer_outside_town_scores_on_septic_21311502():
    # Same parcel, as if outside every town limit with no sewer data: single-family on 1-acre lots.
    # Lot as it is (262,032 sf) qualifies; split min(2, 262,032 / 43,560 = 6) = 2 lots of 131,016 sf;
    # subdivision min(7, 102,826.9 x 0.70 / 43,560 = 1.65) = 1 -> none.
    row, o = run("21311502", sewer_status="unknown", sewer_confidence=None, in_municipal_limit=False)
    assert sorted(k for k in o if k[1] == "septic") == [("cur_sfh", "septic"), ("split_sfh", "septic")]
    assert row["sewer_basis"] == "none_outside" and row["scored_on"] == "septic" and not row["verify_sewer"]
    # split: 2 x 142,500 - road 29,420.25 - stormwater 10,000 - slope = 222,030.42
    assert (row["best_option"], row["best_rlv"]) == ("split_sfh", pytest.approx(222030.42, abs=0.01))
    assert (row["if_sewer_option"], row["if_sewer_rlv"]) == ("sub_conservation", pytest.approx(1857030.42, abs=0.01))
    assert not row["sewer_low_confidence"] and not row["sewer_unverified_high_score"]


def test_unknown_sewer_in_town_is_assumed_served_03508109():
    # 6124 Old Plank Rd: sewer unknown, inside Charlotte -> assumed served, verify_sewer
    row, o = run("03508109")
    assert row["sewer_basis"] == "assumed_in_town" and row["verify_sewer"] and row["sewer_low_confidence"]
    assert row["scored_on"] == "public" and not any(k[1] == "septic" for k in o)


def test_by_right_townhome_can_be_best_use_02918135():
    # 8435 Portsmouth Dr, N2-B (townhome by right): 20 units x 67,100 - road 374.87 ft x 200 -
    # stormwater 100,000 - slope 0.04936 ac x 20,000 = 1,166,039.75, above the subdivision's 9 lots
    # (1,282,500 - 74,973.12 - 45,000 - 987.12 = 1,161,539.75)
    row, o = run("02918135")
    assert o[("townhome", "public")]["kind"] == "by_right"
    assert (row["best_option"], row["best_rlv"]) == ("townhome", pytest.approx(1166039.75, abs=0.01))
    assert o[("sub_standard", "public")]["rlv"] == pytest.approx(1161539.75, abs=0.01)


def test_no_comps_gives_no_value():
    row, o = run("02763166", comps={})
    assert row["best_option"] is None and row["score"] is None
    assert row["no_value_reason"] == "no option with comps and a buildable size"
    assert "no comps within the widest radius" in o[("cur_sfh", "public")]["notes"]


def test_excluded_owners_are_valued_but_not_scored():
    row, _ = run("02763166", owner_type="church_nonprofit")
    assert row["best_rlv"] is not None and row["score"] is None and row["excluded_reason"] == "owner type church_nonprofit"
    row, _ = run("02763166", land_use="TOWN HOUSE COMMON AREA")
    assert row["score"] is None and row["excluded_reason"] == "land use TOWN HOUSE COMMON AREA"


def test_ratio_falls_back_to_broader_groups():
    obs = pd.DataFrame({"municipality": ["DAVIDSON"] * 22 + ["PINEVILLE"] * 2 + ["DAVIDSON"] * 3,
                        "land_class": ["improved"] * 24 + ["vacant"] * 3,
                        "ratio": [1.0] * 11 + [1.4] * 11 + [3.0, 3.0] + [0.9] * 3})
    t = R.ratio_table(obs, 20)
    assert R.pick_ratio(t, "DAVIDSON", "improved") == (pytest.approx(1.2), "DAVIDSON improved", 22)
    # Pineville improved has 2 sales -> countywide improved: 24 sales, median of 11 x 1.0, 11 x 1.4, 2 x 3.0 = 1.4
    assert R.pick_ratio(t, "PINEVILLE", "improved") == (pytest.approx(1.4), "ALL improved", 24)
    # vacant has 3 sales countywide -> countywide all: 27 sales sorted 3 x 0.9, 11 x 1.0 (4th-14th),
    # ... -> the 14th, median, is 1.0
    assert R.pick_ratio(t, "DAVIDSON", "vacant") == (pytest.approx(1.0), "ALL ALL", 27)


def test_comps_widen_until_enough():
    # Target A at (0, 0): 3 sfh comps 0.5 mi away ($200, $210, $220/sf), 2 more at 1.5 mi ($300,
    # $310), 1 at 2.5 mi ($400). 1 mi: 3 < 5 -> 2 mi: 5 comps, median $220, 90th percentile of
    # their sf (1,000..5,000 step 1,000) = 4,600. Target B 10 miles away: none within 3 mi -> thin.
    mi = R.FT_PER_MILE
    comps = pd.DataFrame({"product": "sfh", "x": [0.5 * mi] * 3 + [1.5 * mi] * 2 + [2.5 * mi], "y": 0.0,
                          "price_per_sf": [200, 210, 220, 300, 310, 400], "heated_sf": [1000, 2000, 3000, 4000, 5000, 6000]})
    targets = pd.DataFrame({"pin": ["A", "B"], "x": [0.0, 10 * mi], "y": [0.0, 0.0]})
    cfg = {"radii_miles": [1, 2, 3], "min_comps": 5, "products": {"sfh": [], "townhome": []}}
    df = R.comp_stats(targets, comps, cfg, 0.9).set_index(["pin", "product"])
    a = df.loc[("A", "sfh")]
    assert (a["radius_mi"], a["comp_count"], a["median_price_per_sf"], a["size_cap_sf"], a["thin_comps"]) == (
        2, 5, 220, pytest.approx(4600), False)
    b = df.loc[("B", "sfh")]
    assert (b["radius_mi"], b["comp_count"], b["thin_comps"]) == (3, 0, True) and pd.isna(b["median_price_per_sf"])
    assert df.loc[("A", "townhome"), "comp_count"] == 0


def test_store_and_rerun_without_changes():
    s = "test_rlv"
    engine = get_engine()
    rows, opts = [], []
    for pin in ("02708315", "21311502", "02918135"):
        r, o = R.score_parcel(parcel(pin), COMPS, RULES, ECON, CFG, RATIOS)
        rows.append(r)
        opts += o
    with engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {s} CASCADE; CREATE SCHEMA {s}"))
    try:
        for d in (date(2026, 10, 28), date(2026, 11, 4)):
            with engine.begin() as conn:
                ch_o = R._store(conn, R._frame(opts, R.OPTION_COLS), f"{s}.parcel_rlv_options", ["pin", "option", "sewer"], d)
                ch_p = R._store(conn, R._frame(rows, R.PARCEL_COLS), f"{s}.parcel_rlv", ["pin"], d)
        assert ch_o == {"added": 0, "changed": 0, "removed": 0, "current": len(opts)}
        assert ch_p == {"added": 0, "changed": 0, "removed": 0, "current": 3}
        with engine.connect() as conn:
            got = conn.execute(text(f"SELECT rlv, road_cost, obligation_cost FROM {s}.parcel_rlv_options "
                                    f"WHERE pin = '21311502' AND option = 'sub_mixed_income'")).one()
        assert got[0] == pytest.approx(444530.42, abs=0.01) and got[2] == 1000000
    finally:
        with engine.begin() as conn:
            conn.execute(text(f"DROP SCHEMA {s} CASCADE"))
