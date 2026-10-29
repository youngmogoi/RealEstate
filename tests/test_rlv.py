"""Tests for scoring/rlv.py on the yield test parcels (tests/fixtures/yield/), with their RLV
inputs from parcel_features / parcel_constraints / parcel_cama in tests/fixtures/rlv/parcels.csv
(exported 2026-10-28). Comps and costs are fixed test values (below), not the county's or
config's, so the hand calculations don't move when you edit config/economics.yaml.

House model, per house (sfh comps $250/sf, median 3,000 sf; zoning allows more on every lot here):
revenue 3,000 x 250 = 750,000; build 3,000 x 150 = 450,000; soft 10% = 45,000; profit 15% of
revenue = 112,500; margin 142,500.
Per townhome (comps $230/sf, median 2,200 sf; zoning 880 sf x 3 floors = 2,640): 506,000 - 330,000
- 33,000 - 75,900 = 67,100.
Plex (proxy: townhome $230 x 0.9 = $207/sf, 2,200 sf per unit): duplex building 4,400 sf: 910,800 -
660,000 - 66,000 - 136,620 = 48,180; triplex 6,600 sf: 1,366,200 - 990,000 - 99,000 - 204,930 = 72,270.
Lot model: finished lot 0.25 x median new home 750,000 = 187,500; development 60,000 a lot.
Projects sell 4 a month, discounted at 10% a year: factor F(n) = mean of 1.1^(-m/12), m = 1..ceil(n/4).
Demolition $10 per existing CAMA heated sf. Floors: 48 or 40 ft / 12 ft, at most 3.
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
    "costs": {"build_cost_per_sf": {"sfh": 150, "duplex_triplex": 150, "townhome": 150}, "soft_cost_pct": 10,
              "target_profit_pct": 15, "demo_cost_per_sf": 10},
    # no selling cost and all cash, so the hand-checked numbers below hold; both are tested on their own
    "selling": {"selling_cost_pct": 0},
    "financing": {"loan_to_cost": 0, "construction_rate_pct": None, "loan_closing_pct": None, "build_months": 6,
                  "months_to_sell": 3, "avg_draw_during_build": 0.5},
    "site_work": {"sewer_per_ft": 100, "road_per_ft": 200, "stormwater_per_unit": 5000, "slope_per_acre": 20000,
                  "floodplain_per_unit": 15000, "creek_crossing": 250000, "road_and_stormwater_on_lot_as_is": False},
    "subdivision": {"lot_to_home_ratio": 0.25, "development_cost_per_lot": 60000},
    "project": {"absorption_homes_per_month": 4, "annual_discount_rate_pct": 10, "max_lots_per_project": 150},
    "plex": {"duplex_discount": 0.9, "min_real_sales": 20},
    "obligations": {"affordable_max_price": 250000},
    "adu_premium": 40000,
}
ECON["house_size"] = {"story_height_ft": 12, "max_stories": 3}
ECON["sewer"] = {"septic_min_lot_sf": 43560}
ECON["rollback_tax"] = ECON["rollback_tax"] | {"tax_rate_per_100": {"CHARLOTTE": 1.0}}
EDGES = ECON["comps"]["lot_bands_sf"]                 # 0, 5000, 8000, 12000, 20000, 43560
SFH = {"radius_mi": 1, "comp_count": 12, "median_price_per_sf": 250.0, "median_sale_price": 750000.0,
       "median_heated_sf": 3000.0, "thin_comps": False}
COMPS = {
    "sfh": SFH,
    # the same comps in every lot-size band, so the per-house figures below hold for every lot
    "lot": {b: SFH for b in R.band_labels(EDGES)},
    "county_bands": {b: {"median_heated_sf": 2500.0} for b in R.band_labels(EDGES)},
    "townhome": {"radius_mi": 2, "comp_count": 8, "median_price_per_sf": 230.0, "median_sale_price": 506000.0,
                 "median_heated_sf": 2200.0, "thin_comps": False},
    "plex": {"radius_mi": 3, "comp_count": 3, "median_price_per_sf": 220.0, "median_sale_price": 700000.0,
             "median_heated_sf": 3200.0, "thin_comps": True},
}
# 25 Charlotte improved sales at 1.2 and 3 vacant ones at 2.0: vacant groups are too small (< 20),
# so vacant parcels fall back to countywide all (28 sales, median 1.2).
RATIOS = R.ratio_table(pd.DataFrame({"municipality": ["CHARLOTTE"] * 28, "land_class": ["improved"] * 25 + ["vacant"] * 3,
                                     "ratio": [1.2] * 25 + [2.0] * 3}), 20)


def F_(n: int) -> float:
    months = -(-n // 4)
    return sum(1.1 ** (-m / 12) for m in range(1, months + 1)) / months


def _features() -> dict[str, dict]:
    df = pd.read_csv(FEATURES, dtype={"pin": str})
    for c in ("in_municipal_limit", "vacant", "access_via_common_area"):
        df[c] = df[c].map({"t": True, "f": False})
    return {r["pin"]: {k: (None if pd.isna(v) else v) for k, v in r.items()} for r in df.to_dict("records")}


FEAT = _features()


def parcel(pin: str, **override) -> dict:
    return Y.compute_yield(P[pin], RULES, CFG) | FEAT[pin] | {"lot_depth_ft": P[pin]["lot_depth_ft"]} | override


def run(pin: str, econ: dict = ECON, comps: dict = COMPS, **override) -> tuple[dict, dict]:
    row, opts = R.score_parcel(parcel(pin, **override), comps, RULES, econ, CFG, RATIOS)
    return row, {(o["option"], o["sewer"]): o for o in opts}


def test_core_costs_must_be_filled():
    assert R.blank_core(ECON) == []
    blank = {"sfh": None, "duplex_triplex": None, "townhome": None}
    assert R.blank_core({"costs": {"build_cost_per_sf": blank, "soft_cost_pct": 8, "target_profit_pct": None}}) == [
        "build_cost_per_sf", "target_profit_pct"]
    # one product's build cost is enough to run
    assert R.blank_core({"costs": {"build_cost_per_sf": blank | {"sfh": 200}, "soft_cost_pct": 8,
                                   "target_profit_pct": 15}}) == []


FIN = {"loan_to_cost": 0.75, "construction_rate_pct": 8, "loan_closing_pct": 1, "build_months": 6,
       "months_to_sell": 3, "avg_draw_during_build": 0.5}


def test_selling_and_financing_house_model_02708315():
    # cur_sfh on 02708315 (see test_rebuild_on_its_lot): revenue 750,000, build 450,000, soft 45,000,
    # profit 112,500 -> margin 142,500 before selling and financing; price (land) 393,840.
    econ = copy.deepcopy(ECON) | {"selling": {"selling_cost_pct": 6}, "financing": FIN}
    _, o = run("02708315", econ)
    h = o[("cur_sfh", "public")]
    # selling 6% x 750,000 = 45,000
    # loan 0.75 x (450,000 + 45,000 + 393,840) = 666,630; interest 666,630 x 8% x (6/12 x 0.5 + 3/12)
    # = 26,665.20; closing 1% = 6,666.30; financing 33,331.50
    assert (h["selling_cost"], h["land_in_loan"], h["loan_amount"]) == (
        pytest.approx(45000), pytest.approx(393840), pytest.approx(666630))
    assert (h["financing_interest"], h["financing_closing"], h["financing_cost"]) == (
        pytest.approx(26665.2), pytest.approx(6666.3), pytest.approx(33331.5))
    assert h["rlv"] == pytest.approx(142500 - 45000 - 33331.5 + 40000 - 19440 - 1036.03, abs=0.01)
    assert h["blank_inputs"] is None
    # townhomes (a project): the same formula on the option's totals, land = the whole parcel's price
    t = o[("townhome", "public")]
    assert t["loan_amount"] == pytest.approx(0.75 * (t["build_cost"] + t["soft_cost"] + 393840))


def test_lot_model_has_no_selling_or_loan_21311502():
    # subdivisions sell lots to builders: the selling and construction loan settings don't apply
    econ = copy.deepcopy(ECON) | {"selling": {"selling_cost_pct": 6}, "financing": FIN}
    _, o = run("21311502", econ)
    s = o[("sub_standard", "public")]
    assert (s["selling_cost"], s["financing_cost"], s["loan_amount"]) == (None, None, None)
    assert s["rlv"] == pytest.approx(1312500 * F_(7) - 420000 - 147.1012 * 200 - 35000 - 23549.34, abs=0.01)


def test_blank_selling_financing_and_product_build_cost_02708315():
    econ = copy.deepcopy(ECON) | {"selling": {"selling_cost_pct": None},
                                  "financing": FIN | {"construction_rate_pct": None}}
    econ["costs"]["build_cost_per_sf"]["duplex_triplex"] = None
    _, o = run("02708315", econ)
    h = o[("cur_sfh", "public")]
    # blank selling and loan rate: left out (RLV = upper bound) and named; closing 6,666.30 stays
    assert (h["selling_cost"], h["financing_interest"], h["financing_cost"]) == (None, None, pytest.approx(6666.3))
    assert h["blank_inputs"] == "selling_cost_pct, financing.construction_rate_pct"
    assert h["rlv"] == pytest.approx(142500 - 6666.3 + 40000 - 19440 - 1036.03, abs=0.01)
    # blank duplex_triplex build cost: plex options get no value at all
    d = o[("cur_duplex", "public")]
    assert d["rlv"] is None and d["blank_inputs"] == "build_cost_per_sf.duplex_triplex"


def test_discount_factor():
    assert R.discount_factor(4, 4, 10) == (1, pytest.approx(1.1 ** (-1 / 12)))       # 0.99209
    months, f = R.discount_factor(14, 4, 10)                                          # 4 months
    assert months == 4 and f == pytest.approx((0.992089 + 0.984240 + 0.976454 + 0.968729) / 4, abs=1e-6)


def test_rebuild_on_its_lot_02708315():
    # 12607 Cardinal Point Rd, N1-A, 0.27871 ac = 12,140.8 sf, 80.15 x 193.56 ft; house of 1,944
    # heated sf (CAMA); sewer by plat proxy
    row, o = run("02708315")
    h = o[("cur_sfh", "public")]
    # envelope (80.15 - 2 x 5) x (193.56 - 27 - 40) = 8,878.5; coverage 40% x 12,140.8 = 4,856.3 ->
    # zoning max 4,856.3 x 3 floors = 14,568.9; comps' median 3,000 -> 3,000 sf
    assert h["envelope_sf"] == pytest.approx(8878.5, abs=0.1) and h["coverage_sf"] == pytest.approx(4856.3, abs=0.1)
    assert (h["stories"], h["zoning_max_sf"], h["comp_median_sf"], h["size_sf"]) == (3, pytest.approx(14568.9, abs=0.1), 3000, 3000)
    # 142,500 + ADU 40,000 - demolition 1,944 x 10 - slope 0.05180 ac x 20,000 = 1,036.03; no road or
    # stormwater on the lot as it is; not a project, so no absorption discount
    assert (h["adu_premium"], h["demolition_cost"], h["road_cost"], h["stormwater_cost"]) == (40000, 19440, 0, 0)
    assert (h["absorption_months"], h["discount_cost"]) == (None, 0)
    assert h["rlv"] == pytest.approx(142500 + 40000 - 19440 - 1036.03, abs=0.01) and row["best_option"] == "cur_sfh"
    # Townhome needs rezoning to N1-F (4 units, a project: 1 month): 268,400 x F(4) - demolition -
    # road 80.48 ft x 200 - stormwater 20,000 - slope. Never the best use.
    assert o[("townhome", "public")]["kind"] == "rezoning"
    assert row["rezoning_rlv"] == pytest.approx(4 * 67100 * F_(4) - 19440 - 16096.56 - 20000 - 1036.03, abs=0.01)
    # Price: improved Charlotte 328,200 x 1.2 = 393,840
    assert (row["ratio_group"], row["ratio_sales"], row["price"]) == ("CHARLOTTE improved", 25, pytest.approx(393840))
    assert row["score"] == pytest.approx(162023.97 - 393840, abs=0.01)
    # a rebuild adds no units: no sewer flag although sewer is only a plat proxy
    assert row["sewer_low_confidence"] and not row["sewer_assumed_new_units"]


def test_plex_priced_from_townhome_comps_02708315():
    # only 3 plex comps (< 20): townhome $230 x 0.9, 2,200 sf a unit
    _, o = run("02708315")
    d, t = o[("cur_duplex", "public")], o[("cur_triplex", "public")]
    assert (d["comp_product"], d["comp_proxy"], d["price_per_sf"], d["size_sf"]) == ("townhome", True, pytest.approx(207), 4400)
    assert d["rlv"] == pytest.approx(48180 + 40000 - 19440 - 1036.03, abs=0.01)     # duplex keeps the ADU
    assert t["rlv"] == pytest.approx(72270 - 19440 - 1036.03, abs=0.01)
    assert "townhome comps x duplex_discount" in d["notes"]
    # with 20+ plex sales nearby, the plex comps themselves (3,200 sf building at $220)
    real = COMPS | {"plex": COMPS["plex"] | {"comp_count": 20, "thin_comps": False}}
    _, o = run("02708315", comps=real)
    d = o[("cur_duplex", "public")]
    assert (d["comp_proxy"], d["size_sf"], d["revenue"]) == (False, 3200, 3200 * 220)
    # blank duplex_discount: no plex price at all
    econ = copy.deepcopy(ECON)
    econ["plex"]["duplex_discount"] = None
    _, o = run("02708315", econ)
    assert o[("cur_duplex", "public")]["rlv"] is None and o[("cur_duplex", "public")]["blank_inputs"] == "duplex_discount"


def test_subdivisions_use_the_lot_model_21311502():
    # 8625 Sardis Rd, N1-A, vacant, front piece 102,826.9 sf net on Sardis Rd (frontage 147.1 ft);
    # slope 15-25%: 1.17747 ac x 20,000 = 23,549.34; floodplain flag 0.25896 ac = 10.97% of the net.
    row, o = run("21311502")
    road, slope = 147.1012 * 200, 23549.34
    s = o[("sub_standard", "public")]
    # 7 lots x 187,500 = 1,312,500 over 2 months; development 7 x 60,000; no house built here
    assert (s["model"], s["lot_price"], s["revenue"], s["development_cost"]) == ("lot", 187500, 1312500, 420000)
    assert s["build_cost"] is None and s["absorption_months"] == 2
    assert s["rlv"] == pytest.approx(1312500 * F_(7) - 420000 - road - 35000 - slope, abs=0.01)
    # conservation: 14 lots, 4 months, floodplain floor(14 x 0.1097) = 1 unit
    c = o[("sub_conservation", "public")]
    assert c["rlv"] == pytest.approx(14 * 187500 * F_(14) - 14 * 60000 - road - 70000 - slope - 15000, abs=0.01)
    # mixed income: 11 lots, 2 affordable homes at 250,000 instead of 750,000
    m = o[("sub_mixed_income", "public")]
    assert (m["affordable_units"], m["obligation_cost"]) == (2, 1000000)
    assert m["rlv"] == pytest.approx(11 * 187500 * F_(11) - 660000 - road - 55000 - slope - 15000 - 1000000, abs=0.01)
    # best by-right use = conservation; the rezoning townhome (32 units, 8 months, house model) and
    # the if-crossed options are reported beside it
    assert (row["best_option"], row["best_rlv"]) == ("sub_conservation", pytest.approx(c["rlv"]))
    assert row["rezoning_rlv"] == pytest.approx(32 * 67100 * F_(32) - road - 160000 - slope - 45000, abs=0.01)
    # if crossed: townhome 46 units (12 months) beats conservation 20 lots (5 months)
    assert row["if_crossed_option"] == "if_crossed_townhome"
    assert row["if_crossed_rlv"] == pytest.approx(
        46 * 67100 * F_(46) - road - 230000 - slope - 5 * 15000 - 250000, abs=0.01)
    assert o[("if_crossed_sub_conservation", "public")]["rlv"] == pytest.approx(
        20 * 187500 * F_(20) - 20 * 60000 - road - 100000 - slope - 2 * 15000 - 250000, abs=0.01)
    # vacant: no demolition anywhere, no keep-house option
    assert all(x["demolition_cost"] in (0, None) for x in o.values()) and ("split_sfh_keep_house", "public") not in o
    # vacant land: no usable vacant group (3 sales), so countywide all (28 sales, 1.2): 424,400 x 1.2
    assert (row["land_class"], row["ratio_group"], row["ratio_sales"]) == ("vacant", "ALL ALL", 28)
    assert row["score"] == pytest.approx(c["rlv"] - 509280, abs=0.01)
    assert row["sewer_assumed_new_units"]                  # a subdivision on plat-proxy sewer


def test_project_cap_21311502():
    econ = copy.deepcopy(ECON)
    econ["project"]["max_lots_per_project"] = 10
    row, o = run("21311502", econ)
    c = o[("sub_conservation", "public")]
    # 14 lots valued as 10 (3 months); stormwater and floodplain (floor(10 x 0.1097) = 1) on 10
    assert (c["yield_units"], c["units"], c["over_project_cap"], c["absorption_months"]) == (14, 10, True, 3)
    assert c["rlv"] == pytest.approx(10 * 187500 * F_(10) - 600000 - 147.1012 * 200 - 50000 - 23549.34 - 15000, abs=0.01)
    assert o[("sub_standard", "public")]["over_project_cap"] is False
    assert row["over_project_cap"] == (o[(row["best_option"], "public")]["over_project_cap"])


def test_split_keeping_the_house_03508109():
    # 6124 Old Plank Rd, N1-B, 19,228 sf, house 1,378 heated sf, building value 165,300; splits into
    # 2 lots (frontage 138.8 ft); sewer unknown inside Charlotte (assumed served, verify_sewer)
    row, o = run("03508109")
    road, slope = 138.7648 * 200, 9.2875e-05 * 20000
    split, keep = o[("split_sfh", "public")], o[("split_sfh_keep_house", "public")]
    # tear down and build 2: 285,000 - demolition 13,780 - road - stormwater 10,000 - slope
    assert split["rlv"] == pytest.approx(2 * 142500 - 13780 - road - 10000 - slope, abs=0.01)
    # keep the house, build 1 on the new lot: 142,500 + kept house 165,300 x 1.2 - road - 5,000 - slope
    assert (keep["units"], keep["demolition_cost"], keep["retained_value"]) == (1, 0, pytest.approx(198360))
    assert keep["rlv"] == pytest.approx(142500 + 198360 - road - 5000 - slope, abs=0.01)
    assert row["best_option"] == "split_sfh_keep_house"
    assert row["sewer_basis"] == "assumed_in_town" and row["verify_sewer"] and row["sewer_assumed_new_units"]


def test_blank_costs_are_left_out_and_flagged():
    econ = copy.deepcopy(ECON)
    econ["site_work"]["road_per_ft"] = None
    econ["costs"]["demo_cost_per_sf"] = None
    econ["obligations"]["affordable_max_price"] = None
    row, o = run("21311502", econ)
    c = o[("sub_conservation", "public")]
    assert c["road_cost"] is None and c["blank_inputs"] == "road_per_ft"
    assert c["rlv"] == pytest.approx(14 * 187500 * F_(14) - 840000 - 70000 - 23549.34 - 15000, abs=0.01)
    m = o[("sub_mixed_income", "public")]
    assert m["obligation_cost"] is None and m["blank_inputs"] == "road_per_ft, affordable_max_price"
    assert o[("cur_sfh", "public")]["blank_inputs"] is None    # vacant, no road: nothing missing
    assert row["rlv_incomplete"] and row["blank_inputs"] == "road_per_ft"
    _, o = run("02708315", econ)
    assert o[("cur_sfh", "public")]["demolition_cost"] is None and o[("cur_sfh", "public")]["blank_inputs"] == "demo_cost_per_sf"
    # blank lot_to_home_ratio: subdivisions get no value
    econ["subdivision"]["lot_to_home_ratio"] = None
    row, o = run("21311502", econ)
    assert o[("sub_standard", "public")]["rlv"] is None and "lot_to_home_ratio" in o[("sub_standard", "public")]["blank_inputs"]
    assert row["best_option"] == "split_sfh"


def test_unknown_sewer_outside_town_scores_on_septic_21311502():
    # As if outside every town limit with no sewer data: single-family on 1-acre lots. Lot as it is
    # (262,032 sf) qualifies; split min(2, 262,032 / 43,560 = 6) = 2 lots; subdivision min(7,
    # 102,826.9 x 0.70 / 43,560 = 1.65) = 1 -> none.
    row, o = run("21311502", sewer_status="unknown", sewer_confidence=None, in_municipal_limit=False)
    assert sorted(k for k in o if k[1] == "septic") == [("cur_sfh", "septic"), ("split_sfh", "septic")]
    assert row["sewer_basis"] == "none_outside" and row["scored_on"] == "septic" and not row["verify_sewer"]
    # split: 2 x 142,500 - road 29,420.25 - stormwater 10,000 - slope = 222,030.41
    assert (row["best_option"], row["best_rlv"]) == ("split_sfh", pytest.approx(222030.41, abs=0.01))
    assert row["if_sewer_option"] == "sub_conservation"
    assert not row["sewer_assumed_new_units"]


def test_exclusions():
    # 8435 Portsmouth Dr is an apartment complex (MULTI FAMILY): valued, never scored. Townhome by
    # right (N2-B, 20 units, 5 months): 1,342,000 x F(20) - demolition 5,745 sf x 10 - road 374.87 ft
    # x 200 - stormwater 100,000 - slope 0.04936 ac x 20,000.
    row, o = run("02918135")
    assert (row["exclusion_category"], row["score"], row["redevelopment_watch"]) == ("apartments", None, False)
    assert row["best_rlv"] is not None
    assert o[("townhome", "public")]["rlv"] == pytest.approx(
        20 * 67100 * F_(20) - 57450 - 374.8656 * 200 - 100000 - 987.12, abs=0.01)
    for land_use, cat, watch in [("GOLF COURSE CLASS 1 - CHAMPIONSHIP", "golf_course", True),
                                 ("CLUB, LODGES, UNION HALL, SWIM CLUB", "club", True),
                                 ("SCHOOL,COLLEGE, PRIVATE", "private_school", False),
                                 ("CONDOMINIUM", "condominium", False),
                                 ("FUNERAL (MORTUARY, CEMETERY, CREMATORIUM, MAUS)", "cemetery", False),
                                 ("TOWN HOUSE COMMON AREA", "hoa_common_area", False),
                                 ("UTILITY (GAS, ELECTRIC, TELEPHONE, TELEGRAPH, RAIL", "utility", False),
                                 ("NURSING HOME", "healthcare", False), ("HOME FOR THE AGED", "healthcare", False)]:
        row, _ = run("02763166", land_use=land_use)
        assert (row["exclusion_category"], row["redevelopment_watch"], row["score"]) == (cat, watch, None), land_use
    row, _ = run("02763166", owner_type="church_nonprofit")
    assert row["exclusion_category"] == "church_nonprofit" and row["best_rlv"] is not None
    for land_use in ("SINGLE FAMILY RESIDENTIAL - GOLF", "MULTI FAMILY DUPLEX/TRIPLEX"):   # homes, still scored
        row, _ = run("02763166", land_use=land_use)
        assert row["exclusion_category"] is None and row["score"] is not None and not row["existing_commercial_use"]
    row, _ = run("02763166", land_use="COMMERCIAL")       # flagged, still scored
    assert row["existing_commercial_use"] and row["exclusion_category"] is None and row["score"] is not None


def test_no_comps_gives_no_value():
    row, o = run("02763166", comps={})
    assert row["best_option"] is None and row["score"] is None
    assert row["no_value_reason"] == "no option with comps and a buildable size"
    assert "no comps within the widest radius" in o[("cur_sfh", "public")]["notes"]


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
    # $310), 1 at 2.5 mi ($400). 1 mi: 3 < 5 -> 2 mi: 5 comps, medians $220/sf, sale price 600,000,
    # 3,000 sf. Target B 10 miles away: none within 3 mi -> thin.
    mi = R.FT_PER_MILE
    comps = pd.DataFrame({"product": "sfh", "x": [0.5 * mi] * 3 + [1.5 * mi] * 2 + [2.5 * mi], "y": 0.0,
                          "price_per_sf": [200, 210, 220, 300, 310, 400],
                          "sale_price": [200000, 420000, 660000, 1200000, 1550000, 2400000],
                          "heated_sf": [1000, 2000, 3000, 4000, 5000, 6000]})
    targets = pd.DataFrame({"pin": ["A", "B"], "x": [0.0, 10 * mi], "y": [0.0, 0.0]})
    cfg = {"radii_miles": [1, 2, 3], "min_comps": 5, "products": {"sfh": [], "townhome": []}}
    df = R.comp_stats(targets, comps, cfg).set_index(["pin", "product"])
    a = df.loc[("A", "sfh")]
    assert (a["radius_mi"], a["comp_count"], a["median_price_per_sf"], a["median_sale_price"], a["median_heated_sf"],
            a["thin_comps"]) == (2, 5, 220, 660000, 3000, False)
    b = df.loc[("B", "sfh")]
    assert (b["radius_mi"], b["comp_count"], b["thin_comps"]) == (3, 0, True) and pd.isna(b["median_price_per_sf"])
    assert df.loc[("A", "townhome"), "comp_count"] == 0


def test_lot_bands():
    assert R.band_labels(EDGES) == ["0-5000", "5000-8000", "8000-12000", "12000-20000", "20000-43560", "43560+"]
    assert [R.lot_band(x, EDGES) for x in (4999, 5000, 12140.8, 262031.6)] == ["0-5000", "5000-8000", "12000-20000", "43560+"]
    comps = pd.DataFrame({"product": ["sfh", "sfh", "sfh", "townhome"], "lot_sf": [6000, 7000, 50000, 2000],
                          "price_per_sf": [300, 320, 200, 250], "heated_sf": [2000, 2200, 4000, 1800]})
    t = R.county_bands(comps, EDGES).set_index("lot_band")
    assert (t.loc["5000-8000", "sales"], t.loc["5000-8000", "median_heated_sf"]) == (2, 2100)
    assert t.loc["0-5000", "sales"] == 0                 # townhomes are not counted


def test_homes_priced_from_comps_on_similar_lots_21311502():
    # Conservation lots are 5,000 sf: band 5000-8000. Nearby comps on such lots: $300/sf, median
    # 2,000 sf -> home 2,000 x 300 = 600,000 (zoning allows 5,689 sf); finished lot 0.25 x 600,000.
    comps = COMPS | {"lot": COMPS["lot"] | {"5000-8000": SFH | {"median_price_per_sf": 300.0, "median_heated_sf": 2000.0}}}
    _, o = run("21311502", comps=comps)
    c = o[("sub_conservation", "public")]
    assert (c["lot_band"], c["lot_band_fallback"], c["size_sf"], c["home_price"], c["lot_price"]) == (
        "5000-8000", False, 2000, 600000, 150000)
    assert c["rlv"] == pytest.approx(14 * 150000 * F_(14) - 840000 - 147.1012 * 200 - 70000 - 23549.34 - 15000, abs=0.01)
    # Only 3 such comps nearby: $/sf from all nearby single-family ($250), size from the band
    # countywide (2,500 sf) -> home 625,000
    thin = COMPS | {"lot": COMPS["lot"] | {"5000-8000": SFH | {"comp_count": 3}}}
    _, o = run("21311502", comps=thin)
    c = o[("sub_conservation", "public")]
    assert (c["lot_band_fallback"], c["price_per_sf"], c["size_sf"], c["home_price"]) == (True, 250, 2500, 625000)
    # the lot as it is (262,032 sf) is in the 43560+ band and keeps its own comps
    assert o[("cur_sfh", "public")]["lot_band"] == "43560+" and not o[("cur_sfh", "public")]["lot_band_fallback"]


def test_present_use_value_parcel_21311502():
    # As if enrolled with $500,000 deferred: price = Charlotte 5+ ac land at $200,000/ac x 6.0154 ac;
    # rollback at 1.00 per $100: 5,000 a year x (1 + 1.1025 + 1.1925 + 1.2825) = 22,887.50 (interest
    # 2% + 0.75% a month for 11, 23 and 35 months) on every option
    land = R.land_table(pd.DataFrame({"municipality": ["CHARLOTTE"] * 25, "acres": [10.0] * 25,
                                      "per_acre": [200000.0] * 25}), ECON["land_price"]["acre_bands"], 20)
    plain, _ = run("21311502")
    row, o = R.score_parcel(parcel("21311502", puv_enrolled=True, deferred_value=500000), COMPS, RULES, ECON, CFG,
                            RATIOS, land)
    o = {(x["option"], x["sewer"]): x for x in o}
    assert (row["price_basis"], row["land_price_group"], row["land_price_sales"]) == ("vacant_land_per_acre", "CHARLOTTE 5+ ac", 25)
    assert row["price"] == pytest.approx(262031.6 / 43560 * 200000, abs=1)
    assert o[("sub_conservation", "public")]["rollback_cost"] == pytest.approx(22887.5)
    assert row["best_rlv"] == pytest.approx(plain["best_rlv"] - 22887.5, abs=0.01) and row["puv_enrolled"]
    # a town without a land group falls back to the countywide band; a blank tax rate is flagged
    econ = copy.deepcopy(ECON)
    econ["rollback_tax"]["tax_rate_per_100"] = {}
    row, o = R.score_parcel(parcel("21311502", puv_enrolled=True, deferred_value=500000, municipality="DAVIDSON"),
                            COMPS, RULES, econ, CFG, RATIOS, land)
    assert row["land_price_group"] == "ALL 5+ ac" and "rollback_tax_rate" in row["blank_inputs"]


def test_store_and_rerun_without_changes():
    s = "test_rlv"
    engine = get_engine()
    rows, opts = [], []
    for pin in ("02708315", "21311502", "03508109"):
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
            got = conn.execute(text(f"SELECT retained_value, demolition_cost FROM {s}.parcel_rlv_options "
                                    f"WHERE pin = '03508109' AND option = 'split_sfh_keep_house'")).one()
        assert got == (pytest.approx(198360), 0)
    finally:
        with engine.begin() as conn:
            conn.execute(text(f"DROP SCHEMA {s} CASCADE"))
