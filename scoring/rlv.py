"""Phase 2.3: comps, sales ratios and residual land value (RLV) for every yield option.

Run with `make rlv` (after `make cama` and `make yield`). Settings: config/economics.yaml.
`python -m scoring.rlv --explain <pin>` prints one parcel with every input; `--ratios` prints
the sales ratio table.

Tables (each versioned, history in <table>_history; see ingest/versioning.py):
1. comp_sales: new-construction sales (rules in economics.yaml `comps`), one row per sale, with
   the sold account's CAMA heated sf and building type, $/heated sf, and geom = a point on the
   sold parcel. Product: sfh, townhome, or plex (duplex/triplex/quadraplex pooled).
2. parcel_comps: per parcel with by-right yield and per product: median $/heated sf of the
   comps within 1 mile, widened to 2 then 3 miles while there are fewer than min_comps;
   comp_count, radius_mi, size_cap_sf (the market cap on house size: a percentile of the same
   comps' heated sf) and thin_comps. The comps behind a row are that product's comp_sales within
   radius_mi of the parcel's point on surface (comps_for() lists them).
3. sales_ratios: median sale price / current assessed value by municipality and land class.
4. parcel_rlv_options: one row per parcel, option and sewer case ('public', or 'septic' where
   sewer is unknown outside town limits), holding every input and cost line:
       rlv = revenue - build_cost - soft_cost - profit + adu_premium - site_work - obligation_cost
   A cost whose config value is blank is NULL here and named in blank_inputs; the RLV leaves it
   out (an upper bound). Options: cur_<type> (the lot as it is), split_<type> (2+ lots),
   sub_standard / sub_conservation / sub_compact / sub_mixed_income (2+ lots), townhome (by right,
   or kind 'rezoning'), and if_crossed_* (kind 'if_crossed', plus the creek crossing cost).
5. parcel_rlv: per parcel, the best use (highest by-right RLV), the best rezoning and if-crossed
   options (reported, never the best use), the "if sewer available" RLV, price, score and flags.
   Price = asking price if listed (no listings source yet), else assessed value x sales ratio.
   Score = best RLV - price; blank for parcels never scored as opportunities (economics `score`).

House size per option: footprint = the smaller of the setback envelope and max building
coverage on the option's lot (district minimum lot for subdivision lots; townhome = the unit
footprint in scoring.yaml), times floors (max height / story height, capped), capped by the
market (size_cap_sf). Plex sizes and prices are per building; units share it equally.
"""

import argparse
import importlib
import json
import math
from datetime import date
from pathlib import Path

import pandas as pd
import yaml
from sqlalchemy import Connection, Engine, text

from ingest.db import copy_dataframe, get_engine
from ingest.versioning import apply_versioned, create_staging_from_query, row_hash
from scoring.zoning_rules import load_rules

Y = importlib.import_module("scoring.yield")
ROOT = Path(__file__).resolve().parent.parent
SQFT_PER_ACRE = 43_560
FT_PER_MILE = 5_280
CORE = ["build_cost_per_sf", "soft_cost_pct", "target_profit_pct"]
SUB_COLS = {"sub_standard": "sub_lots", "sub_conservation": "sub_conservation_lots",
            "sub_compact": "sub_compact_lots", "sub_mixed_income": "sub_mixed_income_lots"}
SEPTIC_OPTIONS = ["cur_sfh", "split_sfh", "sub_standard"]

OPTION_COLS = {
    "pin": "str", "option": "str", "sewer": "str", "kind": "str", "product": "str",
    "units": "int", "units_per_building": "int", "buildings": "float", "affordable_units": "int",
    "lot_sf": "float", "lot_width_ft": "float", "lot_depth_ft": "float",
    "envelope_sf": "float", "coverage_sf": "float", "footprint_sf": "float", "height_ft": "float", "stories": "int",
    "zoning_max_sf": "float", "market_cap_sf": "float", "size_sf": "float",
    "comp_radius_mi": "float", "comp_count": "int", "price_per_sf": "float", "thin_comps": "bool",
    "unit_price": "float", "revenue": "float", "build_cost": "float", "soft_cost": "float", "profit": "float",
    "adu_premium": "float", "sewer_ft": "float", "sewer_cost": "float", "road_ft": "float", "road_cost": "float",
    "stormwater_cost": "float", "slope_acres": "float", "slope_cost": "float", "floodplain_units": "int",
    "floodplain_cost": "float", "crossing_cost": "float", "site_work": "float", "obligation_cost": "float",
    "rlv": "float", "blank_inputs": "str", "notes": "str",
}
PARCEL_COLS = {
    "pin": "str", "site_address": "str", "jurisdiction": "str", "zone_code": "str", "municipality": "str",
    "area_sf": "float", "sewer_basis": "str", "sewer_status": "str", "sewer_confidence": "str", "sewer_source": "str",
    "scored_on": "str", "best_option": "str", "best_product": "str", "best_units": "int", "best_rlv": "float",
    "if_sewer_option": "str", "if_sewer_rlv": "float", "rezoning_option": "str", "rezoning_rlv": "float",
    "if_crossed_option": "str", "if_crossed_rlv": "float",
    "asking_price": "float", "assessed_value": "float", "land_class": "str", "ratio": "float", "ratio_group": "str",
    "ratio_sales": "int", "price": "float", "price_basis": "str", "score": "float", "excluded_reason": "str",
    "verify_sewer": "bool", "sewer_low_confidence": "bool", "sewer_unverified_high_score": "bool",
    "thin_comps": "bool", "plex_pooled": "bool", "rlv_incomplete": "bool", "blank_inputs": "str",
    "verify_acreage": "bool", "septic_soils_very_limited": "bool", "no_value_reason": "str",
}
DTYPES = {"str": "object", "int": "Int64", "float": "float64", "bool": "boolean"}


def load_config() -> tuple[dict, dict]:
    read = lambda name: yaml.safe_load((ROOT / "config" / name).read_text(encoding="utf-8"))
    return read("economics.yaml"), read("scoring.yaml")


def blank_core(econ: dict) -> list[str]:
    """Core cost settings still blank; RLV can't be computed until they are filled in."""
    return [k for k in CORE if econ["costs"].get(k) is None]


