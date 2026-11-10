"""Backtest of the residual land value model (built 2026-10-28): model RLV at the time of a land
sale vs the price actually paid.

Run with `make backtest`. Reads config/backtest.yaml (which sales) and config/economics.yaml
(never changed here). Writes reports/backtest_<date>.md (printed too) and, separately,
reports/backtest_townhomes_<date>.md (a rough per-lot comparison).

Sales tested (decided 2026-10-28): land sold as vacant in the last window_months of the sales file
whose account now has one building, built in or after the sale year (proxy for "later got a
building permit"); arm's-length or a land-only validity code (backtest.yaml); deeds that convey one
parcel; built as a single-family house or a duplex/triplex/quadraplex.

RLV at the time of sale: each parcel is valued as vacant (no demolition; teardowns are flagged
because the old house's size is gone from CAMA), on comps rebuilt as of the sale date (new-
construction sales in the comps window before it, never after, never the parcel itself), today's
zoning, parcel shape and economics.yaml, with the price paid as the price (so the loan's land is
what was paid). Compared: the RLV of the option matching what was built (the lot as it is), with
the best-use RLV beside it. Options with blank inputs are upper bounds and counted as such.

Ratio = RLV / price paid: above 1, the model would have paid more than the buyer did.
"""

import argparse
import math
from datetime import date
from pathlib import Path

import pandas as pd
import yaml
from sqlalchemy import Engine, text

import scoring.rlv as R
from ingest.db import get_engine
from scoring.zoning_rules import load_rules

ROOT = Path(__file__).resolve().parent.parent

SALES_SQL = """
WITH deeds AS (SELECT deed_ref, count(DISTINCT pin) AS parcels FROM {s}.parcel_sales
               WHERE deed_ref IS NOT NULL GROUP BY deed_ref)
SELECT DISTINCT ON (c.pin) s.pin AS account_pin, c.pin, s.transfer_id, s.sale_date, s.sale_price, s.validity_desc,
       s.deed_ref, coalesce(d.parcels, 1) AS deed_parcels, c.bldg_type, c.year_built, c.heated_sf AS built_heated_sf
FROM {s}.parcel_sales s
JOIN {s}.parcel_cama c ON c.account_pin = s.pin AND c.building_count = 1
                      AND c.year_built >= extract(year FROM s.sale_date)
LEFT JOIN deeds d ON d.deed_ref = s.deed_ref
WHERE s.sale_price > 0 AND s.sold_as_vacant AND s.sale_date > :start AND s.sale_date <= :as_of
  AND (s.validity_code IS NULL OR s.validity_desc = ANY(:codes))
  AND c.bldg_type = ANY(:types)
  AND (:any_deed OR coalesce(d.parcels, 1) = 1)
ORDER BY c.pin, s.sale_date DESC, s.transfer_id DESC
"""

POINT_SQL = "SELECT pin, ST_X(g) AS x, ST_Y(g) AS y FROM {s}.parcels, ST_PointOnSurface(geom) g WHERE pin = ANY(:pins)"


def load_config() -> dict:
    return yaml.safe_load((ROOT / "config" / "backtest.yaml").read_text(encoding="utf-8"))


def sales_as_of(engine: Engine, s: str = "public") -> date:
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT max(sale_date) FROM {s}.parcel_sales "
                                 "WHERE NOT coalesce(sale_date_after_as_of, false)")).scalar_one()


def comp_pool(engine: Engine, econ: dict, start: date, as_of: date, s: str = "public") -> pd.DataFrame:
    """New-construction comp sales (rlv's rules) between start and as_of, with x, y."""
    c = econ["comps"]
    pairs = [(t, prod) for prod, types in c["products"].items() for t in types]
    sql = (f"SELECT q.account_pin, q.sale_date, q.product, q.price_per_sf, q.sale_price, q.heated_sf, q.lot_sf, "
           f"ST_X(q.geom) AS x, ST_Y(q.geom) AS y FROM ({R.COMP_SALES_SQL.format(s=s)}) q")
    params = {"as_of": as_of, "start": start, "max_age": c["max_age_years"], "min_ppsf": c["min_price_per_sf"],
              "max_ppsf": c["max_price_per_sf"], "types": [t for t, _ in pairs], "products": [p for _, p in pairs]}
    df = pd.read_sql(text(sql), engine, params=params)
    df["sale_date"] = pd.to_datetime(df["sale_date"])
    return df


