"""Deal routing (Phase 2.4, built 2026-10-28): one recommended action per parcel, in table `scores`.

Run with `make route` (after `make rlv`). Reads parcel_rlv, parcel_rlv_options and parcel_yield;
config/partners.yaml (partner buy boxes, routing thresholds), config/my_buybox.yaml (my own spec
homes) and the exclusions in config/economics.yaml `score`.

Actions, first that applies (decided 2026-10-28):
  excluded            never scored (government, utility, church, HOA/common area, golf, mining, ...;
                      economics `score`). Kept with its category and flags.
  build               the best by-right use is one of my products (sfh, duplex, triplex) and that
                      option fits my buy box.
  route               the best by-right use is a partner product and at least one partner's buy box
                      fits it. Every matching partner is listed, best value first.
  build_fallback      a partner product is best but no partner fits, and my own best option fits my
                      buy box (including min_score).
  entitle             the best rezoning value beats the best by-right value by at least
                      entitle_min_uplift_pct AND entitle_min_uplift_usd, and its score is > 0.
  crossing_candidate  the same test for the "if the creek is crossed" value.
  skip                everything else; the reason says why.

Only complete options (no blank inputs) are ranked and routed (decided 2026-10-28); a higher
option with blank inputs is shown beside the action as the upper bound (parcel_rlv upper_bound_*).
Entitle needs a place type in routing.entitle_place_types (Neighborhood 2 or higher intensity).

Townhome partners: for a parcel inside a townhome partner's area, acreage and sewer limits, the
townhome yield and its RLV are recomputed at that partner's unit width (same land, same rule as
parcel_yield) and stored with every input in scores_partner_options. The best by-right use (and
the best rezoning and if-crossed values) is the best of the stored options (townhomes at the
scoring.yaml width) and the partner versions that fit their partner's density and land price limits.

Product of an option: sub_* = subdivision, townhome = townhome, cur_/split_<type> = <type> (sfh,
duplex, triplex, quadraplex); if_crossed_ options as the option they cross to.

Flags stored per parcel: verify_sewer, rail_on_parcel, rear_lot_with_house + rear_lot_front_pin (parcel_rlv),
no_frontage, creek_divides, apartments_possible
(parcel_yield: Multi-Family Stacked allowed by right).
"""

import argparse
import gc
from datetime import date
from pathlib import Path

import pandas as pd
import yaml
from sqlalchemy import Engine, text

import scoring.rlv as R
from ingest.db import get_engine
from scoring.zoning_rules import load_rules

Y = R.Y
ROOT = Path(__file__).resolve().parent.parent
ACTIONS = ["excluded", "build", "route", "build_fallback", "entitle", "crossing_candidate", "check_land", "likely_hoa",
           "skip"]
# actions held for a land check (routing land_checks / assessor_discount) or your verdict
ACTIONABLE = ("build", "route", "build_fallback", "entitle", "crossing_candidate")
TOWNHOME_OPTIONS = ("townhome", "if_crossed_townhome")

SCORES_COLS = {
    "pin": "str", "site_address": "str", "municipality": "str", "acres": "float", "action": "str",
    "option": "str", "option_kind": "str", "product": "str", "units": "int", "rlv": "float", "price": "float",
    "score": "float", "partner": "str", "partner_payment": "str", "unit_width_ft": "float",
    "matched_partners": "str", "best_option": "str", "best_rlv": "float", "best_partner": "str",
    "upper_bound_option": "str", "upper_bound_rlv": "float", "blank_inputs": "str", "place_type": "str",
    "price_per_sf": "float", "best_price_per_sf": "float", "arv": "float", "land_pct_of_arv": "float",
    "build_cost": "float", "cash_needed": "float",
    "exclusion_category": "str", "redevelopment_watch": "bool",
    "verify_sewer": "bool", "no_frontage": "bool", "creek_divides": "bool", "apartments_possible": "bool",
    "rail_on_parcel": "bool", "rear_lot_with_house": "bool", "rear_lot_front_pin": "str",
    "land_psf": "float", "neighbor_land_psf": "float", "neighbor_count": "int", "land_value_ratio_pct": "float",
    "assessor_discount": "bool", "held_action": "str", "stream_length_ft": "float", "creek_on_lot": "bool",
    "power_line_ft": "float", "power_line_near": "bool", "land_check": "str", "land_check_note": "str",
    "likely_hoa": "bool", "hoa_basis": "str", "hoa_name": "str", "hoa_pin": "str", "plat_year": "int",
    "pre_1980_subdivision": "bool",
    "reason": "str",
}
PARTNER_COLS = {"partner": "str", "unit_width_ft": "float"} | R.OPTION_COLS
FLAGS = ["verify_sewer", "no_frontage", "creek_divides", "apartments_possible", "rail_on_parcel", "rear_lot_with_house",
         "assessor_discount", "creek_on_lot", "power_line_near", "likely_hoa", "pre_1980_subdivision"]


