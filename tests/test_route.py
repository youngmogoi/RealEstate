"""Tests for scoring/route.py on the hand-checked fixture parcels of tests/test_rlv.py (test costs)."""

import copy

import pytest

import scoring.rlv as R
import scoring.route as T
from tests.test_rlv import COMPS, ECON, FEAT, P, RATIOS, RULES, CFG, Y

PARTNERS_CFG, BOX = T.load_config()
TEST_PARTNER = PARTNERS_CFG["partners"][0]
ROUTING = PARTNERS_CFG["routing"]
ACRES_21311502 = 262031.6 / 43560          # 6.0154 ac (8625 Sardis Rd)


def setup(pin: str, zone: str | None = None, partners: list[dict] | None = None):
    """(parcel, options on its scored sewer, partner townhome rows) the way run_route builds them.
    `zone` recomputes the yield as if the parcel were zoned so."""
    partners = [TEST_PARTNER] if partners is None else partners
    y = Y.compute_yield(P[pin] | ({"zone_code": zone} if zone else {}), RULES, CFG)
    p_in = y | FEAT[pin] | {"lot_depth_ft": P[pin]["lot_depth_ft"]}
    row, opts = R.score_parcel(p_in, COMPS, RULES, ECON, CFG, RATIOS)
    p = row | {k: y.get(k) for k in T.YIELD_COLS} | {"acres": row["area_sf"] / R.SQFT_PER_ACRE}
    prows = {pt["name"]: T.partner_options(p_in, y, COMPS, RULES, ECON, CFG, RATIOS, None, pt)
             for pt in T.townhome_candidates(p, partners, ROUTING["parcel_county"])}
    return p, [o for o in opts if o["sewer"] == row["scored_on"]], prows


def route(pin: str, zone: str | None = None, partners: list[dict] | None = None, box: dict = BOX,
          routing: dict = ROUTING) -> dict:
    partners = [TEST_PARTNER] if partners is None else partners
    p, opts, prows = setup(pin, zone, partners)
    return T.route_parcel(p, opts, prows, partners, box, routing)


def test_config_seeded():
    assert TEST_PARTNER == {"name": "TEST PARTNER - not real", "products": ["townhome"], "counties": ["Mecklenburg"],
                            "areas": ["all"], "acres_min": 3, "acres_max": 15, "density_min": 6, "density_max": 12,
                            "sewer_required": True, "max_land_price_per_unit": 40000, "townhome_unit_width_ft": 22,
                            "payment_structure": "assignment fee"}
    assert (ROUTING["entitle_min_uplift_pct"], ROUTING["entitle_min_uplift_usd"]) == (25, 250000)
    assert BOX["products"] == ["sfh", "duplex", "triplex"] and BOX["areas"] == ["Charlotte"]


def test_product_of_options():
    assert [T.product(o) for o in ("cur_sfh", "split_triplex_keep_house", "sub_compact", "townhome",
                                   "if_crossed_townhome", "if_crossed_sub_standard", "cur_quadraplex")] == [
        "sfh", "triplex", "subdivision", "townhome", "townhome", "subdivision", "quadraplex"]


def test_uplift_needs_both_thresholds():
    assert T.uplift_ok(1_300_000, 1_000_000, ROUTING)          # +30%, +300k
    assert not T.uplift_ok(1_200_000, 1_000_000, ROUTING)      # +20%
    assert not T.uplift_ok(400_000, 200_000, ROUTING)          # +100% but only +200k
    assert T.uplift_ok(250_000, -50_000, ROUTING)              # by right <= 0: % passes, +300k


def test_partner_width_recomputes_townhome_yield_21311502():
    # 8625 Sardis Rd: townhome needs rezoning to N1-F (place type); net reachable 102,826.9 sf x 0.70
    # usable = 71,978.8 sf. N1-F: 40% coverage, 150 ft buildings, 2 structures per 3,000 sf lot,
    # 250 sf open space per unit. At 22 ft (stored): 71,978.8 x 0.4 / (22 x 40) = 32.7 -> 32.
    # At 18 ft: / (18 x 40) = 39.99 -> 39 (open space 74, lots 23 x 2 x 8 = 368 don't bind).
    y = Y.compute_yield(P["21311502"], RULES, CFG)
    assert (y["townhome_units"], y["if_crossed_townhome_units"]) == (32, 46)
    assert T.partner_townhome_units(y, RULES, CFG, 22) == {"townhome_units": 32, "if_crossed_townhome_units": 46}
    assert T.partner_townhome_units(y, RULES, CFG, 18)["townhome_units"] == 39


def test_route_to_townhome_partner_21311502_as_n2b():
    # Recomputed as if zoned N2-B: townhomes by right. N2-B coverage 60%: 71,978.8 sf usable x 0.6 /
    # (22 x 40) = 49.1 -> 49 units (open space 150 sf/unit: 69; 3,000 sf per building: 23 x 6 = 138).
    # RLV 2,738,493 beats
    # sub_standard (23 lots, 2,616,685). Density 49 / 6.0154 ac = 8.1/ac (6-12); land 509,280 / 49 =
    # 10,393/unit (<= 40,000); 6.0 ac (3-15); public sewer.
    s = route("21311502", "N2-B")
    assert (s["action"], s["option"], s["option_kind"], s["units"]) == ("route", "townhome", "by_right", 49)
    assert (s["partner"], s["matched_partners"], s["partner_payment"]) == (
        "TEST PARTNER - not real", "TEST PARTNER - not real", "assignment fee")
    assert s["rlv"] == pytest.approx(2738493.32, abs=0.01) and s["unit_width_ft"] == 22
    assert s["score"] == pytest.approx(2738493.32 - 509280, abs=0.01)
    assert "land $10,393/unit" in s["reason"]