def comps_at(pin: str, x: float, y: float, account_pin: str, sale_date, pool: pd.DataFrame, econ: dict) -> dict:
    """The comps dict score_parcel takes, as of a sale: comps sold in the comps window BEFORE the
    sale date (no look-ahead), excluding the parcel's own sales."""
    cc = econ["comps"]
    when = pd.Timestamp(sale_date)
    c = pool[(pool["sale_date"] < when) & (pool["sale_date"] > when - pd.DateOffset(months=cc["window_months"]))
             & (pool["account_pin"] != account_pin)]
    t = pd.DataFrame({"pin": [pin], "x": [x], "y": [y]})
    edges = cc["lot_bands_sf"]
    prod = R.comp_stats(t, c, cc)
    sfh = c[c["product"] == "sfh"].assign(product=lambda d: [R.lot_band(v, edges) for v in d["lot_sf"]])
    lot = R.comp_stats(t, sfh, cc | {"products": R.band_labels(edges)}).rename(columns={"product": "lot_band"})
    county = {r["lot_band"]: r for r in R._records(R.county_bands(c, edges))}
    return R.comps_by_pin(prod, lot, county).get(pin, {})


def value_sale(p_in: dict, sale: dict, comps: dict, rules: dict, econ: dict, scoring_cfg: dict,
               ratios: pd.DataFrame, land: pd.DataFrame | None, product: str, options: list[str]) -> dict:
    """One backtest row: the parcel valued as vacant at the price paid, the matching option's RLV."""
    p = p_in | {"vacant": True, "building_value": 0, "existing_heated_sf": None, "asking_price": sale["sale_price"]}
    row, opts = R.score_parcel(p, comps, rules, econ, scoring_cfg, ratios, land)
    cands = [o for o in opts if o["sewer"] == row["scored_on"] and o["option"] in options and o.get("rlv") is not None]
    o = max(cands, key=lambda x: x["rlv"]) if cands else None
    teardown = sale.get("validity_desc") == "DEMO SALE"
    rlv = o["rlv"] if o else None
    return {
        "pin": sale["pin"], "site_address": row.get("site_address"), "sale_date": sale["sale_date"],
        "price_paid": sale["sale_price"], "validity": sale.get("validity_desc") or "arm's length",
        "product": product, "built": f"{sale['bldg_type']} {sale.get('built_heated_sf') or 0:,.0f} sf ({sale['year_built']})",
        "option": o and o["option"], "rlv": rlv, "complete": bool(o) and not o.get("blank_inputs"),
        "blank_inputs": o and o.get("blank_inputs"), "price_tier": o and o.get("price_tier"),
        "price_per_sf": o and o.get("price_per_sf"), "comp_count": o and o.get("comp_count"),
        "comp_radius_mi": o and o.get("comp_radius_mi"), "size_sf": o and o.get("size_sf"),
        "home_price": o and o.get("home_price"), "ratio": None if rlv is None else rlv / sale["sale_price"],
        "best_option": row["best_option"], "best_rlv": row["best_rlv"], "upper_bound_option": row["upper_bound_option"],
        "upper_bound_rlv": row["upper_bound_rlv"], "teardown": teardown,
        "no_match": None if o else "; ".join(
            f"{x['option']}: {('blank ' + x['blank_inputs']) if x.get('blank_inputs') else x.get('notes')}"
            for x in opts if x["sewer"] == row["scored_on"] and x["option"] in options) or "no matching option",
        "_row": row, "_option": o,
    }