def load_config() -> tuple[dict, dict]:
    read = lambda name: yaml.safe_load((ROOT / "config" / name).read_text(encoding="utf-8"))
    return read("partners.yaml"), read("my_buybox.yaml")


def load_verdicts(path: Path | None = None) -> dict[str, dict]:
    """Your manual land checks (config/land_checks.yaml): {pin: {verdict ok|no, note, checked}}."""
    path = path or ROOT / "config" / "land_checks.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None
    out = {str(pin): v for pin, v in (raw or {}).items()}
    for v in out.values():             # YAML reads a bare no as false
        if v and v.get("verdict") is False:
            v["verdict"] = "no"
    bad = [pin for pin, v in out.items() if (v or {}).get("verdict") not in ("ok", "no")]
    if bad:
        raise SystemExit(f"{path.name}: verdict must be ok or no for {', '.join(bad)}")
    return out


def product(option: str) -> str:
    """The product family of an option name (see the module docstring)."""
    o = option.removeprefix("if_crossed_")
    if o.startswith("sub_"):
        return "subdivision"
    if o == "townhome":
        return "townhome"
    return o.removeprefix("cur_").removeprefix("split_").removesuffix("_keep_house")


def _in(value: str | None, allowed: list[str] | None) -> bool:
    if not allowed:
        return True
    return any(a.lower() == "all" for a in allowed) or (value or "").lower() in {a.lower() for a in allowed}


def _money(x) -> str:
    return R._money(x)


def place_misses(box: dict, p: dict, county: str) -> list[str]:
    """Why a parcel is outside a buy box's place, size and sewer limits (empty = inside)."""
    out, acres = [], p["acres"]
    if not _in(county, box.get("counties")):
        out.append(f"county {county} not in {box.get('counties')}")
    if not _in(p.get("municipality"), box.get("areas")):
        out.append(f"area {p.get('municipality')} not in {box.get('areas')}")
    if box.get("acres_min") is not None and acres < box["acres_min"]:
        out.append(f"{acres:,.2f} ac < {box['acres_min']} ac")
    if box.get("acres_max") is not None and acres > box["acres_max"]:
        out.append(f"{acres:,.2f} ac > {box['acres_max']} ac")
    if box.get("sewer_required") and p.get("scored_on") != "public":
        out.append("no public sewer (scored on septic)")
    return out


def partner_misses(partner: dict, p: dict, o: dict) -> list[str]:
    """Why an option is outside a partner's product, density and land price per unit limits."""
    out = []
    if product(o["option"]) not in partner["products"]:
        out.append(f"{product(o['option'])} not a partner product")
        return out
    density = o["units"] / p["acres"] if p["acres"] else None
    if partner.get("density_min") is not None and (density is None or density < partner["density_min"]):
        out.append(f"density {density or 0:,.1f}/ac < {partner['density_min']}")
    if partner.get("density_max") is not None and density is not None and density > partner["density_max"]:
        out.append(f"density {density:,.1f}/ac > {partner['density_max']}")
    cap = partner.get("max_land_price_per_unit")
    if cap is not None:
        if p.get("price") is None:
            out.append("no price")
        elif p["price"] / o["units"] > cap:
            out.append(f"land {_money(p['price'] / o['units'])}/unit > {_money(cap)}")
    return out


