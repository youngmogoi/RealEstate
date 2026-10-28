"""Phase 2.3: comps, sales ratios and residual land value (RLV) for every yield option.

Run with `make rlv` (after `make cama` and `make yield`). Settings: config/economics.yaml.
`python -m scoring.rlv --explain <pin>` prints one parcel with every input; `--ratios` prints
the sales ratio table.

Tables (each versioned, history in <table>_history; see ingest/versioning.py):
1. comp_sales: new-construction sales (rules in economics.yaml `comps`), one row per sale, with
   the sold account's CAMA heated sf and building type, $/heated sf, and geom = a point on the
   sold parcel. Product: sfh, townhome, or plex (duplex/triplex/quadraplex pooled).
2. parcel_comps: per parcel with by-right yield and per product: median $/heated sf, median sale
   price and median heated sf of the comps within 1 mile, widened to 2 then 3 miles while there
   are fewer than min_comps; comp_count, radius_mi, thin_comps. The comps behind a row are that
   product's comp_sales within radius_mi of the parcel's point on surface (comps_for() lists them).
3. sales_ratios: median sale price / current assessed value by municipality and land class.
4. parcel_rlv_options: one row per parcel, option and sewer case ('public', or 'septic' where
   sewer is unknown outside town limits), holding every input and cost line (see the formulas in
   config/economics.yaml). A cost whose config value is blank is NULL here and named in
   blank_inputs; the RLV leaves it out (an upper bound). Options: cur_<type> (rebuild on the lot
   as it is), split_<type> (2+ lots), split_<type>_keep_house (the existing house stays on one of
   the lots), sub_standard / sub_conservation / sub_compact / sub_mixed_income (2+ lots, lot
   model), townhome (by right, or kind 'rezoning'), and if_crossed_* (kind 'if_crossed', plus the
   creek crossing cost).
5. parcel_rlv: per parcel, the best use (highest by-right RLV), the best rezoning and if-crossed
   options (reported, never the best use), the "if sewer available" RLV, price, score and flags.
   Price = asking price if listed (no listings source yet), else assessed value x sales ratio.
   Score = best RLV - price; blank for parcels never scored (economics `score`), which keep an
   exclusion_category; golf courses and clubs are also on redevelopment_watch.

House size (house model): the median heated sf of the nearby comps for the product, capped by
zoning: footprint = the smaller of the setback envelope and max building coverage on the option's
lot (townhome = the unit footprint in scoring.yaml), times floors (max height / story height,
capped). Plex units are priced from townhome comps x duplex_discount (comp_proxy) until the plex
pool has min_real_sales nearby; a plex building then holds its units at the townhome median size.
"""

import argparse
import fnmatch
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
SEPTIC_OPTIONS = ["cur_sfh", "split_sfh", "split_sfh_keep_house", "sub_standard"]

OPTION_COLS = {
    "pin": "str", "option": "str", "sewer": "str", "kind": "str", "model": "str", "product": "str",
    "yield_units": "int", "units": "int", "over_project_cap": "bool", "units_per_building": "int",
    "buildings": "float", "affordable_units": "int",
    "lot_sf": "float", "lot_width_ft": "float", "lot_depth_ft": "float",
    "envelope_sf": "float", "coverage_sf": "float", "footprint_sf": "float", "height_ft": "float", "stories": "int",
    "zoning_max_sf": "float", "comp_median_sf": "float", "size_sf": "float",
    "comp_product": "str", "comp_radius_mi": "float", "comp_count": "int", "price_per_sf": "float",
    "comp_median_price": "float", "thin_comps": "bool", "comp_proxy": "bool",
    "unit_price": "float", "lot_price": "float", "revenue": "float", "build_cost": "float", "soft_cost": "float",
    "profit": "float", "development_cost": "float", "absorption_months": "int", "discount_factor": "float",
    "discount_cost": "float", "adu_premium": "float", "retained_value": "float",
    "existing_heated_sf": "float", "demolition_cost": "float",
    "sewer_ft": "float", "sewer_cost": "float", "road_ft": "float", "road_cost": "float",
    "stormwater_cost": "float", "slope_acres": "float", "slope_cost": "float", "floodplain_units": "int",
    "floodplain_cost": "float", "crossing_cost": "float", "site_work": "float", "obligation_cost": "float",
    "rlv": "float", "blank_inputs": "str", "notes": "str",
}
PARCEL_COLS = {
    "pin": "str", "site_address": "str", "jurisdiction": "str", "zone_code": "str", "municipality": "str",
    "land_use": "str", "owner_type": "str", "area_sf": "float", "building_value": "float",
    "existing_heated_sf": "float",
    "sewer_basis": "str", "sewer_status": "str", "sewer_confidence": "str", "sewer_source": "str",
    "scored_on": "str", "best_option": "str", "best_product": "str", "best_units": "int", "best_rlv": "float",
    "if_sewer_option": "str", "if_sewer_rlv": "float", "rezoning_option": "str", "rezoning_rlv": "float",
    "if_crossed_option": "str", "if_crossed_rlv": "float",
    "asking_price": "float", "assessed_value": "float", "land_class": "str", "ratio": "float", "ratio_group": "str",
    "ratio_sales": "int", "price": "float", "price_basis": "str", "score": "float",
    "exclusion_category": "str", "excluded_reason": "str", "redevelopment_watch": "bool",
    "verify_sewer": "bool", "sewer_low_confidence": "bool", "sewer_assumed_new_units": "bool",
    "thin_comps": "bool", "comp_proxy": "bool", "over_project_cap": "bool", "rlv_incomplete": "bool",
    "blank_inputs": "str", "verify_acreage": "bool", "septic_soils_very_limited": "bool", "no_value_reason": "str",
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
              "median_price_per_sf": "float", "median_sale_price": "float", "median_heated_sf": "float",
              "thin_comps": "bool"}