def stats(df: pd.DataFrame) -> dict:
    """n, complete, median and spread of RLV / price paid, and absolute % errors."""
    r = df["ratio"].dropna()
    err = (r - 1).abs()
    q = lambda v, k: None if v.empty else float(v.quantile(k))
    return {"n": int(len(r)), "complete": int(df.loc[df["ratio"].notna(), "complete"].sum()),
            "median": q(r, 0.5), "p25": q(r, 0.25), "p75": q(r, 0.75),
            "median_abs_err_pct": None if err.empty else float(err.median() * 100),
            "mean_abs_err_pct": None if err.empty else float(err.mean() * 100)}


def miss_size(ratio: float) -> float:
    """How far off a ratio is, symmetric for over and under (|ln ratio|; RLV <= 0 counts as worst)."""
    return math.inf if ratio is None or ratio <= 0 else abs(math.log(ratio))


def townhome_residual(tc: dict | None, per_lot: float, econ: dict, tiers: list[float]) -> dict:
    """One townhome unit's residual before site work, from townhome comps: price - build - soft -
    selling - profit - financing (loan land = the price paid per lot). Rough: no zoning cap, no
    absorption, no site work."""
    if not tc or tc.get("median_price_per_sf") is None or pd.isna(tc.get("median_price_per_sf")):
        return {"residual": None, "note": "no townhome comps"}
    costs, blanks = econ["costs"], []
    ppsf, sf = tc["median_price_per_sf"], tc["median_heated_sf"]
    tier = R.price_tier(ppsf, tiers)
    tier_rate = ((costs.get("build_cost_per_sf_by_tier") or {}).get("townhome") or {}).get(tier)
    rate = tier_rate if tier_rate is not None else costs["build_cost_per_sf"].get("townhome")
    if rate is None:
        return {"residual": None, "note": "no townhome build cost"}
    price, build = ppsf * sf, rate * sf
    soft = build * costs["soft_cost_pct"] / 100
    sell_pct = (econ.get("selling") or {}).get("selling_cost_pct")
    selling = price * sell_pct / 100 if sell_pct is not None else None
    if selling is None:
        blanks.append("selling_cost_pct")
    profit = price * costs["target_profit_pct"] / 100
    fin = R.financing(build, soft, per_lot, econ.get("financing") or {}, blanks)
    residual = price - build - soft - (selling or 0) - profit - (fin["financing_cost"] or 0)
    return {"residual": residual, "unit_price": price, "unit_sf": sf, "price_per_sf": ppsf, "price_tier": tier,
            "comp_count": tc.get("comp_count"), "blank_inputs": ", ".join(blanks) or None}


# ---------------------------------------------------------------- report

def _pct(x) -> str:
    return "-" if x is None or pd.isna(x) else f"{x:,.0f}%"


def _r(x) -> str:
    return "-" if x is None or pd.isna(x) else f"{x:.2f}"


def stats_table(groups: list[tuple[str, pd.DataFrame]]) -> list[str]:
    out = ["| group | sales | complete | median RLV/paid | p25 | p75 | median abs error | mean abs error |",
           "|---|---|---|---|---|---|---|---|"]
    for name, d in groups:
        s = stats(d)
        out.append(f"| {name} | {s['n']} | {s['complete']} | {_r(s['median'])} | {_r(s['p25'])} | {_r(s['p75'])} | "
                   f"{_pct(s['median_abs_err_pct'])} | {_pct(s['mean_abs_err_pct'])} |")
    return out


def tier_groups(df: pd.DataFrame, tiers: list[float]) -> list[tuple[str, pd.DataFrame]]:
    labels = [R.price_tier(e, tiers) for e in tiers]
    return [(f"comps {t} $/sf", df[df["price_tier"] == t]) for t in labels]


def implied_build_cost(r: pd.Series, soft_pct: float) -> float | None:
    """The build cost per sf at which this sale's RLV would equal the price paid, all else as
    configured (house model: RLV falls by sf x buildings x (1 + soft %) per $1/sf of build cost)."""
    o = r["_option"]
    if not o or not o.get("size_sf") or not o.get("build_cost"):
        return None
    sf = o["size_sf"] * o["buildings"]
    return o["build_cost"] / sf + (r["rlv"] - r["price_paid"]) / (sf * (1 + soft_pct / 100))