def resale_floor(box: dict, option: str) -> float | None:
    """My minimum comps $/sf for an option: one number, or by product (plexes share duplex_triplex,
    as in build_cost_per_sf)."""
    floor = box.get("min_resale_per_sf")
    if not isinstance(floor, dict):
        return floor
    fam = product(option)
    return floor.get(fam, floor.get("duplex_triplex") if fam in ("duplex", "triplex", "quadraplex") else None)


def my_misses(box: dict, p: dict, o: dict, county: str) -> list[str]:
    """Why an option is outside my buy box (empty = Build)."""
    out = place_misses(box, p, county)
    if product(o["option"]) not in box["products"]:
        out.insert(0, f"{product(o['option'])} not in my products")
    if box.get("max_units_per_parcel") is not None and o["units"] > box["max_units_per_parcel"]:
        out.append(f"{o['units']} units > {box['max_units_per_parcel']}")
    floor = resale_floor(box, o["option"])
    if floor is not None and (o.get("price_per_sf") is None or o["price_per_sf"] < floor):
        out.append(f"comps {_money(o.get('price_per_sf'))}/sf < {_money(floor)}/sf")
    land_cap = box.get("max_land_pct_of_arv")
    if land_cap is not None and p.get("price") is not None and o.get("revenue"):
        if p["price"] / o["revenue"] * 100 > land_cap:
            out.append(f"land {p['price'] / o['revenue'] * 100:.1f}% of ARV > {land_cap}%")
    cap = box.get("max_sale_price_per_home")
    if cap is not None and o.get("unit_price") is not None and o["unit_price"] > cap:
        out.append(f"home {_money(o['unit_price'])} > {_money(cap)}")
    if p.get("price") is None:
        out.append("no price")
    else:
        if box.get("max_price") is not None and p["price"] > box["max_price"]:
            out.append(f"price {_money(p['price'])} > {_money(box['max_price'])}")
        if box.get("min_score") is not None and o["rlv"] - p["price"] < box["min_score"]:
            out.append(f"score {_money(o['rlv'] - p['price'])} < {_money(box['min_score'])}")
    return out


def uplift_ok(value: float, base: float | None, routing: dict) -> bool:
    """Does `value` beat the by-right `base` by both the % and the $ thresholds?"""
    base = base or 0
    pct_ok = base <= 0 or value >= base * (1 + routing["entitle_min_uplift_pct"] / 100)
    return pct_ok and value - base >= routing["entitle_min_uplift_usd"]


def partner_townhome_units(y: dict, rules: dict, scoring_cfg: dict, width_ft: float) -> dict:
    """townhome_units and if_crossed_townhome_units at another unit width, on the land and rule
    parcel_yield used (by right, or the place-type rezoning target)."""
    j = y["jurisdiction"]
    code = y["townhome_target_district"] if y.get("townhome_needs_rezoning") else y["base_district"]
    rule = Y.district_rule(rules, j, code)
    th = scoring_cfg["townhome"] | {"unit_width_ft": width_ft}
    ratio = scoring_cfg["yield"]["usable_ratio"]
    units = lambda net: Y.townhome_units(rule, net * ratio, y.get("lot_width_ft"), th) if rule and net else 0
    return {"townhome_units": units(y.get("net_sf")) if y.get("townhome_units") else 0,
            "if_crossed_townhome_units": units(y.get("if_crossed_net_sf")) if y.get("if_crossed_townhome_units") else 0}


def partner_options(p_in: dict, y: dict, comps: dict, rules: dict, econ: dict, scoring_cfg: dict,
                    ratios: pd.DataFrame, land: pd.DataFrame | None, partner: dict) -> list[dict]:
    """The parcel's townhome options valued at the partner's unit width (public sewer only)."""
    w = partner["townhome_unit_width_ft"]
    units = partner_townhome_units(y, rules, scoring_cfg, w)
    cfg = scoring_cfg | {"townhome": scoring_cfg["townhome"] | {"unit_width_ft": w}}
    _, opts = R.score_parcel(p_in | units, comps, rules, econ, cfg, ratios, land)
    return [o | {"partner": partner["name"], "unit_width_ft": w} for o in opts
            if o["option"] in TOWNHOME_OPTIONS and o["sewer"] == "public"]