def window_start(as_of: date, months: int) -> date:
    return (pd.Timestamp(as_of) - pd.DateOffset(months=months)).date()


def _frame(rows: list[dict], cols: dict) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=list(cols))
    for c, t in cols.items():
        df[c] = df[c].astype(DTYPES[t])
    return df


def _store(conn: Connection, df: pd.DataFrame, table: str, key: list[str], run_date: date, append: bool = False,
           finish: bool = True) -> dict | None:
    copy_dataframe(conn, df.assign(row_hash=row_hash(df)), f"{table}_staging", append=append)
    return apply_versioned(conn, table, key, run_date) if finish else None


# ---------------------------------------------------------------- 1-2. comps

COMP_SALES_SQL = """
SELECT s.pin AS account_pin, s.transfer_id, s.property_id, c.pin, s.sale_date, s.sale_price, c.heated_sf,
       c.bldg_type, c.year_built, m.product, s.sale_price / c.heated_sf AS price_per_sf,
       ST_PointOnSurface(sh.geom) AS geom
FROM (SELECT DISTINCT ON (pin) * FROM {s}.parcel_sales
      WHERE validity_code IS NULL AND sale_price > 0 AND NOT coalesce(sold_as_vacant, false)
        AND sale_date > :start AND sale_date <= :as_of
      ORDER BY pin, sale_date DESC, transfer_id DESC, property_id DESC) s
JOIN {s}.parcel_cama c ON c.account_pin = s.pin AND c.building_count = 1 AND c.heated_sf > 0
JOIN (SELECT unnest(CAST(:types AS text[])) AS bldg_type, unnest(CAST(:products AS text[])) AS product) m
  ON m.bldg_type = c.bldg_type
JOIN {s}.parcels sh ON sh.pin = c.pin
WHERE extract(year FROM s.sale_date) - c.year_built BETWEEN 0 AND :max_age
  AND s.sale_price / c.heated_sf BETWEEN :min_ppsf AND :max_ppsf
ORDER BY s.pin
"""

COMPS_COLS = {"pin": "str", "product": "str", "radius_mi": "float", "comp_count": "int",
              "median_price_per_sf": "float", "size_cap_sf": "float", "thin_comps": "bool"}


def comp_stats(targets: pd.DataFrame, comps: pd.DataFrame, cfg: dict, cap_pct: float, batch: int = 10_000) -> pd.DataFrame:
    """parcel_comps rows. targets: pin, x, y; comps: product, x, y, price_per_sf, heated_sf (EPSG:2264 feet).

    Per product, comps within the first radius; parcels with fewer than min_comps are searched again
    at the next radius, so each parcel keeps the smallest radius with enough comps, else the widest
    (thin_comps). Medians and the size cap are linear-interpolated percentiles (as percentile_cont).
    Done here with a spatial index in batches, not in SQL: 206k parcels x 3 miles is ~80M pairs,
    which Postgres sorted on disk (it filled C: on 2026-10-28)."""
    import numpy as np
    import shapely

    pts = shapely.points(targets["x"].to_numpy(), targets["y"].to_numpy())
    radii, min_n = cfg["radii_miles"], cfg["min_comps"]
    rows = []
    for product in cfg["products"]:
        cg = comps[comps["product"] == product]
        tree = shapely.STRtree(shapely.points(cg["x"].to_numpy(), cg["y"].to_numpy()))
        ppsf, sf = cg["price_per_sf"].to_numpy(), cg["heated_sf"].to_numpy()
        found: dict[int, tuple] = {}
        todo = np.arange(len(targets))
        for r in radii:
            for i in range(0, len(todo), batch):
                b = todo[i:i + batch]
                ti, ci = tree.query(pts[b], predicate="dwithin", distance=r * FT_PER_MILE)
                pairs = pd.DataFrame({"t": b[ti], "ppsf": ppsf[ci], "sf": sf[ci]})
                g = pairs.groupby("t")
                for t, n, med, cap in zip(g.size().index, g.size(), g["ppsf"].median(), g["sf"].quantile(cap_pct)):
                    found[t] = (r, int(n), float(med), float(cap))
            todo = np.array([t for t in todo if t not in found or found[t][1] < min_n], dtype=int)
        for t, pin in enumerate(targets["pin"]):
            r, n, med, cap = found.get(t, (max(radii), 0, None, None))
            rows.append({"pin": pin, "product": product, "radius_mi": r, "comp_count": n,
                         "median_price_per_sf": med, "size_cap_sf": cap, "thin_comps": n < min_n})
    return _frame(rows, COMPS_COLS).sort_values(["pin", "product"]).reset_index(drop=True)


def build_comps(engine: Engine, econ: dict, sales_as_of: date, run_date: date, s: str = "public") -> dict:
    """Rebuild comp_sales and parcel_comps. Returns change counts."""
    c, hs = econ["comps"], econ["house_size"]
    pairs = [(t, prod) for prod, types in c["products"].items() for t in types]
    params = {"as_of": sales_as_of, "start": window_start(sales_as_of, c["window_months"]), "max_age": c["max_age_years"],
              "min_ppsf": c["min_price_per_sf"], "max_ppsf": c["max_price_per_sf"],
              "types": [t for t, _ in pairs], "products": [p for _, p in pairs]}
    with engine.begin() as conn:
        create_staging_from_query(conn, f"{s}.comp_sales_staging", COMP_SALES_SQL.format(s=s), params)
        sales = apply_versioned(conn, f"{s}.comp_sales", ["account_pin", "transfer_id", "property_id"], run_date)
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS comp_sales_geom_idx ON {s}.comp_sales USING gist (geom)"))
        comps = pd.read_sql(text(f"SELECT product, ST_X(geom) AS x, ST_Y(geom) AS y, price_per_sf, heated_sf "
                                 f"FROM {s}.comp_sales ORDER BY account_pin, transfer_id, property_id"), conn)
        targets = pd.read_sql(text(
            f"SELECT y.pin, ST_X(g) AS x, ST_Y(g) AS y FROM {s}.parcel_yield y "
            f"JOIN {s}.parcels p USING (pin), ST_PointOnSurface(p.geom) g WHERE y.yield_basis = 'by_right' ORDER BY y.pin"), conn)
    df = comp_stats(targets, comps, c, hs["market_cap_percentile"])
    with engine.begin() as conn:
        changes = _store(conn, df, f"{s}.parcel_comps", ["pin", "product"], run_date)
    return {"comp_sales": sales, "parcel_comps": changes}


