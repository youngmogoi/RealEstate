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

Townhome partners: for a parcel inside a townhome partner's area, acreage and sewer limits, the
townhome yield and its RLV are recomputed at that partner's unit width (same land, same rule as
parcel_yield) and stored with every input in scores_partner_options. The best by-right use (and
the best rezoning and if-crossed values) is the best of the stored options (townhomes at the
scoring.yaml width) and the partner versions that fit their partner's density and land price limits.

Product of an option: sub_* = subdivision, townhome = townhome, cur_/split_<type> = <type> (sfh,
duplex, triplex, quadraplex); if_crossed_ options as the option they cross to.

Flags stored per parcel: verify_sewer (parcel_rlv), no_frontage, creek_divides, apartments_possible
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
ACTIONS = ["excluded", "build", "route", "build_fallback", "entitle", "crossing_candidate", "skip"]
TOWNHOME_OPTIONS = ("townhome", "if_crossed_townhome")

SCORES_COLS = {
    "pin": "str", "site_address": "str", "municipality": "str", "acres": "float", "action": "str",
    "option": "str", "option_kind": "str", "product": "str", "units": "int", "rlv": "float", "price": "float",
    "score": "float", "partner": "str", "partner_payment": "str", "unit_width_ft": "float",
    "matched_partners": "str", "best_option": "str", "best_rlv": "float", "best_partner": "str",
    "exclusion_category": "str", "redevelopment_watch": "bool",
    "verify_sewer": "bool", "no_frontage": "bool", "creek_divides": "bool", "apartments_possible": "bool",
    "reason": "str",
}
PARTNER_COLS = {"partner": "str", "unit_width_ft": "float"} | R.OPTION_COLS
FLAGS = ["verify_sewer", "no_frontage", "creek_divides", "apartments_possible"]


def load_config() -> tuple[dict, dict]:
    read = lambda name: yaml.safe_load((ROOT / "config" / name).read_text(encoding="utf-8"))
    return read("partners.yaml"), read("my_buybox.yaml")


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


def my_misses(box: dict, p: dict, o: dict, county: str) -> list[str]:
    """Why an option is outside my buy box (empty = Build)."""
    out = place_misses(box, p, county)
    if product(o["option"]) not in box["products"]:
        out.insert(0, f"{product(o['option'])} not in my products")
    if box.get("max_units_per_parcel") is not None and o["units"] > box["max_units_per_parcel"]:
        out.append(f"{o['units']} units > {box['max_units_per_parcel']}")
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
        "redevelopment_watch": bool(p.get("redevelopment_watch"))} | {f: bool(p.get(f)) for f in FLAGS}

    def done(action: str, o: dict | None, reason: str, **extra) -> dict:
        if o:
            row.update(option=o["option"], option_kind=o["kind"], product=product(o["option"]), units=o["units"],
                       rlv=o["rlv"], score=None if p.get("price") is None else o["rlv"] - p["price"],
                       unit_width_ft=o.get("unit_width_ft"))
        return row | {"action": action, "reason": reason} | extra

    if p.get("exclusion_category"):
        return done("excluded", None, f"excluded: {p['exclusion_category']} ({p.get('excluded_reason')})"
                    + ("; on the redevelopment watch list" if p.get("redevelopment_watch") else ""))

    valued = [o for o in options if o.get("rlv") is not None]
    # a partner's own-width townhome counts as a use only where that partner's limits fit it
    by_name = {pt["name"]: pt for pt in partners}
    tagged = valued + [o for name, rows in partner_rows.items() for o in rows
                       if o.get("rlv") is not None and not partner_misses(by_name[name], p, o)]
    by_kind = lambda kind, rows: [o for o in rows if o["kind"] == kind]
    top = lambda rows: max(rows, key=lambda o: o["rlv"]) if rows else None
    b = top(by_kind("by_right", tagged))
    if b is None:
        return done("skip", None, f"no by-right value ({p.get('no_value_reason') or 'no valued option'})")
    row |= {"best_option": b["option"], "best_rlv": b["rlv"], "best_partner": b.get("partner")}
    fam = product(b["option"])
    partner_products = {x for pt in partners for x in pt["products"]}
    mine = top([o for o in by_kind("by_right", valued) if product(o["option"]) in box["products"]])
    fmt = lambda o: f"{o['option']} ({o['units']} units, RLV {_money(o['rlv'])})"

    # 1. Build: my product is the best use and fits my buy box
    if fam in box["products"]:
        misses = my_misses(box, p, b, county)
        if not misses:
            return done("build", b, f"build: {fmt(b)}, price {_money(p.get('price'))}, "
                                    f"score {_money(b['rlv'] - p['price'])}")
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


# ---------------------------------------------------------------- run over every parcel

RLV_COLS = ["pin", "site_address", "municipality", "area_sf", "scored_on", "price", "score", "exclusion_category",
            "excluded_reason", "redevelopment_watch", "verify_sewer", "no_value_reason"]
YIELD_COLS = ["pin", "jurisdiction", "base_district", "net_sf", "if_crossed_net_sf", "lot_width_ft", "townhome_units",
              "townhome_needs_rezoning", "townhome_target_district", "if_crossed_townhome_units", "no_frontage",
              "creek_divides", "apartments_possible"]
OPT_COLS = ["pin", "option", "sewer", "kind", "product", "units", "rlv"]


def townhome_candidates(p: dict, partners: list[dict], county: str) -> list[dict]:
    """Townhome partners whose place, size and sewer limits the parcel is inside, if it has townhome yield."""
    if p.get("exclusion_category") or not (p.get("townhome_units") or p.get("if_crossed_townhome_units")):
        return []
    return [pt for pt in partners if "townhome" in pt["products"] and not place_misses(pt, p, county)]


def run_route(engine: Engine, partners_cfg: dict, box: dict, econ: dict, scoring_cfg: dict, rules: dict,
              ratios: pd.DataFrame, land: pd.DataFrame | None, run_date: date, s: str = "public",
              chunk: int = 10_000) -> tuple[pd.DataFrame, dict]:
    """Route every parcel in parcel_rlv; store scores and scores_partner_options. Returns (scores, changes)."""
    partners, routing = partners_cfg["partners"], partners_cfg["routing"]
    county = routing["parcel_county"]
    with engine.connect() as conn:
        pins = [r[0] for r in conn.execute(text(f"SELECT pin FROM {s}.parcel_rlv ORDER BY pin"))]
        bands = {r["lot_band"]: r for r in R._records(pd.read_sql(text(f"SELECT * FROM {s}.comp_lot_bands"), conn))}
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
            parcels = [r | ys.get(r["pin"], {}) | {"acres": r["area_sf"] / R.SQFT_PER_ACRE} for r in rlv]

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
    df, changes = run_route(engine, partners_cfg, box, econ, scoring_cfg, load_rules(), ratios, land, date.today())
    print(summary(df, changes))
    print("\n".join(examples(df)))


if __name__ == "__main__":
    main()