def route_parcel(p: dict, options: list[dict], partner_rows: dict[str, list[dict]], partners: list[dict],
                 box: dict, routing: dict) -> dict:
    """The scores row for one parcel. `p`: parcel_rlv + parcel_yield fields and acres; `options`:
    its stored options on the sewer it is scored on; `partner_rows`: {partner name: its townhome
    options at the partner's width}."""
    county = routing["parcel_county"]
    row = dict.fromkeys(SCORES_COLS) | {
        "pin": p["pin"], "site_address": p.get("site_address"), "municipality": p.get("municipality"),
        "acres": p["acres"], "price": p.get("price"), "exclusion_category": p.get("exclusion_category"),
        "redevelopment_watch": bool(p.get("redevelopment_watch")), "upper_bound_option": p.get("upper_bound_option"),
        "upper_bound_rlv": p.get("upper_bound_rlv"), "blank_inputs": p.get("blank_inputs"),
        "place_type": p.get("place_type"), "rear_lot_front_pin": p.get("rear_lot_front_pin"),
        "hoa_basis": p.get("hoa_basis"), "hoa_name": p.get("hoa_name"), "hoa_pin": p.get("hoa_pin"),
        "plat_year": p.get("plat_year")} | {f: bool(p.get(f)) for f in FLAGS}

    def done(action: str, o: dict | None, reason: str, **extra) -> dict:
        if o:
            row.update(option=o["option"], option_kind=o["kind"], product=product(o["option"]), units=o["units"],
                       rlv=o["rlv"], score=None if p.get("price") is None else o["rlv"] - p["price"],
                       unit_width_ft=o.get("unit_width_ft"), price_per_sf=o.get("price_per_sf"))
            # deal-sheet numbers: ARV = the option's homes' sale value; cash = (build + land) x (1 - LTC)
            arv, build, price = o.get("revenue"), o.get("build_cost"), p.get("price")
            row.update(arv=arv, build_cost=build,
                       land_pct_of_arv=price / arv * 100 if arv and price is not None else None,
                       cash_needed=(build + price) * (1 - box["loan_to_cost"])
                       if build is not None and price is not None and box.get("loan_to_cost") is not None else None)
        return row | {"action": action, "reason": reason} | extra

    if p.get("exclusion_category"):
        return done("excluded", None, f"excluded: {p['exclusion_category']} ({p.get('excluded_reason')})"
                    + ("; on the redevelopment watch list" if p.get("redevelopment_watch") else ""))

    complete = lambda o: o.get("rlv") is not None and not o.get("blank_inputs")
    valued = [o for o in options if complete(o)]
    # a partner's own-width townhome counts as a use only where that partner's limits fit it
    by_name = {pt["name"]: pt for pt in partners}
    partner_rows = {name: [o for o in rows if complete(o)] for name, rows in partner_rows.items()}
    tagged = valued + [o for name, rows in partner_rows.items() for o in rows
                       if not partner_misses(by_name[name], p, o)]
    by_kind = lambda kind, rows: [o for o in rows if o["kind"] == kind]
    top = lambda rows: max(rows, key=lambda o: o["rlv"]) if rows else None
    b = top(by_kind("by_right", tagged))
    if b is None:
        upper = (f"; upper bound {p['upper_bound_option']} {_money(p['upper_bound_rlv'])}, blank "
                 f"{p.get('blank_inputs')}" if p.get("upper_bound_option") else "")
        return done("skip", None, f"skip: no complete by-right value "
                                  f"({p.get('no_value_reason') or 'no valued option'}){upper}")
    row |= {"best_option": b["option"], "best_rlv": b["rlv"], "best_partner": b.get("partner"),
            "best_price_per_sf": b.get("price_per_sf")}
    fam = product(b["option"])
    partner_products = {x for pt in partners for x in pt["products"]}
    mine = top([o for o in by_kind("by_right", valued) if product(o["option"]) in box["products"]])
    fmt = lambda o: f"{o['option']} ({o['units']} units, RLV {_money(o['rlv'])})"

    # 1. Build: my product is the best use and fits my buy box
    if fam in box["products"]:
        misses = my_misses(box, p, b, county)
        if not misses:
            arv = b.get("revenue")
            return done("build", b, f"build: {fmt(b)}, price {_money(p.get('price'))}, "
                                    f"score {_money(b['rlv'] - p['price'])}; comps {_money(b.get('price_per_sf'))}/sf, "
                                    f"ARV {_money(arv)}, land {p['price'] / arv * 100:.1f}% of ARV")
        build_why = f"best use {fmt(b)} is mine but outside my buy box: {'; '.join(misses)}"
    else:
        build_why = None

    # 2. Route: a partner product is best and partners fit it (townhome partners on their own width)
    matches, why_not = [], []
    if fam in partner_products:
        for pt in partners:
            if fam not in pt["products"]:
                continue
            place = place_misses(pt, p, county)
            if fam == "townhome":
                o = top([x for x in by_kind("by_right", partner_rows.get(pt["name"], [])) if x.get("rlv") is not None])
            else:
                o = top([x for x in by_kind("by_right", valued) if product(x["option"]) == fam])
            misses = place + (partner_misses(pt, p, o) if o else [f"no {fam} option at its width"])
            if misses:
                why_not.append(f"{pt['name']}: {'; '.join(misses)}")
            else:
                matches.append((o["rlv"], pt, o))
        matches.sort(key=lambda m: -m[0])
        if matches:
            _, pt, o = matches[0]
            names = "; ".join(m[1]["name"] for m in matches)
            return done("route", o, f"route: {fmt(o)} to {names} ({pt.get('payment_structure')}); land "
                                    f"{_money(p['price'] / o['units'])}/unit", partner=pt["name"],
                        partner_payment=pt.get("payment_structure"), matched_partners=names)
        # 3. Build fallback: no partner fits, but my own best option fits my buy box
        if mine and not my_misses(box, p, mine, county):
            return done("build_fallback", mine, f"build fallback: best use {fmt(b)} fits no partner "
                                                f"({' | '.join(why_not)}); mine {fmt(mine)} fits my buy box")

    # 4-5. Entitle / crossing candidate: enough uplift over the best by-right value, score > 0
    for action, kind in (("entitle", "rezoning"), ("crossing_candidate", "if_crossed")):
        if kind == "rezoning" and p.get("place_type") not in routing["entitle_place_types"]:
            continue
        o = top(by_kind(kind, tagged))
        if o and uplift_ok(o["rlv"], b["rlv"], routing) and p.get("price") is not None and o["rlv"] - p["price"] > 0:
            return done(action, o, f"{action}: {fmt(o)} vs by-right {fmt(b)}, +{_money(o['rlv'] - b['rlv'])}; "
                                   f"score {_money(o['rlv'] - p['price'])}", partner=o.get("partner"))

    # 6. Skip
    if build_why:
        reason = build_why
    elif fam in partner_products:
        reason = f"best use {fmt(b)}: no partner fits ({' | '.join(why_not)}) and nothing of mine fits my buy box"
    else:
        reason = f"best use {fmt(b)}: neither mine nor a partner product"
    return done("skip", None, "skip: " + reason)