def comps_for(engine: Engine, pin: str, product: str, s: str = "public") -> pd.DataFrame:
    """The comp sales behind a parcel_comps row (for auditing)."""
    sql = f"""SELECT c.account_pin, c.transfer_id, c.sale_date, c.sale_price, c.heated_sf, c.price_per_sf,
                     round((ST_Distance(c.geom, ST_PointOnSurface(p.geom)) / {FT_PER_MILE})::numeric, 2) AS miles
              FROM {s}.parcel_comps pc JOIN {s}.parcels p USING (pin)
              JOIN {s}.comp_sales c ON c.product = pc.product
               AND ST_DWithin(c.geom, ST_PointOnSurface(p.geom), pc.radius_mi * {FT_PER_MILE})
              WHERE pc.pin = :pin AND pc.product = :product ORDER BY c.price_per_sf"""
    return pd.read_sql(text(sql), engine, params={"pin": pin, "product": product})


# ---------------------------------------------------------------- 3. sales ratios

RATIO_SQL = """
SELECT s.pin AS account_pin, s.transfer_id, s.sale_date, s.sale_price, o.total_value AS assessed_value,
       coalesce(o.municipality, '(none)') AS municipality,
       CASE WHEN coalesce(o.building_value, 0) = 0 THEN 'vacant' ELSE 'improved' END AS land_class
FROM {s}.parcel_sales s
JOIN {s}.parcel_owners o ON o.account_pin = s.pin
LEFT JOIN {s}.parcel_cama c ON c.account_pin = s.pin
WHERE s.validity_code IS NULL AND s.sale_price > 0 AND o.total_value > 0
  AND s.sale_date > :start AND s.sale_date <= :as_of
  AND (coalesce(o.building_value, 0) = 0) = coalesce(s.sold_as_vacant, false)
  AND (coalesce(o.building_value, 0) = 0 OR c.year_built_newest < extract(year FROM s.sale_date))
"""
# Land class = parcel_features.vacant (building value 0), not CAMA vac_or_improved: ~7,800 "VAC"
# accounts carry a building with value (checked 2026-10-28).
ALL = "ALL"


def ratio_observations(engine: Engine, price: dict, sales_as_of: date, s: str = "public") -> pd.DataFrame:
    params = {"as_of": sales_as_of, "start": window_start(sales_as_of, price["window_months"])}
    obs = pd.read_sql(text(RATIO_SQL.format(s=s)), engine, params=params)
    return obs.assign(ratio=obs["sale_price"] / obs["assessed_value"])


def ratio_table(obs: pd.DataFrame, min_sales: int) -> pd.DataFrame:
    """Median sale/assessed ratio and sale count by municipality + class, countywide class, countywide."""
    rows = [(m, c, len(g), g["ratio"].median()) for (m, c), g in obs.groupby(["municipality", "land_class"])]
    rows += [(ALL, c, len(g), g["ratio"].median()) for c, g in obs.groupby("land_class")]
    rows.append((ALL, ALL, len(obs), obs["ratio"].median() if len(obs) else None))
    df = pd.DataFrame(rows, columns=["municipality", "land_class", "sales", "median_ratio"])
    df["usable"] = df["sales"] >= min_sales
    return df


def pick_ratio(table: pd.DataFrame, municipality: str | None, land_class: str) -> tuple[float | None, str, int]:
    """(ratio, group, sales) from the most specific usable group."""
    t = table.set_index(["municipality", "land_class"])
    for key in ((municipality or "(none)", land_class), (ALL, land_class), (ALL, ALL)):
        if key in t.index and t.loc[key, "usable"]:
            return float(t.loc[key, "median_ratio"]), f"{key[0]} {key[1]}", int(t.loc[key, "sales"])
    return None, "none", 0


def format_ratios(table: pd.DataFrame, min_sales: int, start: date, end: date) -> str:
    lines = [f"Sales ratio (median sale price / current assessed value), arm's-length sales {start} to {end}",
             f"  {'municipality':<36}{'class':<10}{'sales':>8}{'ratio':>8}   (needs {min_sales}+ sales)"]
    for r in table.itertuples():
        mark = "" if r.usable else "  too few: falls back"
        ratio = f"{r.median_ratio:8.3f}" if pd.notna(r.median_ratio) else "       -"
        lines.append(f"  {r.municipality:<36}{r.land_class:<10}{r.sales:>8,}{ratio}{mark}")
    return "\n".join(lines)


def build_ratios(engine: Engine, econ: dict, sales_as_of: date, run_date: date, s: str = "public") -> tuple[pd.DataFrame, dict]:
    pc = econ["price"]
    table = ratio_table(ratio_observations(engine, pc, sales_as_of, s), pc["min_sales"])
    stored = table.assign(window_start=window_start(sales_as_of, pc["window_months"]), window_end=sales_as_of)
    stored["window_start"] = pd.to_datetime(stored["window_start"])
    stored["window_end"] = pd.to_datetime(stored["window_end"])
    with engine.begin() as conn:
        changes = _store(conn, stored, f"{s}.sales_ratios", ["municipality", "land_class"], run_date)
    return table, changes


# ---------------------------------------------------------------- 4. options

def _height(rule: dict) -> float | None:
    h = rule.get("max_height_ft")
    return h.get("value") if isinstance(h, dict) else h


def _opt(name: str, kind: str, product: str, units: int, upb: int, std: dict, height_rule: dict,
         lot_sf: float | None, width: float | None, depth: float | None, creates_lots: bool, **extra) -> dict:
    return {"option": name, "kind": kind, "product": product, "units": int(units), "units_per_building": upb,
            "std": std, "height_ft": _height(height_rule), "lot_sf": lot_sf, "lot_width_ft": width,
            "lot_depth_ft": depth, "creates_lots": creates_lots, "footprint_sf": None, "affordable_units": 0,
            "adu": False, "notes": []} | extra


