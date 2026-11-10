"""Tests for scoring/backtest.py on the fixture parcels and test costs of tests/test_rlv.py."""

import math

import pandas as pd
import pytest

import scoring.backtest as B
from tests.test_rlv import CFG, COMPS, ECON, RATIOS, RULES, parcel

TIERS = ECON["comps"]["price_tiers_per_sf"]


def test_comps_at_uses_only_earlier_sales_and_never_the_parcel():
    # 7 single-family comps near (0, 0): 5 before the 2025-06-01 sale, 1 after it, 1 the parcel's own
    pool = pd.DataFrame({
        "account_pin": ["A", "B", "C", "D", "E", "F", "SELF"],
        "sale_date": pd.to_datetime(["2024-01-01", "2024-06-01", "2024-09-01", "2025-01-01", "2025-05-31",
                                     "2025-07-01", "2025-03-01"]),
        "product": "sfh", "price_per_sf": [200.0, 210, 220, 230, 240, 900, 900],
        "sale_price": 500000.0, "heated_sf": 2500.0, "lot_sf": 9000.0, "x": 100.0, "y": 100.0})
    c = B.comps_at("P", 0.0, 0.0, "SELF", "2025-06-01", pool, ECON)
    assert (c["sfh"]["comp_count"], c["sfh"]["median_price_per_sf"], c["sfh"]["radius_mi"]) == (5, 220, 1)
    assert c["lot"]["8000-12000"]["comp_count"] == 5 and c["county_bands"]["8000-12000"]["sales"] == 5
    # a sale before most of them: only 2 earlier comps -> thin at the widest radius
    c = B.comps_at("P", 0.0, 0.0, "SELF", "2024-07-01", pool, ECON)
    assert (c["sfh"]["comp_count"], c["sfh"]["thin_comps"]) == (2, True)


def test_value_sale_as_vacant_at_the_price_paid_02708315():
    # 12607 Cardinal Point Rd sold as a lot for 300,000, then built. Valued as vacant: cur_sfh margin
    # 142,500 + ADU 40,000 - slope 1,036.03, no demolition -> 181,463.97; ratio 0.6049
    sale = {"pin": "02708315", "sale_price": 300000, "sale_date": "2025-03-01", "validity_desc": None,
            "bldg_type": "SINGLE FAMILY RESIDENTIAL", "built_heated_sf": 3100, "year_built": 2025}
    r = B.value_sale(parcel("02708315"), sale, COMPS, RULES, ECON, CFG, RATIOS, None, "sfh", ["cur_sfh"])
    assert (r["option"], r["complete"], r["price_tier"], r["teardown"]) == ("cur_sfh", True, "250-350", False)
    assert r["rlv"] == pytest.approx(181463.97, abs=0.01) and r["ratio"] == pytest.approx(181463.97 / 300000)
    assert r["_row"]["price"] == 300000 and r["_option"]["demolition_cost"] == 0
    # a teardown is flagged (the old house's size is gone from CAMA, so no demolition cost)
    assert B.value_sale(parcel("02708315"), sale | {"validity_desc": "DEMO SALE"}, COMPS, RULES, ECON, CFG,
                        RATIOS, None, "sfh", ["cur_sfh"])["teardown"]


def test_stats_and_miss_size():
    s = B.stats(pd.DataFrame({"ratio": [0.5, 1.0, 2.0, None], "complete": [True, False, True, True]}))
    # abs errors 50%, 0%, 100%
    assert (s["n"], s["complete"], s["median"], s["median_abs_err_pct"], s["mean_abs_err_pct"]) == (
        3, 2, 1.0, 50.0, 50.0)
    assert B.miss_size(0.5) == pytest.approx(B.miss_size(2.0)) and B.miss_size(-0.1) == math.inf


def test_townhome_unit_residual():
    # fixture townhome comps $230/sf x 2,200 sf = 506,000; build 150 x 2,200 = 330,000; soft 33,000;
    # profit 15% = 75,900; no selling or loan in the test costs -> 67,100 (as test_rlv's townhomes)
    r = B.townhome_residual(COMPS["townhome"], 50000, ECON, TIERS)
    assert (r["unit_price"], r["price_tier"], r["blank_inputs"]) == (506000, "<250", None)
    assert r["residual"] == pytest.approx(67100)
    assert B.townhome_residual(None, 50000, ECON, TIERS)["residual"] is None


def test_implied_build_cost_02708315():
    # cur_sfh at test costs: RLV 181,463.97 on a 3,000 sf house at $150/sf (+10% soft). Paid 300,000:
    # the build cost would have to fall by (300,000 - 181,463.97) / (3,000 x 1.1) = 35.92 -> $114.08/sf
    sale = {"pin": "02708315", "sale_price": 300000, "sale_date": "2025-03-01", "validity_desc": None,
            "bldg_type": "SINGLE FAMILY RESIDENTIAL", "built_heated_sf": 3100, "year_built": 2025}
    r = B.value_sale(parcel("02708315"), sale, COMPS, RULES, ECON, CFG, RATIOS, None, "sfh", ["cur_sfh"])
    assert B.implied_build_cost(pd.Series(r), 10) == pytest.approx(150 - 118536.03 / 3300, abs=0.01)


def test_sale_row_has_municipality_arv_and_land_share_02708315():
    # ARV = cur_sfh's home sale value 3,000 sf x $250 = 750,000; paid 300,000 = 40% of ARV
    sale = {"pin": "02708315", "sale_price": 300000, "sale_date": "2025-03-01", "validity_desc": None,
            "bldg_type": "SINGLE FAMILY RESIDENTIAL", "built_heated_sf": 3100, "year_built": 2025}
    r = B.value_sale(parcel("02708315"), sale, COMPS, RULES, ECON, CFG, RATIOS, None, "sfh", ["cur_sfh"])
    assert (r["municipality"], r["arv"], r["land_pct_of_arv"]) == ("CHARLOTTE", 750000, 40.0)


def test_land_rule_share_and_report_bands():
    df = pd.DataFrame({"ratio": [0.8, 1.0, 1.2, None], "complete": True, "land_pct_of_arv": [10.0, 20.0, 25.0, 5.0]})
    # the sale without a ratio doesn't count; 2 of 3 at most 20%
    assert B.stats(df, 20)["land_rule_pct"] == pytest.approx(200 / 3)
    assert B.stats(df)["land_rule_pct"] is None
    bands = B.load_config()["report_bands_per_sf"]
    assert [B.R.price_tier(x, bands) for x in (229, 230, 275, 349, 500, 900)] == [
        "<230", "230-275", "275-350", "275-350", "500+", "500+"]


def test_townhome_residual_uses_carry_in_the_simple_model():
    # 506,000 - 330,000 - 33,000 - 75,900 - carry 3.5% x 506,000 (17,710) = 49,390
    econ = ECON | {"carry_cost_pct": 3.5}
    assert B.townhome_residual(COMPS["townhome"], 50000, econ, TIERS)["residual"] == pytest.approx(49390)