def hoa_hold(row: dict, box: dict) -> dict:
    """my_buybox exclude_likely_hoa (decided 2026-11-10): a likely-HOA parcel is never Build (or
    build_fallback); it stays scored and goes to its own list, action likely_hoa."""
    if not box.get("exclude_likely_hoa") or not row.get("likely_hoa") or row["action"] not in ("build", "build_fallback"):
        return row
    return row | {"held_action": row["action"], "action": "likely_hoa",
                  "reason": f"likely HOA: {row.get('hoa_basis')} ({row.get('hoa_name')}, PIN {row.get('hoa_pin')}); "
                            f"would be {row['reason']}"}


def land_hold(row: dict, land: dict | None, routing: dict, verdict: dict | None = None) -> dict:
    """Your verdict, then the land checks, on one scores row (decided 2026-11-09). An actionable row
    goes to check_land when the assessor discounts its land (assessor_discount), a mapped creek
    crosses a lot under land_checks.creek_max_acres, or a power line is mapped within power_line_ft.
    Your verdict "ok" lets the action stand; "no" skips it with your note (an exclusion still wins).
    `land`: {land_psf, neighbor_land_psf, neighbor_count, stream_length_ft, power_line_ft}."""
    if verdict:
        row = row | {"land_check": verdict["verdict"], "land_check_note": verdict.get("note")}
    if row["action"] not in ACTIONABLE or not (land or verdict):
        return row
    row = row | (land or {})
    ad, lc = routing.get("assessor_discount") or {}, routing.get("land_checks") or {}
    why = []
    own, nbr, n = row.get("land_psf"), row.get("neighbor_land_psf"), row.get("neighbor_count") or 0
    if ad and own is not None and nbr and n >= ad["min_neighbors"]:
        row["land_value_ratio_pct"] = pct = own / nbr * 100
        if pct < ad["min_ratio_pct"]:
            row["assessor_discount"] = True
            why.append(f"assessed land ${own:,.2f}/sf is {pct:.0f}% of the ${nbr:,.2f}/sf median of {n} single-family "
                       f"lots within {ad['radius_ft']:,} ft (< {ad['min_ratio_pct']}%)")
    if lc.get("creek_max_acres") is not None and (row.get("stream_length_ft") or 0) > 0 \
            and row["acres"] < lc["creek_max_acres"]:
        row["creek_on_lot"] = True
        why.append(f"a mapped creek crosses the lot ({row['stream_length_ft']:,.0f} ft; lot under "
                   f"{lc['creek_max_acres']:g} ac)")
    if lc.get("power_line_ft") is not None and row.get("power_line_ft") is not None \
            and row["power_line_ft"] <= lc["power_line_ft"]:
        row["power_line_near"] = True
        why.append(f"a transmission line is mapped {row['power_line_ft']:,.0f} ft away (<= {lc['power_line_ft']:,} ft)")
    if verdict and verdict["verdict"] == "no":
        return row | {"held_action": row["action"], "action": "skip",
                      "reason": f"skip: your land check {verdict.get('checked')}: {verdict.get('note')}; "
                                f"would be {row['reason']}"}
    if not why or (verdict and verdict["verdict"] == "ok"):
        return row
    return row | {"held_action": row["action"], "action": "check_land",
                  "reason": f"check land: {'; '.join(why)}; would be {row['reason']}"}