def calibration(valued: pd.DataFrame, econ: dict, tiers: list[float]) -> list[str]:
    """Hints for economics.yaml, never applied: the build cost that would make RLV = price paid, and
    price paid / the comps' home price (compare lot_to_home_ratio), by comp $/sf tier."""
    v = valued.assign(implied=[implied_build_cost(r, econ["costs"]["soft_cost_pct"]) for _, r in valued.iterrows()],
                      paid_to_home=valued["price_paid"] / valued["home_price"])
    out = ["", "## Calibration hints (nothing is changed)", "",
           "Implied build cost = the $/sf at which the median sale's RLV equals the price paid, with every other "
           "input as configured (blank inputs still left out, so with financing filled in it would be lower). "
           "Paid / home = price paid / the comps' home price, the lot share builders actually paid.", "",
           "| comps $/sf | sales | configured build $/sf | implied build $/sf (median) | p25 | p75 | paid / home |",
           "|---|---|---|---|---|---|---|"]
    for name, d in [("all", v)] + [(t, v[v["price_tier"] == t]) for t in (R.price_tier(e, tiers) for e in tiers)]:
        if d.empty:
            continue
        rate = (d["_option"].map(lambda o: o["build_cost"] / (o["size_sf"] * o["buildings"]))).median()
        out.append(f"| {name} | {len(d)} | {rate:,.0f} | {d['implied'].median():,.0f} | {d['implied'].quantile(.25):,.0f} | "
                   f"{d['implied'].quantile(.75):,.0f} | {d['paid_to_home'].median():.2f} |")
    out.append("")
    out.append(f"Configured lot_to_home_ratio: {econ['subdivision'].get('lot_to_home_ratio')}. Teardowns: median "
               f"ratio {_r(v.loc[v['teardown'], 'ratio'].median())} vs {_r(v.loc[~v['teardown'], 'ratio'].median())} "
               "for vacant lots (teardowns carry no demolition cost here).")
    return out + [""]


def report(df: pd.DataFrame, cfg: dict, econ: dict, as_of: date, start: date, counts: dict) -> str:
    tiers = econ["comps"]["price_tiers_per_sf"]
    valued = df[df["ratio"].notna()]
    blanks = valued["blank_inputs"].dropna().str.split(", ").explode().value_counts()
    lines = [f"# RLV backtest, {date.today()}", "",
             f"Land sales {start} to {as_of} (sales file as of {as_of}), sold as vacant, one parcel per deed, "
             f"arm's length or {', '.join(cfg['validity_codes_kept'])}, and now built on (CAMA: one building, built in "
             "or after the sale year; proxy for a building permit). Each valued as vacant on comps as of its sale "
             "date, today's zoning and economics.yaml, at the price paid. Ratio = RLV of the option matching what "
             "was built / price paid (above 1: the model would have paid more).", "",
             f"Sales found {counts['found']}; with by-right yield {counts['with_yield']} (without: "
             + ", ".join(f"{k} {v}" for k, v in counts["no_yield"].items()) + f"); valued {len(valued)}; "
             f"no matching value {counts['no_match']}. Teardowns (demolition not costed): {int(valued['teardown'].sum())}.",
             ""]
    if len(valued) and valued["complete"].sum() < len(valued):
        lines += [f"**{len(valued) - int(valued['complete'].sum())} of {len(valued)} RLVs are upper bounds** (blank "
                  "inputs left out): " + ", ".join(f"{k} {v}" for k, v in blanks.items()) + ".", ""]
    lines += ["## RLV / price paid", ""]
    lines += stats_table([("all", valued)] + [(p, valued[valued["product"] == p]) for p in cfg["built_products"]]
                         + tier_groups(valued, tiers))
    lines += ["", "Complete RLVs only:", ""]
    comp = valued[valued["complete"]]
    lines += stats_table([("all", comp)] + [(p, comp[comp["product"] == p]) for p in cfg["built_products"]]
                         + tier_groups(comp, tiers))
    lines += ["", "Best-use RLV / price paid (the best complete by-right option, often a split): median "
              f"{_r((valued['best_rlv'] / valued['price_paid']).median())}.", ""]

    lines += calibration(valued, econ, tiers)
    worst = valued.assign(miss=valued["ratio"].map(miss_size)).sort_values("miss", ascending=False).head(cfg["worst_n"])
    lines += [f"## {cfg['worst_n']} worst misses", "",
              "By |ln(RLV / paid)|. Each block is rlv.explain for the parcel valued at the sale; its \"best use\" "
              "line is the option matching what was built.", ""]
    for i, (_, r) in enumerate(worst.iterrows(), 1):
        lines += [f"### {i}. {r['pin']} {r['site_address'] or ''}: RLV {R._money(r['rlv'])} vs paid "
                  f"{R._money(r['price_paid'])} (ratio {_r(r['ratio'])})", "",
                  f"Sold {r['sale_date']} ({r['validity']}); built {r['built']}; comps {r['price_tier']} $/sf.", "", "```"]
        lines += R.explain(pd.Series(r["_row"]), pd.Series(r["_option"]), econ)
        lines += ["```", ""]
    nm = df[df["ratio"].isna()]
    if len(nm):
        lines += ["## Sales with no matching value", ""]
        lines += [f"- {r.pin} {r.site_address or ''}, paid {R._money(r.price_paid)}: {r.no_match}" for r in nm.itertuples()]
    return "\n".join(lines) + "\n"