def _sub_specs(rules: dict, j: str, rule: dict) -> dict:
    """(lot sf, lot width, standards rule) of a new lot in each subdivision option."""
    types, alts = rule["types"], rule.get("alternatives") or {}
    lot, width = Y.type_standards(rules, j, rule, "sfh") if Y._allowed(types.get("sfh")) else (
        rule["min_lot_sf"]["value"], rule["min_lot_width_ft"]["value"] or 0)
    std = Y.standards_rule(rules, j, rule, "sfh") if Y._allowed(types.get("sfh")) else rule
    specs = {"sub_standard": (lot, width, std, [])}
    if c := alts.get("conservation"):
        cut = 1 - c["lot_area_and_width_reduction_pct"] / 100
        specs["sub_conservation"] = (lot * cut, width * cut, std, ["conservation lots: district setbacks used (4.5.A cuts them)"])
    if c := alts.get("compact"):
        specs["sub_compact"] = (c["min_lot_sf"], c["min_lot_width_ft"],
                                {"setbacks_ft": c["setbacks_ft"], "max_building_coverage_pct": c.get("max_building_coverage_pct")}, [])
    if (c := alts.get("voluntary_mixed_income")) and (target := Y.district_rule(rules, j, c["develop_as"])):
        specs["sub_mixed_income"] = (target["min_lot_sf"]["value"], target["min_lot_width_ft"]["value"], target, [])
    return specs