LAND_KEYS = ("land_psf", "neighbor_land_psf", "neighbor_count", "stream_length_ft", "power_line_ft")
LAND_SQL = """
SELECT p.pin, f.land_value / NULLIF(ST_Area(p.geom), 0) AS land_psf, nb.med AS neighbor_land_psf, nb.n AS neighbor_count,
       c.stream_length_ft,
       (SELECT min(ST_Distance(cf.geom, p.geom)) FROM {s}.constraint_features cf
        WHERE cf.layer = 'power_lines' AND ST_DWithin(cf.geom, p.geom, :power_ft)) AS power_line_ft
FROM {s}.parcels p JOIN {s}.parcel_features f USING (pin)
LEFT JOIN {s}.parcel_constraints c USING (pin)
LEFT JOIN LATERAL (
    SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY nf.land_value / ST_Area(n.geom)) AS med, count(*) AS n
    FROM {s}.parcels n JOIN {s}.parcel_features nf ON nf.pin = n.pin
    WHERE n.pin <> p.pin AND ST_DWithin(n.geom, p.geom, :radius) AND nf.land_value > 0
      AND nf.land_use LIKE ANY(:uses) AND ST_Area(n.geom) > 0) nb ON true
WHERE p.pin = ANY(:pins)
"""


# ---------------------------------------------------------------- run over every parcel

RLV_COLS = ["pin", "site_address", "municipality", "area_sf", "scored_on", "price", "score", "exclusion_category",
            "excluded_reason", "redevelopment_watch", "verify_sewer", "no_value_reason", "upper_bound_option",
            "upper_bound_rlv", "blank_inputs", "rail_on_parcel", "rear_lot_with_house", "rear_lot_front_pin"]
YIELD_COLS = ["pin", "jurisdiction", "base_district", "net_sf", "if_crossed_net_sf", "lot_width_ft", "townhome_units",
              "townhome_needs_rezoning", "townhome_target_district", "if_crossed_townhome_units", "no_frontage",
              "creek_divides", "apartments_possible", "place_type"]