def townhome_report(th: pd.DataFrame, econ: dict, as_of: date, start: date) -> str:
    tiers = econ["comps"]["price_tiers_per_sf"]
    v = th[th["ratio"].notna()]
    lines = [f"# Townhome lots: rough per-lot backtest, {date.today()}", "",
             f"Townhome lots sold as vacant {start} to {as_of} and now built (CAMA TOWNHOUSE), one row per deed. "
             "Price per lot = deed price / parcels on the deed. Model = one townhome unit's residual before site "
             "work (nearby townhome comps as of the sale, house-model costs, loan land = price per lot). Rough: no "
             "zoning cap, absorption or site work, so it overstates what a lot is worth.", "",
             f"Deeds {len(th)}; valued {len(v)}.", ""]
    lines += stats_table([("all", v)] + tier_groups(v, tiers))
    lines += ["", "| deed | first pin | lots | paid per lot | unit residual | ratio | comps $/sf | blank |",
              "|---|---|---|---|---|---|---|---|"]
    for r in v.sort_values("ratio").itertuples():
        lines.append(f"| {r.deed_ref} | {r.pin} | {r.deed_parcels} | {R._money(r.price_paid)} | {R._money(r.rlv)} | "
                     f"{_r(r.ratio)} | {r.price_tier} | {r.blank_inputs or ''} |")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- run