def yield_options(p: dict, rules: dict, scoring_cfg: dict) -> list[dict]:
    """Every yield option of a parcel (public sewer), with the lot each building sits on."""
    j = p["jurisdiction"]
    rule = Y.district_rule(rules, j, p.get("base_district")) if p.get("yield_basis") == "by_right" else None
    if rule is None:
        return []
    types = rule["types"]
    ob = p.get("obligations") or {}
    ob = json.loads(ob) if isinstance(ob, str) else ob     # a str from compute_yield, a dict from jsonb
    area, width, depth, frontage = p["area_sf"], p.get("lot_width_ft"), p.get("lot_depth_ft"), p.get("frontage_ft")
    split_w = min(width, frontage) if width and frontage else (width or frontage)
    adu_to = ((types.get("adu") or {}).get("requires") or {}).get("accessory_to", [])
    out = []
    for t in Y.TYPES:
        e = types.get(t) or {}
        if not Y._allowed(e):
            continue
        upb, product = e["units_per_building"], "sfh" if t == "sfh" else "plex"
        std = Y.standards_rule(rules, j, rule, t)
        aff = (ob.get(t) or {}).get("affordable_units_per_building", 0)
        if p.get(f"cur_{t}"):
            out.append(_opt(f"cur_{t}", "by_right", product, p[f"cur_{t}"], upb, std, rule, area, width, depth, False,
                            affordable_units=aff * (p[f"cur_{t}"] // upb), adu=bool(p.get("adu_allowed")) and t in adu_to))
        if (p.get("split_lots") or 0) >= 2 and p.get(f"split_{t}"):
            n_lots = p[f"split_{t}"] // upb
            out.append(_opt(f"split_{t}", "by_right", product, p[f"split_{t}"], upb, std, rule, area / n_lots,
                            split_w / n_lots if split_w else None, depth, True, affordable_units=aff * n_lots))

    specs = _sub_specs(rules, j, rule)
    th = scoring_cfg["townhome"]
    for prefix, kind in (("", "by_right"), ("if_crossed_", "if_crossed")):
        for name, col in SUB_COLS.items():
            n = p.get(prefix + col)
            if not n or n < 2 or name not in specs:
                continue
            lot, w, std, notes = specs[name]
            aff = 0
            if name == "sub_mixed_income":
                aff = p["sub_mixed_income_affordable_units"] if not prefix else \
                    Y.mixed_income_affordable(n - (p.get("if_crossed_sub_lots") or 0))
            out.append(_opt(prefix + name, kind, "sfh", n, 1, std, rule, lot, w, lot / w if w else None, True,
                            affordable_units=aff, notes=list(notes)))
        if units := p.get(prefix + "townhome_units"):
            rezoning = bool(p.get("townhome_needs_rezoning"))
            target = Y.district_rule(rules, j, p["townhome_target_district"]) if rezoning else rule
            k = kind if prefix else ("rezoning" if rezoning else "by_right")
            out.append(_opt(prefix + "townhome", k, "townhome", units, 1, target, target, None, None, None, True,
                            footprint_sf=th["unit_width_ft"] * th["unit_depth_ft"],
                            notes=[f"needs rezoning to {p['townhome_target_district']} (speculative)"] if rezoning else []))
    return out


def septic_options(p: dict, rules: dict, scoring_cfg: dict, septic_min_sf: float) -> list[dict]:
    """Single-family options without public sewer: every lot at least septic_min_sf."""
    out = []
    for o in yield_options(p, rules, scoring_cfg):
        if o["option"] not in SEPTIC_OPTIONS or o["kind"] != "by_right":
            continue
        o = o | {"notes": o["notes"] + [f"septic: lots of at least {septic_min_sf:,.0f} sf"]}
        if o["option"] == "cur_sfh":
            if p["area_sf"] >= septic_min_sf:
                out.append(o)
        elif o["option"] == "split_sfh":
            n = min(o["units"], Y._floor(p["area_sf"] / septic_min_sf))
            if n >= 2:
                w = o["lot_width_ft"] * o["units"] / n if o["lot_width_ft"] else None
                out.append(o | {"units": n, "lot_sf": p["area_sf"] / n, "lot_width_ft": w})
        else:
            n = min(o["units"], Y._floor((p.get("net_sf") or 0) * scoring_cfg["yield"]["usable_ratio"] / septic_min_sf))
            if n >= 2:
                lot = max(o["lot_sf"], septic_min_sf)
                out.append(o | {"units": n, "lot_sf": lot,
                                "lot_depth_ft": lot / o["lot_width_ft"] if o["lot_width_ft"] else None})
    return out


def sewer_basis(p: dict) -> str:
    """CLAUDE.md "Unknown sewer": known_distance | proxy_served | assumed_in_town | none_outside."""
    if p.get("sewer_distance_ft") is not None:
        return "known_distance"
    if p.get("sewer_status") == "served":
        return "proxy_served"
    return "assumed_in_town" if p.get("in_municipal_limit") else "none_outside"


def footprint(o: dict) -> tuple[float | None, float | None, float | None]:
    """(setback envelope sf, coverage limit sf, footprint sf) of one building."""
    if o["footprint_sf"]:
        return None, None, o["footprint_sf"]
    sb = o["std"].get("setbacks_ft") or {}
    nums = [sb.get(k) for k in ("front", "side", "rear")]
    w, d = o["lot_width_ft"], o["lot_depth_ft"]
    envelope = None
    if w and d and all(isinstance(x, int | float) for x in nums):
        front, side, rear = nums
        envelope = max(w - 2 * side, 0) * max(d - front - rear, 0)
    coverage = Y.coverage_pct(o["std"], o["lot_sf"]) * o["lot_sf"] / 100 \
        if o["std"].get("max_building_coverage_pct") and o["lot_sf"] else None
    limits = [x for x in (envelope, coverage) if x is not None]
    return envelope, coverage, (min(limits) if limits else None)


def floodplain_units(o: dict, p: dict, house_fp_sf: float | None) -> int:
    """Units whose building falls in the 1% floodplain flag (outside the unbuildable area). One
    building on the lot as it is falls in only if the land outside the flag can't hold it; on new
    lots, the share of units = the flag's share of the net buildable land (rounded down)."""
    net_sf = p.get("net_sf") or 0
    flag_sf = (p.get("floodplain_flag_acres") or 0) * SQFT_PER_ACRE
    if flag_sf <= 0 or net_sf <= 0:
        return 0
    share = min(flag_sf / net_sf, 1)
    if not o["creates_lots"]:
        return o["units"] if house_fp_sf and (1 - share) * net_sf < house_fp_sf else 0
    return Y._floor(o["units"] * share)


def value_option(o: dict, p: dict, comps: dict, econ: dict, basis: str, sewer: str) -> dict:
    """One option's RLV with every input (a parcel_rlv_options row)."""
    costs, sw, hs = econ["costs"], econ["site_work"], econ["house_size"]
    c = comps.get(o["product"]) or {}
    blanks, notes = [], list(o["notes"])

    def item(name: str, qty: float, rate: float | None) -> float | None:
        if not qty:
            return 0.0
        if rate is None:
            blanks.append(name)
            return None
        return qty * rate

    envelope, coverage, fp = footprint(o)
    stories = min(int(o["height_ft"] // hs["story_height_ft"]), hs["max_stories"]) if o["height_ft"] else hs["max_stories"]
    if not o["height_ft"]:
        notes.append("no height limit in the rule: max_stories used")
    zoning_max = fp * stories if fp is not None else None
    cap = c.get("size_cap_sf")
    sizes = [x for x in (zoning_max, cap) if x is not None]
    size = min(sizes) if sizes else None
    if zoning_max is None:
        notes.append("zoning size unknown (no lot width/depth or coverage)")
    upb = o["units_per_building"]
    buildings = o["units"] / upb
    row = {"pin": p["pin"], "option": o["option"], "sewer": sewer, "kind": o["kind"], "product": o["product"],
           "units": o["units"], "units_per_building": upb, "buildings": buildings,
           "affordable_units": o["affordable_units"], "lot_sf": o["lot_sf"], "lot_width_ft": o["lot_width_ft"],
           "lot_depth_ft": o["lot_depth_ft"], "envelope_sf": envelope, "coverage_sf": coverage, "footprint_sf": fp,
           "height_ft": o["height_ft"], "stories": stories, "zoning_max_sf": zoning_max, "market_cap_sf": cap,
           "size_sf": size, "comp_radius_mi": c.get("radius_mi"), "comp_count": c.get("comp_count", 0),
           "price_per_sf": c.get("median_price_per_sf"), "thin_comps": bool(c.get("thin_comps", True))}
    if o["product"] == "plex":
        notes.append("plex comps pool duplex, triplex and quadraplex")
    ppsf = row["price_per_sf"]
    if size is None or ppsf is None or size <= 0:
        notes.append("no comps within the widest radius" if ppsf is None else "no buildable house size")
        return row | {"notes": "; ".join(dict.fromkeys(notes)) or None, "blank_inputs": None}

    revenue = buildings * ppsf * size
    build = buildings * costs["build_cost_per_sf"] * size
    soft = build * costs["soft_cost_pct"] / 100
    profit = revenue * costs["target_profit_pct"] / 100
    unit_price = ppsf * size / upb
    adu = item("adu_premium", 1 if o["adu"] else 0, econ.get("adu_premium"))
    lots = o["creates_lots"] or sw.get("road_and_stormwater_on_lot_as_is")
    sewer_ft = p.get("sewer_distance_ft") if basis == "known_distance" and sewer == "public" else 0
    road_ft = (p.get("frontage_ft") or 0) if lots else 0
    slope_acres = p.get("slope_15_25_acres") or 0
    fp_units = floodplain_units(o, p, size / stories)      # the house's own footprint, not the zoning maximum
    site = {
        "sewer_cost": item("sewer_per_ft", sewer_ft, sw.get("sewer_per_ft")),
        "road_cost": item("road_per_ft", road_ft, sw.get("road_per_ft")),
        "stormwater_cost": item("stormwater_per_unit", o["units"] if lots else 0, sw.get("stormwater_per_unit")),
        "slope_cost": item("slope_per_acre", slope_acres, sw.get("slope_per_acre")),
        "floodplain_cost": item("floodplain_per_unit", fp_units, sw.get("floodplain_per_unit")),
        "crossing_cost": item("creek_crossing", 1 if o["kind"] == "if_crossed" else 0, sw.get("creek_crossing")),
    }
    cap_price = (econ.get("obligations") or {}).get("affordable_max_price")
    oblig = item("affordable_max_price", o["affordable_units"],
                 None if cap_price is None else max(unit_price - cap_price, 0))
    site_work = sum(v for v in site.values() if v is not None)
    rlv = revenue - build - soft - profit + (adu or 0) - site_work - (oblig or 0)
    return row | site | {
        "unit_price": unit_price, "revenue": revenue, "build_cost": build, "soft_cost": soft, "profit": profit,
        "adu_premium": adu, "sewer_ft": sewer_ft, "road_ft": road_ft, "slope_acres": slope_acres,
        "floodplain_units": fp_units, "site_work": site_work, "obligation_cost": oblig, "rlv": rlv,
        "blank_inputs": ", ".join(blanks) or None, "notes": "; ".join(dict.fromkeys(notes)) or None}


# ---------------------------------------------------------------- 5. per parcel

def best(rows: list[dict], kind: str) -> dict | None:
    """Highest RLV of one kind (by_right, rezoning, if_crossed); ties keep the first option."""
    cands = [r for r in rows if r["kind"] == kind and r.get("rlv") is not None]
    return max(cands, key=lambda r: r["rlv"]) if cands else None


def excluded_reason(p: dict, score_cfg: dict) -> str | None:
    if p.get("owner_type") in score_cfg["exclude_owner_types"]:
        return f"owner type {p['owner_type']}"
    lu = (p.get("land_use") or "").upper()
    if any(w in lu for w in score_cfg["exclude_land_use_words"]):
        return f"land use {lu}"
    if score_cfg.get("exclude_access_via_common_area") and p.get("access_via_common_area"):
        return "access via common area"
    return None


def score_parcel(p: dict, comps: dict, rules: dict, econ: dict, scoring_cfg: dict,
                 ratios: pd.DataFrame) -> tuple[dict, list[dict]]:
    """(parcel_rlv row, parcel_rlv_options rows) for one parcel."""
    basis = sewer_basis(p)
    public = [value_option(o, p, comps, econ, basis, "public") for o in yield_options(p, rules, scoring_cfg)]
    septic = [value_option(o, p, comps, econ, basis, "septic")
              for o in septic_options(p, rules, scoring_cfg, econ["sewer"]["septic_min_lot_sf"])] \
        if basis == "none_outside" else []
    scored = septic if basis == "none_outside" else public
    b = best(scored, "by_right")
    if_sewer = best(public, "by_right") if basis == "none_outside" else None
    rez, crossed = best(public, "rezoning"), best(public, "if_crossed")

    land_class = "vacant" if p.get("vacant") else "improved"
    ratio, group, n = pick_ratio(ratios, p.get("municipality"), land_class)
    assessed = p.get("total_value")
    price = assessed * ratio if assessed is not None and ratio is not None else None
    reason = excluded_reason(p, econ["score"])
    score = b["rlv"] - price if b and price is not None and not reason else None
    low_conf = basis in ("proxy_served", "assumed_in_town")
    no_value = None
    if not b:
        no_value = ("no single-family option on septic-size lots" if basis == "none_outside" and not septic
                    else "no option with comps and a buildable size" if scored else "no by-right option")
    row = {
        "pin": p["pin"], "site_address": p.get("site_address"), "jurisdiction": p["jurisdiction"],
        "zone_code": p.get("zone_code"), "municipality": p.get("municipality"), "area_sf": p.get("area_sf"),
        "sewer_basis": basis, "sewer_status": p.get("sewer_status"), "sewer_confidence": p.get("sewer_confidence"),
        "sewer_source": p.get("sewer_source"), "scored_on": "septic" if basis == "none_outside" else "public",
        "best_option": b and b["option"], "best_product": b and b["product"], "best_units": b and b["units"],
        "best_rlv": b and b["rlv"], "if_sewer_option": if_sewer and if_sewer["option"],
        "if_sewer_rlv": if_sewer and if_sewer["rlv"], "rezoning_option": rez and rez["option"],
        "rezoning_rlv": rez and rez["rlv"], "if_crossed_option": crossed and crossed["option"],
        "if_crossed_rlv": crossed and crossed["rlv"],
        "asking_price": None, "assessed_value": assessed, "land_class": land_class, "ratio": ratio,
        "ratio_group": group, "ratio_sales": n, "price": price, "price_basis": "assessed_x_ratio",
        "score": score, "excluded_reason": reason,
        "verify_sewer": basis == "assumed_in_town", "sewer_low_confidence": low_conf,
        "sewer_unverified_high_score": bool(low_conf and score is not None
                                            and score > econ["sewer"]["low_confidence_score_cap"]),
        "thin_comps": bool(b and b["thin_comps"]), "plex_pooled": bool(b and b["product"] == "plex"),
        "rlv_incomplete": bool(b and b["blank_inputs"]), "blank_inputs": b and b["blank_inputs"],
        "verify_acreage": bool(p.get("verify_acreage")),
        "septic_soils_very_limited": basis == "none_outside" and p.get("septic_dominant_rating") == "Very limited",
        "no_value_reason": no_value,
    }
    return row, public + septic


INPUT_SQL = """
SELECT y.*, f.site_address, f.municipality, f.in_municipal_limit, f.vacant, f.sewer_status, f.sewer_confidence,
       f.sewer_source, f.sewer_distance_ft, f.slope_15_25_acres, f.floodplain_flag_acres, f.total_value,
       f.lot_depth_ft, f.owner_type, f.land_use, f.access_via_common_area, c.septic_dominant_rating
FROM {s}.parcel_yield y JOIN {s}.parcel_features f USING (pin)
LEFT JOIN {s}.parcel_constraints c USING (pin)
WHERE y.yield_basis = 'by_right' AND y.pin BETWEEN :lo AND :hi
ORDER BY y.pin
"""


def _records(df: pd.DataFrame) -> list[dict]:
    return [{k: (None if v is None or (not isinstance(v, str) and pd.isna(v)) else v) for k, v in r.items()}
            for r in df.drop(columns=["row_hash", "valid_from"], errors="ignore").to_dict("records")]


def comps_by_pin(df: pd.DataFrame) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in _records(df):
        out.setdefault(r["pin"], {})[r["product"]] = r
    return out


def run_rlv(engine: Engine, econ: dict, scoring_cfg: dict, rules: dict, ratios: pd.DataFrame, run_date: date,
            s: str = "public", chunk: int = 20_000) -> tuple[pd.DataFrame, dict]:
    """Value every option of every by-right parcel and store both tables. Returns (parcel_rlv, changes).
    Parcels are read `chunk` at a time by pin range: the whole input does not fit in this PC's memory."""
    with engine.connect() as conn:
        pins = [r[0] for r in conn.execute(text(f"SELECT pin FROM {s}.parcel_yield WHERE yield_basis = 'by_right' ORDER BY pin"))]
    ranges = [(pins[i], pins[min(i + chunk, len(pins)) - 1]) for i in range(0, len(pins), chunk)] or [("", "")]
    parcels = []
    with engine.begin() as conn:
        for i, (lo, hi) in enumerate(ranges):
            bounds = {"lo": lo, "hi": hi}
            inputs = pd.read_sql(text(INPUT_SQL.format(s=s)), conn, params=bounds)
            comps = comps_by_pin(pd.read_sql(text(f"SELECT * FROM {s}.parcel_comps WHERE pin BETWEEN :lo AND :hi"),
                                             conn, params=bounds))
            rows, options = [], []
            for p in _records(inputs):
                row, opts = score_parcel(p, comps.get(p["pin"], {}), rules, econ, scoring_cfg, ratios)
                rows.append(row)
                options += opts
            parcels.append(_frame(rows, PARCEL_COLS))
            _store(conn, _frame(options, OPTION_COLS), f"{s}.parcel_rlv_options", ["pin", "option", "sewer"],
                   run_date, append=i > 0, finish=False)
        opt_changes = apply_versioned(conn, f"{s}.parcel_rlv_options", ["pin", "option", "sewer"], run_date)
        df = pd.concat(parcels, ignore_index=True)
        changes = {"parcel_rlv_options": opt_changes,
                   "parcel_rlv": _store(conn, df, f"{s}.parcel_rlv", ["pin"], run_date)}
    return df, changes


# ---------------------------------------------------------------- printing

def _money(x) -> str:
    return "blank" if x is None or pd.isna(x) else f"${x:,.0f}"


def explain(p: pd.Series, o: pd.Series | None, econ: dict) -> list[str]:
    """Every input behind a parcel's score, as printable lines."""
    costs, sw = econ["costs"], econ["site_work"]
    flags = [c for c in ("verify_sewer", "sewer_low_confidence", "sewer_unverified_high_score", "thin_comps",
                         "plex_pooled", "rlv_incomplete", "verify_acreage", "septic_soils_very_limited") if p[c]]
    lines = [f"{p['pin']}  {p['site_address'] or '(no address)'}  {p['jurisdiction']} {p['zone_code']}  "
             f"{p['area_sf'] / SQFT_PER_ACRE:,.2f} ac  [parcel_rlv, parcel_yield, parcel_features]",
             f"  sewer: {p['sewer_basis']} (status {p['sewer_status']}, confidence {p['sewer_confidence']}, "
             f"source {p['sewer_source']}); scored on {p['scored_on']} sewer"]
    if o is None:
        lines.append(f"  no value: {p['no_value_reason']}")
        return lines
    b = o["buildings"]
    lines += [
        f"  best use: {o['option']} ({o['kind']}), {o['units']} {o['product']} units in {b:g} building(s), "
        f"{o['affordable_units']} affordable  [parcel_rlv_options {p['pin']} / {o['option']} / {o['sewer']}]",
        f"    lot {o['lot_sf'] or 0:,.0f} sf, {o['lot_width_ft'] or 0:,.1f} x {o['lot_depth_ft'] or 0:,.1f} ft; "
        f"envelope {o['envelope_sf'] or 0:,.0f} sf, coverage limit {o['coverage_sf'] or 0:,.0f} sf -> footprint "
        f"{o['footprint_sf'] or 0:,.0f} sf x {o['stories']} floors (height {o['height_ft']} ft) = zoning max "
        f"{o['zoning_max_sf'] or 0:,.0f} sf",
        f"    comps: {o['comp_count']} {o['product']} sales within {o['comp_radius_mi']:g} mi"
        f"{' (THIN)' if o['thin_comps'] else ''}: median ${o['price_per_sf']:,.0f}/sf; market cap "
        f"{o['market_cap_sf']:,.0f} sf  [comp_sales; comps_for('{p['pin']}', '{o['product']}')]",
        f"    house size {o['size_sf']:,.0f} sf per building -> unit price {_money(o['unit_price'])}",
        f"    revenue    {b:g} x {o['size_sf']:,.0f} sf x ${o['price_per_sf']:,.0f} = {_money(o['revenue'])}",
        f"    build cost {b:g} x {o['size_sf']:,.0f} sf x ${costs['build_cost_per_sf']:,.0f} = -{_money(o['build_cost'])}",
        f"    soft cost  {costs['soft_cost_pct']}% of build = -{_money(o['soft_cost'])}",
        f"    profit     {costs['target_profit_pct']}% of revenue = -{_money(o['profit'])}",
        f"    ADU premium +{_money(o['adu_premium'])}",
        f"    site work  -{_money(o['site_work'])}: sewer {o['sewer_ft'] or 0:,.0f} ft = {_money(o['sewer_cost'])}; "
        f"road {o['road_ft'] or 0:,.0f} ft x {_money(sw.get('road_per_ft'))} = {_money(o['road_cost'])}; "
        f"stormwater {o['units']} x {_money(sw.get('stormwater_per_unit'))} = {_money(o['stormwater_cost'])}; "
        f"slope {o['slope_acres'] or 0:,.2f} ac x {_money(sw.get('slope_per_acre'))} = {_money(o['slope_cost'])}; "
        f"floodplain {o['floodplain_units']} units = {_money(o['floodplain_cost'])}; "
        f"creek crossing {_money(o['crossing_cost'])}",
        f"    obligations {o['affordable_units']} affordable units = -{_money(o['obligation_cost'])}",
        f"    RLV = {_money(o['rlv'])}" + (f"  (upper bound: blank {o['blank_inputs']})" if o["blank_inputs"] else ""),
        f"  price: assessed {_money(p['assessed_value'])} x ratio {p['ratio']:.3f} ({p['ratio_group']}, "
        f"{p['ratio_sales']:,} sales) = {_money(p['price'])}  (no listing)  [parcel_features.total_value, sales_ratios]",
        f"  score = RLV - price = {_money(p['score'])}",
    ]
    also = [f"{k} {p[k + '_option']} {_money(p[k + '_rlv'])}" for k in ("rezoning", "if_crossed", "if_sewer")
            if pd.notna(p[k + "_option"])]
    if also:
        lines.append("  also (not the best use): " + "; ".join(also))
    if pd.notna(o["notes"]):
        lines.append(f"  notes: {o['notes']}")
    if flags:
        lines.append("  flags: " + ", ".join(flags))
    return lines


def top(engine: Engine, econ: dict, n: int, s: str = "public") -> list[str]:
    ps = pd.read_sql(text(f"SELECT * FROM {s}.parcel_rlv WHERE score IS NOT NULL ORDER BY score DESC, pin LIMIT :n"),
                     engine, params={"n": n})
    return _explain_rows(engine, ps, econ, s, numbered=True)


def _explain_rows(engine: Engine, ps: pd.DataFrame, econ: dict, s: str, numbered: bool = False) -> list[str]:
    opts = pd.read_sql(text(f"SELECT o.* FROM {s}.parcel_rlv_options o JOIN {s}.parcel_rlv p ON p.pin = o.pin "
                            f"AND o.option = p.best_option AND o.sewer = p.scored_on WHERE o.pin = ANY(:pins)"),
                       engine, params={"pins": ps["pin"].tolist()}).set_index("pin")
    lines = []
    for i, p in enumerate(ps.itertuples(index=False), 1):
        p = pd.Series(p._asdict())
        body = explain(p, opts.loc[p["pin"]] if p["pin"] in opts.index else None, econ)
        lines += ([f"#{i} " + body[0]] if numbered else body[:1]) + body[1:] + [""]
    return lines


def summary(df: pd.DataFrame, changes: dict) -> str:
    valued = df[df["best_rlv"].notna()]
    lines = [f"Residual land value: {len(df):,} parcels with by-right yield, {len(valued):,} valued",
             "  sewer basis: " + ", ".join(f"{k} {v:,}" for k, v in df["sewer_basis"].value_counts().items()),
             "  best use: " + ", ".join(f"{k} {v:,}" for k, v in valued["best_option"].value_counts().head(8).items()),
             f"  scored {int(df['score'].notna().sum()):,}; excluded {int(df['excluded_reason'].notna().sum()):,}; "
             f"RLV > 0 {int((valued['best_rlv'] > 0).sum()):,}; score > 0 {int((df['score'] > 0).sum()):,}",
             "  flags: " + ", ".join(f"{c} {int(df[c].sum()):,}" for c in
                                     ("verify_sewer", "sewer_unverified_high_score", "thin_comps", "plex_pooled",
                                      "rlv_incomplete", "septic_soils_very_limited")),
             f"  changes: {changes}"]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--explain", metavar="PIN")
    ap.add_argument("--ratios", action="store_true", help="print the sales ratio table only")
    args = ap.parse_args()
    econ, scoring_cfg = load_config()
    engine = get_engine()
    if args.explain:
        ps = pd.read_sql(text("SELECT * FROM parcel_rlv WHERE pin = :pin"), engine, params={"pin": args.explain})
        print("\n".join(_explain_rows(engine, ps, econ, "public")) if len(ps) else f"{args.explain}: not in parcel_rlv")
        return

    run_date = date.today()
    with engine.connect() as conn:
        sales_as_of = conn.execute(text("SELECT max(snapshot_date) FROM ingest_runs WHERE source = 'county_parcels'")).scalar_one()
    pc = econ["price"]
    table, ratio_changes = build_ratios(engine, econ, sales_as_of, run_date)
    print(format_ratios(table, pc["min_sales"], window_start(sales_as_of, pc["window_months"]), sales_as_of))
    if args.ratios:
        return
    comp_changes = build_comps(engine, econ, sales_as_of, run_date)
    with engine.connect() as conn:
        print("\nComps: " + ", ".join(f"{r[0]} {r[1]:,} sales" for r in conn.execute(text(
            "SELECT product, count(*) FROM comp_sales GROUP BY 1 ORDER BY 1"))))
        for r in conn.execute(text("SELECT product, radius_mi, count(*), count(*) FILTER (WHERE thin_comps), "
                                   "percentile_cont(0.5) WITHIN GROUP (ORDER BY median_price_per_sf) "
                                   "FROM parcel_comps GROUP BY 1, 2 ORDER BY 1, 2")):
            print(f"  {r[0]:<9} {r[1]:g} mi: {r[2]:>7,} parcels, {r[3]:>7,} thin, median $/sf "
                  f"{'-' if r[4] is None else f'{r[4]:,.0f}'}")
    print(f"  changes: {comp_changes}")

    log = {"ratios": ratio_changes, **comp_changes, "economics": econ}
    missing = blank_core(econ)
    if missing:
        print(f"\nRLV not computed: fill in {', '.join(missing)} under `costs` in config/economics.yaml.")
    else:
        df, rlv_changes = run_rlv(engine, econ, scoring_cfg, load_rules(), table, run_date)
        log |= rlv_changes
        print("\n" + summary(df, rlv_changes))
        print(f"\nTop {econ['score']['top_n']} by score (best RLV - price)\n")
        print("\n".join(top(engine, econ, econ["score"]["top_n"])))
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO ingest_runs (source, snapshot_date, source_file, snapshot_written, report) "
                 "VALUES ('parcel_rlv', :d, :f, true, CAST(:r AS jsonb))"),
            {"d": run_date, "f": "parcel_sales, parcel_cama, parcel_yield, parcel_features; config/economics.yaml",
             "r": json.dumps(log, default=str)})


if __name__ == "__main__":
    main()