def comp_stats(targets: pd.DataFrame, comps: pd.DataFrame, cfg: dict, batch: int = 10_000) -> pd.DataFrame:
    """parcel_comps rows. targets: pin, x, y; comps: product, x, y, price_per_sf, sale_price, heated_sf
    (EPSG:2264 feet).

    Per product, comps within the first radius; parcels with fewer than min_comps are searched again
    at the next radius, so each parcel keeps the smallest radius with enough comps, else the widest
    (thin_comps). Medians are interpolated (as percentile_cont). Done here with a spatial index in
    batches, not in SQL: 206k parcels x 3 miles is ~80M pairs, which Postgres sorted on disk (it
    filled C: on 2026-10-28)."""
    import numpy as np
    import shapely

    pts = shapely.points(targets["x"].to_numpy(), targets["y"].to_numpy())
    radii, min_n = cfg["radii_miles"], cfg["min_comps"]
    rows = []
    for product in cfg["products"]:
        cg = comps[comps["product"] == product]
        tree = shapely.STRtree(shapely.points(cg["x"].to_numpy(), cg["y"].to_numpy()))
        ppsf, price, sf = cg["price_per_sf"].to_numpy(), cg["sale_price"].to_numpy(), cg["heated_sf"].to_numpy()
        found: dict[int, tuple] = {}
        todo = np.arange(len(targets))
        for r in radii:
            for i in range(0, len(todo), batch):
                b = todo[i:i + batch]
                ti, ci = tree.query(pts[b], predicate="dwithin", distance=r * FT_PER_MILE)
                g = pd.DataFrame({"t": b[ti], "ppsf": ppsf[ci], "price": price[ci], "sf": sf[ci]}).groupby("t")
                med = g.median()
                for t, n, m_ppsf, m_price, m_sf in zip(med.index, g.size(), med["ppsf"], med["price"], med["sf"]):
                    found[t] = (r, int(n), float(m_ppsf), float(m_price), float(m_sf))
            todo = np.array([t for t in todo if t not in found or found[t][1] < min_n], dtype=int)
        for t, pin in enumerate(targets["pin"]):
            r, n, m_ppsf, m_price, m_sf = found.get(t, (max(radii), 0, None, None, None))
            rows.append({"pin": pin, "product": product, "radius_mi": r, "comp_count": n,
                         "median_price_per_sf": m_ppsf, "median_sale_price": m_price, "median_heated_sf": m_sf,
                         "thin_comps": n < min_n})
    return _frame(rows, COMPS_COLS).sort_values(["pin", "product"]).reset_index(drop=True)