def run(engine: Engine, cfg: dict, econ: dict, scoring_cfg: dict, rules: dict, ratios: pd.DataFrame,
        land: pd.DataFrame | None, s: str = "public") -> tuple[pd.DataFrame, pd.DataFrame, dict, date, date]:
    as_of = sales_as_of(engine, s)
    start = R.window_start(as_of, cfg["window_months"])
    types = {t: prod for prod, spec in cfg["built_products"].items() for t in spec["bldg_types"]}
    read = lambda params: pd.read_sql(text(SALES_SQL.format(s=s)), engine, params={"start": start, "as_of": as_of} | params)
    sales = read({"codes": cfg["validity_codes_kept"], "types": list(types), "any_deed": not cfg["single_parcel_deeds_only"]})
    th_sales = read({"codes": cfg["townhome"]["validity_codes_kept"], "types": cfg["townhome"]["bldg_types"], "any_deed": True})
    th_sales = th_sales.sort_values(["deed_ref", "pin"]).drop_duplicates("deed_ref")
    pool = comp_pool(engine, econ, R.window_start(start, econ["comps"]["window_months"]), as_of, s)
    pins = list(dict.fromkeys(sales["pin"].tolist() + th_sales["pin"].tolist()))
    input_sql = R.INPUT_SQL.replace("y.pin BETWEEN :lo AND :hi", "y.pin = ANY(:pins)").format(s=s)
    inputs = {r["pin"]: r for r in R._records(pd.read_sql(
        text(input_sql), engine, params={"pins": pins, "puv": econ["land_price"]["puv_exemption"]}))}
    pts = {r["pin"]: r for r in R._records(pd.read_sql(text(POINT_SQL.format(s=s)), engine, params={"pins": pins}))}

    rows = []
    for sale in R._records(sales):
        if sale["pin"] not in inputs:
            continue
        pt = pts[sale["pin"]]
        comps = comps_at(sale["pin"], pt["x"], pt["y"], sale["account_pin"], sale["sale_date"], pool, econ)
        prod = types[sale["bldg_type"]]
        rows.append(value_sale(inputs[sale["pin"]], sale, comps, rules, econ, scoring_cfg, ratios, land, prod,
                               cfg["built_products"][prod]["options"]))
    df = pd.DataFrame(rows)
    missing = [x for x in sales["pin"] if x not in inputs]
    why = pd.read_sql(text(f"SELECT CASE WHEN site_plan_governs THEN 'site plan governs' WHEN no_zoning_rule THEN "
                           f"'no zoning rule' ELSE coalesce(yield_basis, 'none') END AS why, count(*) AS n "
                           f"FROM {s}.parcel_yield WHERE pin = ANY(:pins) GROUP BY 1 ORDER BY 2 DESC"),
                      engine, params={"pins": missing})
    no_yield = dict(zip(why["why"], why["n"].astype(int)))
    if len(missing) > sum(no_yield.values()):
        no_yield["not in parcel_yield"] = len(missing) - sum(no_yield.values())
    counts = {"found": len(sales), "with_yield": len(rows), "no_yield": no_yield,
              "no_match": int(df["ratio"].isna().sum()) if len(df) else 0}

    th_rows = []
    for sale in R._records(th_sales):
        pt = pts.get(sale["pin"])
        if pt is None:
            continue
        comps = comps_at(sale["pin"], pt["x"], pt["y"], sale["account_pin"], sale["sale_date"], pool, econ)
        per_lot = sale["sale_price"] / sale["deed_parcels"]
        res = townhome_residual(comps.get("townhome"), per_lot, econ, econ["comps"]["price_tiers_per_sf"])
        th_rows.append({"deed_ref": sale["deed_ref"], "pin": sale["pin"], "deed_parcels": sale["deed_parcels"],
                        "sale_date": sale["sale_date"], "price_paid": per_lot, "rlv": res["residual"],
                        "ratio": None if res["residual"] is None else res["residual"] / per_lot,
                        "price_tier": res.get("price_tier"), "blank_inputs": res.get("blank_inputs"),
                        "complete": not res.get("blank_inputs")})
    return df, pd.DataFrame(th_rows), counts, as_of, start


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.parse_args()
    engine = get_engine()
    econ, scoring_cfg = R.load_config()
    cfg = load_config()
    missing = R.blank_core(econ)
    if missing:
        print(f"Backtest needs the core costs: fill in {', '.join(missing)} in config/economics.yaml.")
        return
    with engine.connect() as conn:
        ratios = pd.read_sql(text("SELECT municipality, land_class, sales, median_ratio, usable FROM sales_ratios"), conn)
        land = pd.read_sql(text("SELECT municipality, acre_band, sales, median_per_acre, usable FROM land_prices"), conn)
    df, th, counts, as_of, start = run(engine, cfg, econ, scoring_cfg, load_rules(), ratios, land)
    out = ROOT / cfg["report_dir"]
    out.mkdir(exist_ok=True)
    text_main = report(df, cfg, econ, as_of, start, counts)
    (out / f"backtest_{date.today()}.md").write_text(text_main, encoding="utf-8")
    (out / f"backtest_townhomes_{date.today()}.md").write_text(townhome_report(th, econ, as_of, start), encoding="utf-8")
    print(text_main)
    print(f"Saved {cfg['report_dir']}/backtest_{date.today()}.md and backtest_townhomes_{date.today()}.md")


if __name__ == "__main__":
    main()