def test_partner_unit_width_changes_the_routed_option_21311502():
    # an 18 ft partner: 43,187 sf of coverage / (18 x 40) = 59.98 -> 59 units, valued on its own width
    p18 = TEST_PARTNER | {"name": "P18", "townhome_unit_width_ft": 18}
    _, _, prows = setup("21311502", "N2-B", [p18])
    th = next(o for o in prows["P18"] if o["option"] == "townhome")
    assert (th["units"], th["unit_width_ft"], th["footprint_sf"]) == (59, 18, 720)
    s = route("21311502", "N2-B", [p18])
    assert (s["action"], s["partner"], s["units"], s["unit_width_ft"]) == ("route", "P18", 59, 18)
    assert s["rlv"] == pytest.approx(th["rlv"]) and s["best_partner"] == "P18"


def test_no_partner_fits_falls_through_21311502_as_n2b():
    # density 8.1/ac > a max of 8: no partner. My best (split_sfh, 4 houses) is 6.0 ac > 3 ac and
    # the price 509,280 > 400,000: no build fallback. If crossed: 69 townhomes 3,544,087 vs by
    # right 2,738,493: +805,594 (+29%), score > 0 -> crossing candidate. (145,255.8 sf x 0.7 x 0.6 /
    # 880 = 69.3 -> 69 units)
    dense = TEST_PARTNER | {"density_max": 8}
    s = route("21311502", "N2-B", [dense])
    assert (s["action"], s["option"], s["units"]) == ("crossing_candidate", "if_crossed_townhome", 69)
    assert s["rlv"] == pytest.approx(3544086.82, abs=0.01)
    # the stored 22 ft townhome is still the best by-right use
    assert (s["best_option"], s["best_partner"]) == ("townhome", None)
    # with my buy box opened up, my own split fits: build fallback
    open_box = BOX | {"acres_max": None, "max_price": None, "min_score": None}
    s = route("21311502", "N2-B", [dense], open_box)
    assert (s["action"], s["option"], s["units"]) == ("build_fallback", "split_sfh", 4)
    assert "density 8.1/ac > 8" in s["reason"]


def test_crossing_candidate_and_entitle_21311502():
    # As zoned (N1-A): best by right sub_conservation 14 lots 1,595,523. Rezoning townhome (N1-F, 32
    # units) 1,814,185 is only +218,662 (+13.7%): not entitle. If crossed: townhome 46 units
    # 2,324,426, +728,903 (+45.7%), score 2,324,426 - 509,280 > 0 -> crossing candidate.
    s = route("21311502")
    assert (s["action"], s["option"], s["units"]) == ("crossing_candidate", "if_crossed_townhome", 46)
    assert s["rlv"] == pytest.approx(2324425.89, abs=0.01) and s["best_option"] == "sub_conservation"
    # lower thresholds (10%, $200k): the rezoning comes first -> entitle
    s = route("21311502", routing=ROUTING | {"entitle_min_uplift_pct": 10, "entitle_min_uplift_usd": 200000})
    assert (s["action"], s["option"], s["option_kind"]) == ("entitle", "townhome", "rezoning")
    assert s["rlv"] == pytest.approx(1814185.02, abs=0.01)


def test_build_and_skip_02708315():
    # 12607 Cardinal Point Rd, Charlotte, 0.28 ac: best cur_sfh 1 house, RLV 162,024, price 393,840.
    # Score -231,816 < 50,000 -> not build; rezoning +47,680 -> not entitle -> skip.
    s = route("02708315")
    assert s["action"] == "skip" and s["option"] is None
    assert "score -$231,816 < $50,000" in s["reason"] and s["best_option"] == "cur_sfh"
    # without the score floor it fits: 1 unit <= 4, 0.28 ac in 0.1-3, price <= 400,000, public sewer
    s = route("02708315", box=BOX | {"min_score": None})
    assert (s["action"], s["option"], s["units"]) == ("build", "cur_sfh", 1)
    assert s["score"] == pytest.approx(162023.97 - 393840, abs=0.01)
    # outside my areas
    s = route("02708315", box=BOX | {"min_score": None, "areas": ["Huntersville"]})
    assert s["action"] == "skip" and "area CHARLOTTE not in ['Huntersville']" in s["reason"]


def test_excluded_parcel_is_kept_with_its_category_02918135():
    s = route("02918135")
    assert (s["action"], s["exclusion_category"], s["score"], s["option"]) == ("excluded", "apartments", None, None)
    assert s["reason"].startswith("excluded: apartments")


def test_flags_are_carried():
    p, opts, prows = setup("21311502", "N2-B")
    s = T.route_parcel(p | {"verify_sewer": True}, opts, prows, [TEST_PARTNER], BOX, ROUTING)
    assert s["verify_sewer"] and s["apartments_possible"] and not s["no_frontage"]
    assert s["creek_divides"] == bool(p["creek_divides"]) and p["creek_divides"]


def test_mining_excluded_and_watched():
    score = copy.deepcopy(R.load_config()[0]["score"])
    assert R.exclusion({"land_use": "MINING"}, score) == ("mining", "land use MINING")
    assert "mining" in score["redevelopment_watch"]