def build_comps(engine: Engine, econ: dict, sales_as_of: date, run_date: date, s: str = "public") -> dict:
    """Rebuild comp_sales and parcel_comps. Returns change counts."""
    c = econ["comps"]
    pairs = [(t, prod) for prod, types in c["products"].items() for t in types]
    params = {"as_of": sales_as_of, "start": window_start(sales_as_of, c["window_months"]), "max_age": c["max_age_years"],
              "min_ppsf": c["min_price_per_sf"], "max_ppsf": c["max_price_per_sf"],
              "types": [t for t, _ in pairs], "products": [p for _, p in pairs]}
    with engine.begin() as conn:
        create_staging_from_query(conn, f"{s}.comp_sales_staging", COMP_SALES_SQL.format(s=s), params)
        sales = apply_versioned(conn, f"{s}.comp_sales", ["account_pin", "transfer_id", "property_id"], run_date)
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS comp_sales_geom_idx ON {s}.comp_sales USING gist (geom)"))
        comps = pd.read_sql(text(f"SELECT product, ST_X(geom) AS x, ST_Y(geom) AS y, price_per_sf, sale_price, heated_sf "
                                 f"FROM {s}.comp_sales ORDER BY account_pin, transfer_id, property_id"), conn)
        targets = pd.read_sql(text(
            f"SELECT y.pin, ST_X(g) AS x, ST_Y(g) AS y FROM {s}.parcel_yield y "
            f"JOIN {s}.parcels p USING (pin), ST_PointOnSurface(p.geom) g WHERE y.yield_basis = 'by_right' ORDER BY y.pin"), conn)
    df = comp_stats(targets, comps, c)
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
    """model: house | lot; project: absorption and the project cap apply; removes_building: the
    existing building (if any) is demolished; keep_house: it stays and its value is retained."""
    return {"option": name, "kind": kind, "model": "house", "product": product, "units": int(units),
            "units_per_building": upb, "std": std, "height_ft": _height(height_rule), "lot_sf": lot_sf,
            "lot_width_ft": width, "lot_depth_ft": depth, "creates_lots": creates_lots, "project": False,
            "removes_building": True, "keep_house": False, "footprint_sf": None, "affordable_units": 0,
            "adu": False, "notes": []} | extra