HOA_COLS = ["pin", "likely_hoa", "hoa_basis", "hoa_name", "hoa_pin", "plat_year", "pre_1980_subdivision"]
OPT_COLS = ["pin", "option", "sewer", "kind", "product", "units", "rlv", "unit_price", "blank_inputs",
            "price_per_sf", "revenue", "build_cost"]


def townhome_candidates(p: dict, partners: list[dict], county: str) -> list[dict]:
    """Townhome partners whose place, size and sewer limits the parcel is inside, if it has townhome yield."""
    if p.get("exclusion_category") or not (p.get("townhome_units") or p.get("if_crossed_townhome_units")):
        return []
    return [pt for pt in partners if "townhome" in pt["products"] and not place_misses(pt, p, county)]


def run_route(engine: Engine, partners_cfg: dict, box: dict, econ: dict, scoring_cfg: dict, rules: dict,
              ratios: pd.DataFrame, land: pd.DataFrame | None, run_date: date, s: str = "public",
              chunk: int = 10_000, verdicts: dict[str, dict] | None = None) -> tuple[pd.DataFrame, dict]:
    """Route every parcel in parcel_rlv; store scores and scores_partner_options. Returns (scores, changes).
    `verdicts`: your manual land checks (load_verdicts)."""
    partners, routing = partners_cfg["partners"], partners_cfg["routing"]
    county = routing["parcel_county"]
    with engine.connect() as conn:
        pins = [r[0] for r in conn.execute(text(f"SELECT pin FROM {s}.parcel_rlv ORDER BY pin"))]
        bands = {r["lot_band"]: r for r in R._records(pd.read_sql(text(f"SELECT * FROM {s}.comp_lot_bands"), conn))}
        has_hoa = conn.execute(text(f"SELECT to_regclass('{s}.parcel_hoa') IS NOT NULL")).scalar_one()
    ranges = [(pins[i], pins[min(i + chunk, len(pins)) - 1]) for i in range(0, len(pins), chunk)] or [("", "")]
    input_sql = R.INPUT_SQL.replace("y.pin BETWEEN :lo AND :hi", "y.pin = ANY(:pins)")
    with engine.begin() as conn:
        for i, (lo, hi) in enumerate(ranges):
            bounds = {"lo": lo, "hi": hi}
            read = lambda sql: R._records(pd.read_sql(text(sql), conn, params=bounds))
            rlv = read(f"SELECT {', '.join(RLV_COLS)} FROM {s}.parcel_rlv WHERE pin BETWEEN :lo AND :hi")
            ys = {r["pin"]: r for r in read(f"SELECT {', '.join(YIELD_COLS)} FROM {s}.parcel_yield "
                                            "WHERE yield_basis = 'by_right' AND pin BETWEEN :lo AND :hi")}
            opts: dict[str, list[dict]] = {}
            for o in read(f"SELECT {', '.join(OPT_COLS)} FROM {s}.parcel_rlv_options WHERE pin BETWEEN :lo AND :hi"):
                opts.setdefault(o["pin"], []).append(o)
            hoa = {r["pin"]: r for r in read(f"SELECT {', '.join(HOA_COLS)} FROM {s}.parcel_hoa "
                                             "WHERE pin BETWEEN :lo AND :hi")} if has_hoa else {}
            parcels = [r | ys.get(r["pin"], {}) | hoa.get(r["pin"], {}) | {"acres": r["area_sf"] / R.SQFT_PER_ACRE}
                       for r in rlv]

            # townhome partners: recompute at their widths, for parcels inside their limits
            wanted = {p["pin"]: townhome_candidates(p, partners, county) for p in parcels}
            wanted = {k: v for k, v in wanted.items() if v}
            prows: dict[str, dict[str, list[dict]]] = {}
            if wanted:
                ids = {"pins": list(wanted)}
                inputs = {r["pin"]: r for r in R._records(pd.read_sql(
                    text(input_sql.format(s=s)), conn, params=ids | {"puv": econ["land_price"]["puv_exemption"]}))}
                rd = lambda t: pd.read_sql(text(f"SELECT * FROM {s}.{t} WHERE pin = ANY(:pins)"), conn, params=ids)
                comps = R.comps_by_pin(rd("parcel_comps"), rd("parcel_comps_lot"), bands)
                for pin, pts in wanted.items():
                    prows[pin] = {pt["name"]: partner_options(inputs[pin], ys[pin], comps.get(pin, {}), rules, econ,
                                                              scoring_cfg, ratios, land, pt) for pt in pts}
            rows = [route_parcel(p, [o for o in opts.get(p["pin"], []) if o["sewer"] == p["scored_on"]],
                                 prows.get(p["pin"], {}), partners, box, routing) for p in parcels]
            rows = [hoa_hold(r, box) for r in rows]
            # land checks only for parcels that would be acted on (a few thousand spatial lookups)
            ad, lc = routing.get("assessor_discount") or {}, routing.get("land_checks") or {}
            act = [r["pin"] for r in rows if r["action"] in ACTIONABLE]
            lands = {r["pin"]: {k: r[k] for k in LAND_KEYS} for r in R._records(pd.read_sql(
                text(LAND_SQL.format(s=s)), conn, params={
                    "pins": act, "radius": ad.get("radius_ft", 0), "uses": list(ad.get("neighbor_land_uses", [])),
                    "power_ft": lc.get("power_line_ft") or 0}))} if act else {}
            rows = [land_hold(r, lands.get(r["pin"]), routing, (verdicts or {}).get(r["pin"])) for r in rows]
            extra = [o for d in prows.values() for rs in d.values() for o in rs]
            R._store(conn, R._frame(rows, SCORES_COLS), f"{s}.scores", ["pin"], run_date, append=i > 0, finish=False)
            R._store(conn, R._frame(extra, PARTNER_COLS), f"{s}.scores_partner_options",
                     ["pin", "partner", "option", "sewer"], run_date, append=i > 0, finish=False)
            del rlv, ys, opts, parcels, prows, rows, extra
            gc.collect()
        changes = {t: R.apply_versioned(conn, f"{s}.{t}", key, run_date)
                   for t, key in (("scores", ["pin"]), ("scores_partner_options", ["pin", "partner", "option", "sewer"]))}
        df = pd.read_sql(text(f"SELECT * FROM {s}.scores"), conn)
    return df, changes