def _sub_specs(rules: dict, j: str, rule: dict) -> dict:
    """(lot sf, lot width, standards rule, notes) of a new lot in each subdivision option."""
    types, alts = rule["types"], rule.get("alternatives") or {}
    lot, width = Y.type_standards(rules, j, rule, "sfh") if Y._allowed(types.get("sfh")) else (
        rule["min_lot_sf"]["value"], rule["min_lot_width_ft"]["value"] or 0)
    std = Y.standards_rule(rules, j, rule, "sfh") if Y._allowed(types.get("sfh")) else rule
    specs = {"sub_standard": (lot, width, std, [])}
    if c := alts.get("conservation"):
        cut = 1 - c["lot_area_and_width_reduction_pct"] / 100
        specs["sub_conservation"] = (lot * cut, width * cut, std, [])
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
    built = not p.get("vacant")
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
            lot = (area / n_lots, split_w / n_lots if split_w else None, depth)
            out.append(_opt(f"split_{t}", "by_right", product, p[f"split_{t}"], upb, std, rule, *lot, True,
                            affordable_units=aff * n_lots, n_lots=n_lots))
            if built:      # the existing house stays on one lot; new buildings on the others
                out.append(_opt(f"split_{t}_keep_house", "by_right", product, (n_lots - 1) * upb, upb, std, rule, *lot,
                                True, affordable_units=aff * (n_lots - 1), n_lots=n_lots, removes_building=False,
                                keep_house=True))

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
                            affordable_units=aff, notes=list(notes), model="lot", project=True))
        if units := p.get(prefix + "townhome_units"):
            rezoning = bool(p.get("townhome_needs_rezoning"))
            target = Y.district_rule(rules, j, p["townhome_target_district"]) if rezoning else rule
            k = kind if prefix else ("rezoning" if rezoning else "by_right")
            out.append(_opt(prefix + "townhome", k, "townhome", units, 1, target, target, None, None, None, True,
                            footprint_sf=th["unit_width_ft"] * th["unit_depth_ft"], project=True,
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
        elif o["option"].startswith("split_sfh"):
            n_lots = min(o["n_lots"], Y._floor(p["area_sf"] / septic_min_sf))
            units = n_lots - 1 if o["keep_house"] else n_lots
            if n_lots >= 2 and units >= 1:
                w = o["lot_width_ft"] * o["n_lots"] / n_lots if o["lot_width_ft"] else None
                out.append(o | {"units": units, "n_lots": n_lots, "lot_sf": p["area_sf"] / n_lots, "lot_width_ft": w})
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


def discount_factor(units: int, per_month: float, annual_pct: float) -> tuple[int, float]:
    """(sell-out months, present-value factor): units sold evenly over ceil(units / per_month)
    months, month m's sales discounted by (1 + rate) ^ -(m / 12)."""
    months = max(math.ceil(units / per_month), 1)
    r = annual_pct / 100
    return months, sum((1 + r) ** (-m / 12) for m in range(1, months + 1)) / months


def comp_basis(o: dict, comps: dict, econ: dict) -> dict:
    """Which comps price an option: its product's, or for plex the townhome comps x duplex_discount
    (comp_proxy) until the plex pool has min_real_sales nearby. Returns comp fields and the size of
    one building before the zoning cap (comp_median_sf)."""
    pc = econ["plex"]
    if o["product"] == "plex" and (comps.get("plex") or {}).get("comp_count", 0) < pc["min_real_sales"]:
        c, d = comps.get("townhome") or {}, pc.get("duplex_discount")
        ppsf = c.get("median_price_per_sf")
        unit_sf = c.get("median_heated_sf")
        return {"comp_product": "townhome", "comp_radius_mi": c.get("radius_mi"), "comp_count": c.get("comp_count", 0),
                "price_per_sf": None if ppsf is None or d is None else ppsf * d, "comp_median_price": None,
                "comp_median_sf": None if unit_sf is None else unit_sf * o["units_per_building"],
                "thin_comps": bool(c.get("thin_comps", True)), "comp_proxy": True, "_blank": d is None and ppsf is not None}
    c = comps.get(o["product"]) or {}
    return {"comp_product": o["product"], "comp_radius_mi": c.get("radius_mi"), "comp_count": c.get("comp_count", 0),
            "price_per_sf": c.get("median_price_per_sf"), "comp_median_price": c.get("median_sale_price"),
            "comp_median_sf": c.get("median_heated_sf"), "thin_comps": bool(c.get("thin_comps", True)),
            "comp_proxy": False, "_blank": False}


def value_option(o: dict, p: dict, comps: dict, econ: dict, basis: str, sewer: str) -> dict:
    """One option's RLV with every input (a parcel_rlv_options row)."""
    costs, sw, hs, proj = econ["costs"], econ["site_work"], econ["house_size"], econ["project"]
    blanks, notes = [], list(o["notes"])

    def item(name: str, qty: float, rate: float | None) -> float | None:
        if not qty:
            return 0.0
        if rate is None:
            blanks.append(name)
            return None
        return qty * rate

    units, over_cap = o["units"], False
    if o["project"] and proj.get("max_lots_per_project") and units > proj["max_lots_per_project"]:
        units, over_cap = proj["max_lots_per_project"], True
        notes.append(f"valued at the project cap of {units} of {o['units']} {'lots' if o['model'] == 'lot' else 'units'}")
    upb = o["units_per_building"]
    buildings = units / upb
    cb = comp_basis(o, comps, econ)
    if cb.pop("_blank"):
        blanks.append("duplex_discount")
    if cb["comp_proxy"]:
        notes.append("priced from townhome comps x duplex_discount (comp_proxy)")
    envelope, coverage, fp = footprint(o)
    stories = min(int(o["height_ft"] // hs["story_height_ft"]), hs["max_stories"]) if o["height_ft"] else hs["max_stories"]
    zoning_max = fp * stories if fp is not None else None
    affordable = min(o["affordable_units"], units)
    row = dict.fromkeys(OPTION_COLS) | {
           "pin": p["pin"], "option": o["option"], "sewer": sewer, "kind": o["kind"], "model": o["model"],
           "product": o["product"], "yield_units": o["units"], "units": units, "over_project_cap": over_cap,
           "units_per_building": upb, "buildings": buildings, "affordable_units": affordable,
           "lot_sf": o["lot_sf"], "lot_width_ft": o["lot_width_ft"], "lot_depth_ft": o["lot_depth_ft"],
           "existing_heated_sf": p.get("existing_heated_sf")} | cb

    if o["model"] == "lot":
        ratio = econ["subdivision"].get("lot_to_home_ratio")
        if ratio is None:
            blanks.append("lot_to_home_ratio")
        home = cb["comp_median_price"]
        if home is None or ratio is None:
            notes.append("no comps within the widest radius" if home is None else "no finished lot price")
            return row | {"notes": "; ".join(dict.fromkeys(notes)) or None, "blank_inputs": ", ".join(blanks) or None}
        lot_price = ratio * home
        revenue = units * lot_price
        unit_price, house_fp = home, None
        dev = item("development_cost_per_lot", units, econ["subdivision"].get("development_cost_per_lot"))
        build = soft = profit = None
        margin = revenue
        row |= {"lot_price": lot_price, "development_cost": dev}
    else:
        if not o["height_ft"]:
            notes.append("no height limit in the rule: max_stories used")
        sizes = [x for x in (zoning_max, cb["comp_median_sf"]) if x is not None]
        size = min(sizes) if sizes else None
        if zoning_max is None:
            notes.append("zoning size unknown (no lot width/depth or coverage)")
        row |= {"envelope_sf": envelope, "coverage_sf": coverage, "footprint_sf": fp, "height_ft": o["height_ft"],
                "stories": stories, "zoning_max_sf": zoning_max, "size_sf": size}
        ppsf = cb["price_per_sf"]
        if size is None or ppsf is None or size <= 0:
            notes.append("no buildable house size" if ppsf is not None else
                         "no comps within the widest radius" if "duplex_discount" not in blanks else "no plex price")
            return row | {"notes": "; ".join(dict.fromkeys(notes)) or None, "blank_inputs": ", ".join(blanks) or None}
        revenue = buildings * ppsf * size
        build = buildings * costs["build_cost_per_sf"] * size
        soft = build * costs["soft_cost_pct"] / 100
        profit = revenue * costs["target_profit_pct"] / 100
        unit_price, house_fp, dev = ppsf * size / upb, size / stories, 0.0
        margin = revenue - build - soft - profit
        row |= {"build_cost": build, "soft_cost": soft, "profit": profit}

    months, factor = None, 1.0
    if o["project"]:
        per_month, rate = proj.get("absorption_homes_per_month"), proj.get("annual_discount_rate_pct")
        if per_month is None:
            blanks.append("absorption_homes_per_month")
        if rate is None:
            blanks.append("annual_discount_rate_pct")
        if per_month is not None and rate is not None:
            months, factor = discount_factor(units, per_month, rate)
    discount = margin * (1 - factor)

    adu = item("adu_premium", 1 if o["adu"] else 0, econ.get("adu_premium"))
    retained = 0.0
    if o["keep_house"]:
        retained = (p.get("building_value") or 0) * (p.get("_ratio") or 1)
        notes.append("kept house counted at assessed building value x sales ratio")
    heated = p.get("existing_heated_sf") or 0
    removes = o["removes_building"] and not p.get("vacant")
    if removes and not heated:
        notes.append("existing building with no CAMA heated sf: demolition not costed")
    demo = item("demo_cost_per_sf", heated if removes else 0, costs.get("demo_cost_per_sf"))
    lots = o["creates_lots"] or sw.get("road_and_stormwater_on_lot_as_is")
    sewer_ft = p.get("sewer_distance_ft") if basis == "known_distance" and sewer == "public" else 0
    road_ft = (p.get("frontage_ft") or 0) if lots else 0
    slope_acres = p.get("slope_15_25_acres") or 0
    fp_units = floodplain_units(o | {"units": units}, p, house_fp)
    site = {
        "sewer_cost": item("sewer_per_ft", sewer_ft, sw.get("sewer_per_ft")),
        "road_cost": item("road_per_ft", road_ft, sw.get("road_per_ft")),
        "stormwater_cost": item("stormwater_per_unit", units if lots else 0, sw.get("stormwater_per_unit")),
        "slope_cost": item("slope_per_acre", slope_acres, sw.get("slope_per_acre")),
        "floodplain_cost": item("floodplain_per_unit", fp_units, sw.get("floodplain_per_unit")),
        "crossing_cost": item("creek_crossing", 1 if o["kind"] == "if_crossed" else 0, sw.get("creek_crossing")),
    }
    cap_price = (econ.get("obligations") or {}).get("affordable_max_price")
    oblig = item("affordable_max_price", affordable, None if cap_price is None else max(unit_price - cap_price, 0))
    site_work = sum(v for v in site.values() if v is not None)
    rlv = margin - discount - (dev or 0) + (adu or 0) + retained - (demo or 0) - site_work - (oblig or 0)
    return row | site | {
        "unit_price": unit_price, "revenue": revenue, "absorption_months": months, "discount_factor": factor,
        "discount_cost": discount, "adu_premium": adu, "retained_value": retained, "demolition_cost": demo,
        "sewer_ft": sewer_ft, "road_ft": road_ft, "slope_acres": slope_acres, "floodplain_units": fp_units,
        "site_work": site_work, "obligation_cost": oblig, "rlv": rlv,
        "blank_inputs": ", ".join(dict.fromkeys(blanks)) or None, "notes": "; ".join(dict.fromkeys(notes)) or None}


# ---------------------------------------------------------------- 5. per parcel

def best(rows: list[dict], kind: str) -> dict | None:
    """Highest RLV of one kind (by_right, rezoning, if_crossed); ties keep the first option."""
    cands = [r for r in rows if r["kind"] == kind and r.get("rlv") is not None]
    return max(cands, key=lambda r: r["rlv"]) if cands else None


def exclusion(p: dict, score_cfg: dict) -> tuple[str | None, str | None]:
    """(category, reason) for parcels never scored, else (None, None)."""
    for cat, owner in score_cfg["exclude_owner_types"].items():
        if p.get("owner_type") == owner:
            return cat, f"owner type {owner}"
    lu = (p.get("land_use") or "").upper()
    for cat, patterns in score_cfg["exclude_land_uses"].items():
        if any(fnmatch.fnmatchcase(lu, pat) for pat in patterns):
            return cat, f"land use {lu}"
    if score_cfg.get("exclude_access_via_common_area") and p.get("access_via_common_area"):
        return "hoa_common_area", "access via common area"
    return None, None


def adds_units(o: dict | None, p: dict) -> bool:
    """Does the option add dwellings or lots? (an existing building counts as one unit)"""
    return bool(o) and (o["option"].startswith(("split_", "sub_", "townhome", "if_crossed_"))
                        or o["units"] > (0 if p.get("vacant") else 1))


def score_parcel(p: dict, comps: dict, rules: dict, econ: dict, scoring_cfg: dict,
                 ratios: pd.DataFrame) -> tuple[dict, list[dict]]:
    """(parcel_rlv row, parcel_rlv_options rows) for one parcel."""
    basis = sewer_basis(p)
    land_class = "vacant" if p.get("vacant") else "improved"
    ratio, group, n = pick_ratio(ratios, p.get("municipality"), land_class)
    p = p | {"_ratio": ratio}
    public = [value_option(o, p, comps, econ, basis, "public") for o in yield_options(p, rules, scoring_cfg)]
    septic = [value_option(o, p, comps, econ, basis, "septic")
              for o in septic_options(p, rules, scoring_cfg, econ["sewer"]["septic_min_lot_sf"])] \
        if basis == "none_outside" else []
    scored = septic if basis == "none_outside" else public
    b = best(scored, "by_right")
    if_sewer = best(public, "by_right") if basis == "none_outside" else None
    rez, crossed = best(public, "rezoning"), best(public, "if_crossed")

    assessed = p.get("total_value")
    price = assessed * ratio if assessed is not None and ratio is not None else None
    category, reason = exclusion(p, econ["score"])
    score = b["rlv"] - price if b and price is not None and not reason else None
    low_conf = basis in ("proxy_served", "assumed_in_town")
    no_value = None
    if not b:
        no_value = ("no single-family option on septic-size lots" if basis == "none_outside" and not septic
                    else "no option with comps and a buildable size" if scored else "no by-right option")
    row = {
        "pin": p["pin"], "site_address": p.get("site_address"), "jurisdiction": p["jurisdiction"],
        "zone_code": p.get("zone_code"), "municipality": p.get("municipality"), "land_use": p.get("land_use"),
        "owner_type": p.get("owner_type"), "area_sf": p.get("area_sf"), "building_value": p.get("building_value"),
        "existing_heated_sf": p.get("existing_heated_sf"),
        "sewer_basis": basis, "sewer_status": p.get("sewer_status"), "sewer_confidence": p.get("sewer_confidence"),
        "sewer_source": p.get("sewer_source"), "scored_on": "septic" if basis == "none_outside" else "public",
        "best_option": b and b["option"], "best_product": b and b["product"], "best_units": b and b["units"],
        "best_rlv": b and b["rlv"], "if_sewer_option": if_sewer and if_sewer["option"],
        "if_sewer_rlv": if_sewer and if_sewer["rlv"], "rezoning_option": rez and rez["option"],
        "rezoning_rlv": rez and rez["rlv"], "if_crossed_option": crossed and crossed["option"],
        "if_crossed_rlv": crossed and crossed["rlv"],
        "asking_price": None, "assessed_value": assessed, "land_class": land_class, "ratio": ratio,
        "ratio_group": group, "ratio_sales": n, "price": price, "price_basis": "assessed_x_ratio",
        "score": score, "exclusion_category": category, "excluded_reason": reason,
        "redevelopment_watch": category in econ["score"]["redevelopment_watch"],
        "verify_sewer": basis == "assumed_in_town", "sewer_low_confidence": low_conf,
        "sewer_assumed_new_units": low_conf and adds_units(b, p),
        "thin_comps": bool(b and b["thin_comps"]), "comp_proxy": bool(b and b["comp_proxy"]),
        "over_project_cap": bool(b and b["over_project_cap"]),
        "rlv_incomplete": bool(b and b["blank_inputs"]), "blank_inputs": b and b["blank_inputs"],
        "verify_acreage": bool(p.get("verify_acreage")),
        "septic_soils_very_limited": basis == "none_outside" and p.get("septic_dominant_rating") == "Very limited",
        "no_value_reason": no_value,
    }
    return row, public + septic


INPUT_SQL = """
SELECT y.*, f.site_address, f.municipality, f.in_municipal_limit, f.vacant, f.sewer_status, f.sewer_confidence,
       f.sewer_source, f.sewer_distance_ft, f.slope_15_25_acres, f.floodplain_flag_acres, f.total_value,
       f.building_value, f.lot_depth_ft, f.owner_type, f.land_use, f.access_via_common_area,
       c.septic_dominant_rating, h.existing_heated_sf
FROM {s}.parcel_yield y JOIN {s}.parcel_features f USING (pin)
LEFT JOIN {s}.parcel_constraints c USING (pin)
LEFT JOIN LATERAL (SELECT sum(heated_sf) AS existing_heated_sf FROM {s}.parcel_cama a WHERE a.pin = y.pin) h ON true
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
            s: str = "public", chunk: int = 5_000) -> tuple[pd.DataFrame, dict]:
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
            del inputs, comps, rows, options
        opt_changes = apply_versioned(conn, f"{s}.parcel_rlv_options", ["pin", "option", "sewer"], run_date)
        df = pd.concat(parcels, ignore_index=True)
        changes = {"parcel_rlv_options": opt_changes,
                   "parcel_rlv": _store(conn, df, f"{s}.parcel_rlv", ["pin"], run_date)}
    return df, changes


# ---------------------------------------------------------------- printing

def _money(x) -> str:
    return "blank" if x is None or pd.isna(x) else f"-${-x:,.0f}" if x < 0 else f"${x:,.0f}"


def _n(x, fmt: str = ",.0f") -> str:
    return "-" if x is None or pd.isna(x) else format(x, fmt)


FLAG_COLS = ["verify_sewer", "sewer_low_confidence", "sewer_assumed_new_units", "thin_comps", "comp_proxy",
             "over_project_cap", "rlv_incomplete", "verify_acreage", "septic_soils_very_limited"]


def explain(p: pd.Series, o: pd.Series | None, econ: dict) -> list[str]:
    """Every input behind a parcel's score, as printable lines."""
    costs, sw, sub, proj = econ["costs"], econ["site_work"], econ["subdivision"], econ["project"]
    lines = [f"{p['pin']}  {p['site_address'] or '(no address)'}  {p['jurisdiction']} {p['zone_code']}  "
             f"{p['area_sf'] / SQFT_PER_ACRE:,.2f} ac  {p['land_use']}  [parcel_rlv, parcel_yield, parcel_features]",
             f"  sewer: {p['sewer_basis']} (status {p['sewer_status']}, confidence {p['sewer_confidence']}, "
             f"source {p['sewer_source']}); scored on {p['scored_on']} sewer; existing building "
             f"{_money(p['building_value'])}, {_n(p['existing_heated_sf'])} heated sf (CAMA)"]
    if p["excluded_reason"] is not None and pd.notna(p["excluded_reason"]):
        lines.append(f"  NOT SCORED: {p['exclusion_category']} ({p['excluded_reason']})"
                     + ("; on the redevelopment watch list" if p["redevelopment_watch"] else ""))
    if o is None:
        lines.append(f"  no value: {p['no_value_reason']}")
        return lines
    cap = f" (yield {o['yield_units']}, over the project cap)" if o["over_project_cap"] else ""
    lines.append(f"  best use: {o['option']} ({o['kind']}, {o['model']} model), {o['units']} {o['product']} units{cap}, "
                 f"{o['affordable_units']} affordable  [parcel_rlv_options {p['pin']} / {o['option']} / {o['sewer']}]")
    comp = (f"    comps: {o['comp_count']} {o['comp_product']} sales within {_n(o['comp_radius_mi'], 'g')} mi"
            f"{' (THIN)' if o['thin_comps'] else ''}"
            f"{' x duplex_discount ' + str(econ['plex']['duplex_discount']) + ' (PROXY)' if o['comp_proxy'] else ''}: "
            f"median ${_n(o['price_per_sf'])}/sf, {_n(o['comp_median_sf'])} sf, sale {_money(o['comp_median_price'])}"
            f"  [comp_sales; comps_for('{p['pin']}', '{o['comp_product']}')]")
    if o["model"] == "lot":
        lines += [comp,
                  f"    finished lot price {sub['lot_to_home_ratio']} x median new home {_money(o['comp_median_price'])} "
                  f"= {_money(o['lot_price'])}",
                  f"    lot sales  {o['units']} x {_money(o['lot_price'])} = {_money(o['revenue'])}",
                  f"    development {o['units']} x {_money(sub.get('development_cost_per_lot'))} = "
                  f"-{_money(o['development_cost'])}"]
    else:
        b = o["buildings"]
        lines += [f"    lot {_n(o['lot_sf'])} sf, {_n(o['lot_width_ft'], ',.1f')} x {_n(o['lot_depth_ft'], ',.1f')} ft; "
                  f"envelope {_n(o['envelope_sf'])} sf, coverage limit {_n(o['coverage_sf'])} sf -> footprint "
                  f"{_n(o['footprint_sf'])} sf x {o['stories']} floors (height {_n(o['height_ft'], 'g')} ft) = zoning max "
                  f"{_n(o['zoning_max_sf'])} sf",
                  comp,
                  f"    house size {_n(o['size_sf'])} sf per building (smaller of comps' median and zoning max) "
                  f"-> unit price {_money(o['unit_price'])}",
                  f"    revenue    {b:g} x {_n(o['size_sf'])} sf x ${_n(o['price_per_sf'])} = {_money(o['revenue'])}",
                  f"    build cost {b:g} x {_n(o['size_sf'])} sf x ${costs['build_cost_per_sf']:,} = -{_money(o['build_cost'])}",
                  f"    soft cost  {costs['soft_cost_pct']}% of build = -{_money(o['soft_cost'])}",
                  f"    profit     {costs['target_profit_pct']}% of revenue = -{_money(o['profit'])}"]
    if pd.notna(o["absorption_months"]):
        lines.append(f"    absorption {o['units']} / {proj['absorption_homes_per_month']} a month = "
                     f"{o['absorption_months']} months at {proj['annual_discount_rate_pct']}%/yr: factor "
                     f"{o['discount_factor']:.4f} -> discount -{_money(o['discount_cost'])}")
    lines += [
        f"    ADU premium +{_money(o['adu_premium'])}; kept house +{_money(o['retained_value'])}; demolition "
        f"{_n(o['existing_heated_sf'])} sf x {_money(costs.get('demo_cost_per_sf'))} = -{_money(o['demolition_cost'])}",
        f"    site work  -{_money(o['site_work'])}: sewer {_n(o['sewer_ft'])} ft = {_money(o['sewer_cost'])}; "
        f"road {_n(o['road_ft'])} ft x {_money(sw.get('road_per_ft'))} = {_money(o['road_cost'])}; "
        f"stormwater {o['units']} x {_money(sw.get('stormwater_per_unit'))} = {_money(o['stormwater_cost'])}; "
        f"slope {_n(o['slope_acres'], ',.2f')} ac x {_money(sw.get('slope_per_acre'))} = {_money(o['slope_cost'])}; "
        f"floodplain {o['floodplain_units']} units = {_money(o['floodplain_cost'])}; "
        f"creek crossing {_money(o['crossing_cost'])}",
        f"    obligations {o['affordable_units']} affordable units = -{_money(o['obligation_cost'])}",
        f"    RLV = {_money(o['rlv'])}" + (f"  (upper bound: blank {o['blank_inputs']})" if pd.notna(o["blank_inputs"]) else ""),
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
    flags = [c for c in FLAG_COLS if p[c]]
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
             "  best use: " + ", ".join(f"{k} {v:,}" for k, v in valued["best_option"].value_counts().head(10).items()),
             f"  scored {int(df['score'].notna().sum()):,}; RLV > 0 {int((valued['best_rlv'] > 0).sum()):,}; "
             f"score > 0 {int((df['score'] > 0).sum()):,}",
             "  not scored: " + ", ".join(f"{k} {v:,}" for k, v in df["exclusion_category"].value_counts().items())
             + f"; redevelopment watch {int(df['redevelopment_watch'].sum()):,}",
             "  flags: " + ", ".join(f"{c} {int(df[c].sum()):,}" for c in FLAG_COLS),
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