def summary(df: pd.DataFrame, changes: dict) -> str:
    counts = df["action"].value_counts()
    lines = [f"Routing: {len(df):,} parcels with by-right yield",
             "  actions: " + ", ".join(f"{a} {int(counts.get(a, 0)):,}" for a in ACTIONS),
             "  flags: " + ", ".join(f"{f} {int(df[f].sum()):,}" for f in FLAGS),
             f"  changes: {changes}"]
    return "\n".join(lines)


def examples(df: pd.DataFrame, n: int = 5) -> list[str]:
    """n parcels per action: the highest-scoring ones (excluded: the largest)."""
    out = []
    for a in ACTIONS:
        d = df[df["action"] == a]
        d = d.sort_values("acres" if a in ("excluded", "skip") else "score", ascending=False).head(n)
        out.append(f"\n{a} ({int((df['action'] == a).sum()):,})")
        for r in d.itertuples():
            flags = ", ".join(f for f in FLAGS if getattr(r, f))
            out.append(f"  {r.pin}  {r.site_address or '(no address)'}  {r.acres:,.2f} ac  {r.reason}"
                       + (f"  [{flags}]" if flags else ""))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.parse_args()
    engine = get_engine()
    econ, scoring_cfg = R.load_config()
    partners_cfg, box = load_config()
    with engine.connect() as conn:
        ratios = pd.read_sql(text("SELECT municipality, land_class, sales, median_ratio, usable FROM sales_ratios"), conn)
        land = pd.read_sql(text("SELECT municipality, acre_band, sales, median_per_acre, usable FROM land_prices"), conn)
        has_rlv = conn.execute(text("SELECT to_regclass('public.parcel_rlv') IS NOT NULL")).scalar_one()
    if not has_rlv:
        print("No parcel_rlv yet: run `make rlv` (needs the core costs in config/economics.yaml).")
        return
    df, changes = run_route(engine, partners_cfg, box, econ, scoring_cfg, load_rules(), ratios, land, date.today(),
                            verdicts=load_verdicts())
    print(summary(df, changes))
    print("\n".join(examples(df)))


if __name__ == "__main__":
    main()
