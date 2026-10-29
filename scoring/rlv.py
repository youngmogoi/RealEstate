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
   exclusion_category (owner type, land use, common-area access, or county address uses: utility
   sites, and parcels whose every occupiable address is institutional); golf courses and clubs are
   also on redevelopment_watch. An institutional address beside other addresses is only flagged.

House size (house model): the median heated sf of the nearby comps for the product, capped by
zoning: footprint = the smaller of the setback envelope and max building coverage on the option's
lot (townhome = the unit footprint in scoring.yaml), times floors (max height / story height,
capped). Plex units are priced from townhome comps x duplex_discount (comp_proxy) until the plex
pool has min_real_sales nearby; a plex building then holds its units at the townhome median size.
"""

import argparse
import fnmatch
import gc
import importlib
import json
import math
import re
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
CORE = ["build_cost_per_sf", "soft_cost_pct", "target_profit_pct"]   # build cost: at least one product
# economics costs.build_cost_per_sf key for each comp product
BUILD_COST_KEY = {"sfh": "sfh", "townhome": "townhome", "plex": "duplex_triplex"}
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
    "comp_product": "str", "lot_band": "str", "lot_band_fallback": "bool", "comp_radius_mi": "float",
    "comp_count": "int", "price_per_sf": "float", "comp_median_price": "float", "thin_comps": "bool", "comp_proxy": "bool",
    "home_price": "float", "unit_price": "float", "lot_price": "float", "revenue": "float", "build_cost": "float", "soft_cost": "float",
    "selling_cost": "float", "land_in_loan": "float", "loan_amount": "float", "financing_interest": "float",
    "financing_closing": "float", "financing_cost": "float", "profit": "float", "development_cost": "float", "absorption_months": "int", "discount_factor": "float",
    "discount_cost": "float", "adu_premium": "float", "retained_value": "float",
    "existing_heated_sf": "float", "demolition_cost": "float",
    "sewer_ft": "float", "sewer_cost": "float", "road_ft": "float", "road_cost": "float",
    "stormwater_cost": "float", "slope_acres": "float", "slope_cost": "float", "floodplain_units": "int",
    "floodplain_cost": "float", "crossing_cost": "float", "site_work": "float", "obligation_cost": "float",
    "rollback_cost": "float", "rlv": "float", "blank_inputs": "str", "notes": "str",
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
    "ratio_sales": "int", "puv_enrolled": "bool", "deferred_value": "float", "land_price_per_acre": "float",
    "land_price_group": "str", "land_price_sales": "int", "price": "float", "price_basis": "str", "score": "float",
    "exclusion_category": "str", "excluded_reason": "str", "redevelopment_watch": "bool",
    "existing_commercial_use": "bool", "addresses": "int", "occupiable_addresses": "int", "asset_address_uses": "str",
    "owner_name": "str", "occupiable_address_uses": "str", "utility_address": "bool", "institutional_address": "bool",
    "verify_sewer": "bool", "sewer_low_confidence": "bool", "sewer_assumed_new_units": "bool",
    "thin_comps": "bool", "comp_proxy": "bool", "lot_band_fallback": "bool", "over_project_cap": "bool",
    "rlv_incomplete": "bool",
    "blank_inputs": "str", "verify_acreage": "bool", "septic_soils_very_limited": "bool", "no_value_reason": "str",
}
DTYPES = {"str": "object", "int": "Int64", "float": "float64", "bool": "boolean"}


def load_config() -> tuple[dict, dict]:
    read = lambda name: yaml.safe_load((ROOT / "config" / name).read_text(encoding="utf-8"))
    return read("economics.yaml"), read("scoring.yaml")


def blank_core(econ: dict) -> list[str]:
    """Core cost settings still blank; RLV can't be computed until they are filled in."""
    c = econ["costs"]
    filled = lambda k: (any(v is not None for v in (c.get(k) or {}).values()) if k == "build_cost_per_sf"
                        else c.get(k) is not None)
    return [k for k in CORE if not filled(k)]


def financing(build: float, soft: float, land: float | None, fin: dict, blanks: list[str]) -> dict:
    """Construction loan carry, house model (economics `financing`): loan = loan_to_cost x (build +
    soft + land), land = the parcel's price estimate; interest = loan x rate x (build_months / 12 x
    avg_draw_during_build + months_to_sell / 12); closing = loan x loan_closing_pct. A blank input
    leaves its part out (flagged); financing_cost = the parts known, None if neither is."""
    out = {"land_in_loan": land, "loan_amount": None, "financing_interest": None, "financing_closing": None,
           "financing_cost": None}
    ltc = fin.get("loan_to_cost")
    if ltc == 0:
        return out | {"loan_amount": 0.0, "financing_interest": 0.0, "financing_closing": 0.0, "financing_cost": 0.0}
    missing = [k for k in ("loan_to_cost",) if fin.get(k) is None] + ([] if land is not None else ["land price"])
    if missing:
        blanks.extend(f"financing.{k}" for k in missing)
        return out
    loan = ltc * (build + soft + land)
    out["loan_amount"] = loan
    timing = ("build_months", "avg_draw_during_build", "months_to_sell", "construction_rate_pct")
    if gap := [k for k in timing if fin.get(k) is None]:
        blanks.extend(f"financing.{k}" for k in gap)
    else:
        out["financing_interest"] = loan * fin["construction_rate_pct"] / 100 * (
            fin["build_months"] / 12 * fin["avg_draw_during_build"] + fin["months_to_sell"] / 12)
    if fin.get("loan_closing_pct") is None:
        blanks.append("financing.loan_closing_pct")
    else:
        out["financing_closing"] = loan * fin["loan_closing_pct"] / 100
    parts = [v for v in (out["financing_interest"], out["financing_closing"]) if v is not None]
    out["financing_cost"] = sum(parts) if parts else None
    return out


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
       ST_Area(sh.geom) AS lot_sf, ST_PointOnSurface(sh.geom) AS geom
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
    frames = []
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
        stats = np.full((len(targets), 5), np.nan)
        stats[:, 0], stats[:, 1] = max(radii), 0
        if found:
            idx = np.fromiter(found, dtype=int)
            stats[idx] = np.array(list(found.values()), dtype=float)
        frames.append(pd.DataFrame({
            "pin": targets["pin"].to_numpy(), "product": product, "radius_mi": stats[:, 0],
            "comp_count": stats[:, 1].astype(int), "median_price_per_sf": stats[:, 2],
            "median_sale_price": stats[:, 3], "median_heated_sf": stats[:, 4], "thin_comps": stats[:, 1] < min_n}))
    df = pd.concat(frames, ignore_index=True)
    for c, t in COMPS_COLS.items():
        df[c] = df[c].astype(DTYPES[t])
    return df.sort_values(["pin", "product"]).reset_index(drop=True)


def lot_band(lot_sf: float | None, edges: list[float]) -> str | None:
    """Label of the lot-size band holding lot_sf: "5000-8000", ..., "43560+"."""
    if lot_sf is None or pd.isna(lot_sf):
        return None
    i = max(k for k, e in enumerate(edges) if lot_sf >= e) if lot_sf >= edges[0] else 0
    return f"{edges[i]}+" if i == len(edges) - 1 else f"{edges[i]}-{edges[i + 1]}"


def band_labels(edges: list[float]) -> list[str]:
    return [lot_band(e, edges) for e in edges]


def county_bands(comps: pd.DataFrame, edges: list[float]) -> pd.DataFrame:
    """comp_lot_bands rows: countywide single-family comps per lot band (the size-cap fallback)."""
    sfh = comps[comps["product"] == "sfh"].assign(lot_band=lambda d: [lot_band(x, edges) for x in d["lot_sf"]])
    g = sfh.groupby("lot_band")
    out = pd.DataFrame({"lot_band": band_labels(edges)}).merge(pd.DataFrame({
        "sales": g.size(), "median_price_per_sf": g["price_per_sf"].median(),
        "median_heated_sf": g["heated_sf"].median(), "median_lot_sf": g["lot_sf"].median()}).reset_index(),
        on="lot_band", how="left")
    out["sales"] = out["sales"].fillna(0).astype("Int64")
    return out


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
        comps = pd.read_sql(text(f"SELECT product, ST_X(geom) AS x, ST_Y(geom) AS y, price_per_sf, sale_price, heated_sf, "
                                 f"lot_sf FROM {s}.comp_sales ORDER BY account_pin, transfer_id, property_id"), conn)
        targets = pd.read_sql(text(
            f"SELECT y.pin, ST_X(g) AS x, ST_Y(g) AS y FROM {s}.parcel_yield y "
            f"JOIN {s}.parcels p USING (pin), ST_PointOnSurface(p.geom) g WHERE y.yield_basis = 'by_right' ORDER BY y.pin"), conn)
    changes = {"comp_sales": sales}
    df = comp_stats(targets, comps, c)
    with engine.begin() as conn:
        changes["parcel_comps"] = _store(conn, df, f"{s}.parcel_comps", ["pin", "product"], run_date)
    # single-family comps by lot-size band: the band label stands in for the product
    edges = c["lot_bands_sf"]
    sfh = comps[comps["product"] == "sfh"].assign(product=lambda d: [lot_band(x, edges) for x in d["lot_sf"]])
    df = comp_stats(targets, sfh, c | {"products": band_labels(edges)}).rename(columns={"product": "lot_band"})
    with engine.begin() as conn:
        changes["parcel_comps_lot"] = _store(conn, df, f"{s}.parcel_comps_lot", ["pin", "lot_band"], run_date)
        changes["comp_lot_bands"] = _store(conn, county_bands(comps, edges), f"{s}.comp_lot_bands", ["lot_band"], run_date)
    return changes


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
  AND c.exemption IS DISTINCT FROM :puv
"""
# Land class = parcel_features.vacant (building value 0), not CAMA vac_or_improved: ~7,800 "VAC"
# accounts carry a building with value (checked 2026-10-28). Present-use value land is left out:
# its taxable value is a fraction of market, which would inflate the ratio.
ALL = "ALL"

LAND_SQL = """
SELECT s.pin AS account_pin, s.transfer_id, s.sale_date, s.sale_price, p.gis_acres AS acres,
       coalesce(o.municipality, '(none)') AS municipality, s.sale_price / p.gis_acres AS per_acre
FROM {s}.parcel_sales s
JOIN {s}.parcel_owners o ON o.account_pin = s.pin
JOIN {s}.parcels p ON p.pin = o.pin AND p.account_count = 1
WHERE s.validity_code IS NULL AND s.sale_price > 0 AND p.gis_acres > 0
  AND s.sale_date > :start AND s.sale_date <= :as_of
  AND coalesce(o.building_value, 0) = 0 AND coalesce(s.sold_as_vacant, false)
"""


def acre_band(acres: float | None, edges: list[float]) -> str | None:
    if acres is None or pd.isna(acres):
        return None
    i = max(k for k, e in enumerate(edges) if acres >= e)
    return f"{edges[i]}+ ac" if i == len(edges) - 1 else f"{edges[i]}-{edges[i + 1]} ac"


def land_table(obs: pd.DataFrame, edges: list[float], min_sales: int) -> pd.DataFrame:
    """Median vacant-land $ per acre by municipality + acre band, and countywide band."""
    obs = obs.assign(acre_band=[acre_band(a, edges) for a in obs["acres"]])
    rows = [(m, b, len(g), g["per_acre"].median()) for (m, b), g in obs.groupby(["municipality", "acre_band"])]
    rows += [(ALL, b, len(g), g["per_acre"].median()) for b, g in obs.groupby("acre_band")]
    df = pd.DataFrame(rows, columns=["municipality", "acre_band", "sales", "median_per_acre"])
    df["usable"] = df["sales"] >= min_sales
    return df


def pick_land_price(table: pd.DataFrame, municipality: str | None, acres: float, edges: list[float]
                    ) -> tuple[float | None, str, int]:
    """($ per acre, group, sales): municipality + band if usable, else the countywide band (even if thin)."""
    band = acre_band(acres, edges)
    t = table.set_index(["municipality", "acre_band"])
    for key in ((municipality or "(none)", band), (ALL, band)):
        if key in t.index and (t.loc[key, "usable"] or key[0] == ALL):
            return float(t.loc[key, "median_per_acre"]), f"{key[0]} {band}", int(t.loc[key, "sales"])
    return None, "none", 0


def build_land_prices(engine: Engine, econ: dict, sales_as_of: date, run_date: date, s: str = "public"
                      ) -> tuple[pd.DataFrame, dict]:
    lp = econ["land_price"]
    params = {"as_of": sales_as_of, "start": window_start(sales_as_of, lp["window_months"])}
    table = land_table(pd.read_sql(text(LAND_SQL.format(s=s)), engine, params=params), lp["acre_bands"], lp["min_sales"])
    with engine.begin() as conn:
        changes = _store(conn, table, f"{s}.land_prices", ["municipality", "acre_band"], run_date)
    return table, changes


def format_land(table: pd.DataFrame, min_sales: int) -> str:
    lines = [f"Vacant land $ per acre (present-use value parcels), needs {min_sales}+ sales:"]
    for r in table.itertuples():
        lines.append(f"  {r.municipality:<36}{r.acre_band:<10}{r.sales:>6,}  ${r.median_per_acre:>12,.0f}"
                     + ("" if r.usable else "  too few"))
    return "\n".join(lines)


def ratio_observations(engine: Engine, price: dict, sales_as_of: date, s: str = "public",
                       puv: str = "AGRICULTURAL FLAG") -> pd.DataFrame:
    params = {"as_of": sales_as_of, "start": window_start(sales_as_of, price["window_months"]), "puv": puv}
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
    table = ratio_table(ratio_observations(engine, pc, sales_as_of, s, econ["land_price"]["puv_exemption"]),
                        pc["min_sales"])
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
    """(lot sf, lot width, standards rule, notes, height rule) of a new lot in each subdivision option."""
    types, alts = rule["types"], rule.get("alternatives") or {}
    lot, width = Y.type_standards(rules, j, rule, "sfh") if Y._allowed(types.get("sfh")) else (
        rule["min_lot_sf"]["value"], rule["min_lot_width_ft"]["value"] or 0)
    std = Y.standards_rule(rules, j, rule, "sfh") if Y._allowed(types.get("sfh")) else rule
    specs = {"sub_standard": (lot, width, std, [], rule)}
    if c := alts.get("conservation"):
        cut = 1 - c["lot_area_and_width_reduction_pct"] / 100
        specs["sub_conservation"] = (lot * cut, width * cut, std, [], rule)
    if c := alts.get("compact"):
        # Table 4-8 lot, width and setbacks; height 48 ft in every compact district (4.5.C.2.a.v)
        specs["sub_compact"] = (c["min_lot_sf"], c["min_lot_width_ft"],
                                {"setbacks_ft": c["setbacks_ft"], "max_building_coverage_pct": c.get("max_building_coverage_pct")},
                                [], c if c.get("max_height_ft") else rule)
    if (c := alts.get("voluntary_mixed_income")) and (target := Y.district_rule(rules, j, c["develop_as"])):
        specs["sub_mixed_income"] = (target["min_lot_sf"]["value"], target["min_lot_width_ft"]["value"], target, [], rule)
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
    split_w = Y.split_frontage(width, frontage)
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
            lot, w, std, notes, height_rule = specs[name]
            aff = 0
            if name == "sub_mixed_income":
                aff = p["sub_mixed_income_affordable_units"] if not prefix else \
                    Y.mixed_income_affordable(n - (p.get("if_crossed_sub_lots") or 0))
            out.append(_opt(prefix + name, kind, "sfh", n, 1, std, height_rule, lot, w, lot / w if w else None, True,
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


def rollback_tax(p: dict, rb: dict) -> float | None:
    """Present-use value rollback (G.S. 105-277.4(c)): deferred value x tax rate for the current
    year and the years before it, each with late-payment interest. None while the rate is blank."""
    rate = (rb.get("tax_rate_per_100") or {}).get(p.get("municipality"))
    if rate is None:
        return None
    interest = lambda i: 0 if i == 0 else (rb["interest_first_month_pct"] + rb["interest_monthly_pct"] * (12 * i - 1)) / 100
    return (p.get("deferred_value") or 0) * rate / 100 * sum(1 + interest(i) for i in range(rb["years"]))


def comp_basis(o: dict, comps: dict, econ: dict) -> dict:
    """Which comps price an option. Single-family: the comps on lots in the option's lot-size band
    within the radii; with fewer than min_comps, $/sf from all nearby single-family comps and the
    size from the band's countywide median (lot_band_fallback). Plex: townhome comps x
    duplex_discount (comp_proxy) until the plex pool has min_real_sales nearby. Townhome: its own.
    Returns comp fields and the size of one building before the zoning cap (comp_median_sf)."""
    pc, cc = econ["plex"], econ["comps"]
    if o["product"] == "sfh":
        band = lot_band(o["lot_sf"], cc["lot_bands_sf"])
        local, near = (comps.get("lot") or {}).get(band) or {}, comps.get("sfh") or {}
        if local.get("comp_count", 0) >= cc["min_comps"]:
            return {"comp_product": "sfh", "lot_band": band, "lot_band_fallback": False,
                    "comp_radius_mi": local["radius_mi"], "comp_count": local["comp_count"],
                    "price_per_sf": local["median_price_per_sf"], "comp_median_price": local["median_sale_price"],
                    "comp_median_sf": local["median_heated_sf"], "thin_comps": False, "comp_proxy": False, "_blank": False}
        county = (comps.get("county_bands") or {}).get(band) or {}
        return {"comp_product": "sfh", "lot_band": band, "lot_band_fallback": True,
                "comp_radius_mi": near.get("radius_mi"), "comp_count": near.get("comp_count", 0),
                "price_per_sf": near.get("median_price_per_sf"), "comp_median_price": near.get("median_sale_price"),
                "comp_median_sf": county.get("median_heated_sf", near.get("median_heated_sf")),
                "thin_comps": bool(near.get("thin_comps", True)), "comp_proxy": False, "_blank": False}
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
    fin = econ.get("financing") or {}
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
    if cb.get("lot_band_fallback"):
        notes.append(f"few comps on {cb['lot_band']} sf lots nearby: $/sf from all nearby single-family, "
                     "size from the band countywide")
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

    # The house each building can be: the smaller of what zoning allows on the option's lot and the
    # comps' median; priced at the comps' $/sf (both models)
    if not o["height_ft"]:
        notes.append("no height limit in the rule: max_stories used")
    sizes = [x for x in (zoning_max, cb["comp_median_sf"]) if x is not None]
    size = min(sizes) if sizes else None
    if zoning_max is None:
        notes.append("zoning size unknown (no lot width/depth or coverage)")
    row |= {"envelope_sf": envelope, "coverage_sf": coverage, "footprint_sf": fp, "height_ft": o["height_ft"],
            "stories": stories, "zoning_max_sf": zoning_max, "size_sf": size}
    ppsf = cb["price_per_sf"]

    def no_value(reason: str) -> dict:
        notes.append(reason)
        return row | {"notes": "; ".join(dict.fromkeys(notes)) or None, "blank_inputs": ", ".join(blanks) or None}

    if ppsf is None:
        return no_value("no comps within the widest radius" if "duplex_discount" not in blanks else "no plex price")
    if size is None or size <= 0:
        return no_value("no buildable house size")
    home = ppsf * size                 # one building
    row["home_price"] = home
    if o["model"] == "lot":
        ratio = econ["subdivision"].get("lot_to_home_ratio")
        if ratio is None:
            blanks.append("lot_to_home_ratio")
            return no_value("no finished lot price")
        lot_price = ratio * home
        revenue = units * lot_price
        unit_price, house_fp = home, None
        dev = item("development_cost_per_lot", units, econ["subdivision"].get("development_cost_per_lot"))
        margin = revenue       # lots sold to builders: no selling cost or construction loan here
        row |= {"lot_price": lot_price, "development_cost": dev}
    else:
        key = BUILD_COST_KEY[o["product"]]
        rate = (costs["build_cost_per_sf"] or {}).get(key)
        if rate is None:
            blanks.append(f"build_cost_per_sf.{key}")
            return no_value(f"no build cost for {key}")
        revenue = buildings * home
        build = buildings * rate * size
        soft = build * costs["soft_cost_pct"] / 100
        profit = revenue * costs["target_profit_pct"] / 100
        pct = (econ.get("selling") or {}).get("selling_cost_pct")
        selling = item("selling_cost_pct", revenue, None if pct is None else pct / 100)
        fin_row = financing(build, soft, p.get("_price"), fin, blanks)
        unit_price, house_fp, dev = home / upb, size / stories, 0.0
        margin = revenue - build - soft - profit - (selling or 0) - (fin_row["financing_cost"] or 0)
        row |= {"build_cost": build, "soft_cost": soft, "profit": profit, "selling_cost": selling} | fin_row

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
    rollback = item("rollback_tax_rate", 1 if p.get("puv_enrolled") else 0, rollback_tax(p, econ["rollback_tax"]))
    site_work = sum(v for v in site.values() if v is not None)
    rlv = margin - discount - (dev or 0) + (adu or 0) + retained - (demo or 0) - site_work - (oblig or 0) - (rollback or 0)
    return row | site | {
        "unit_price": unit_price, "revenue": revenue, "absorption_months": months, "discount_factor": factor,
        "discount_cost": discount, "adu_premium": adu, "retained_value": retained, "demolition_cost": demo,
        "sewer_ft": sewer_ft, "road_ft": road_ft, "slope_acres": slope_acres, "floodplain_units": fp_units,
        "site_work": site_work, "obligation_cost": oblig, "rollback_cost": rollback, "rlv": rlv,
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
    uses = asset_uses(p)
    if p.get("addresses") and not p.get("occupiable_addresses") and uses & set(score_cfg["utility_site_uses"]):
        return "utility_site", f"every county address is unoccupiable, incl. {', '.join(sorted(uses))}"
    occupied = address_uses(p)
    cats = institutional_categories(occupied, score_cfg)
    if occupied and all(cats.get(u) for u in occupied) and not institutional_guarded(p, score_cfg):
        first = next(c for c in score_cfg["institutional_address_uses"] if c in cats.values())
        return first, f"every occupiable county address is institutional: {', '.join(sorted(occupied))}"
    return None, None


def institutional_guarded(p: dict, score_cfg: dict) -> bool:
    """A residential land use whose owner is not an institution: the institutional address use is
    likely stale or a home business (economics score.institutional_guard), so flag, don't exclude."""
    g = score_cfg["institutional_guard"]
    lu = (p.get("land_use") or "").upper()
    if not any(fnmatch.fnmatchcase(lu, pat) for pat in g["residential_land_uses"]):
        return False
    if p.get("owner_type") in g["institution_owner_types"]:
        return False
    words = "|".join(re.escape(w) for w in g["institution_owner_words"])
    return not re.search(rf"\b(?:{words})\b", (p.get("owner_name") or "").upper())


def address_uses(p: dict) -> set[str]:
    """Uses of the parcel's occupiable (non-ASSET) county addresses ("USE CODE nn" where blank)."""
    return set((p.get("occupiable_address_uses") or "").split("; ")) - {""}


def institutional_categories(uses: set[str], score_cfg: dict) -> dict[str, str]:
    """{address use: category} for the institutional ones among `uses` (first category wins)."""
    out = {}
    for cat, names in score_cfg["institutional_address_uses"].items():
        for u in uses & set(names):
            out.setdefault(u, cat)
    return out


def asset_uses(p: dict) -> set[str]:
    """Uses of the parcel's unoccupiable (ASSET) county addresses."""
    return set((p.get("asset_address_uses") or "").split("; ")) - {""}


def adds_units(o: dict | None, p: dict) -> bool:
    """Does the option add dwellings or lots? (an existing building counts as one unit)"""
    return bool(o) and (o["option"].startswith(("split_", "sub_", "townhome", "if_crossed_"))
                        or o["units"] > (0 if p.get("vacant") else 1))


def score_parcel(p: dict, comps: dict, rules: dict, econ: dict, scoring_cfg: dict,
                 ratios: pd.DataFrame, land: pd.DataFrame | None = None) -> tuple[dict, list[dict]]:
    """(parcel_rlv row, parcel_rlv_options rows) for one parcel. `land`: the vacant land $ per acre
    table, used to price present-use value parcels."""
    basis = sewer_basis(p)
    land_class = "vacant" if p.get("vacant") else "improved"
    ratio, group, n = pick_ratio(ratios, p.get("municipality"), land_class)
    assessed = p.get("total_value")
    price = assessed * ratio if assessed is not None and ratio is not None else None
    price_basis, per_acre, land_group, land_n = "assessed_x_ratio", None, None, None
    if p.get("puv_enrolled") and land is not None:
        acres = (p.get("area_sf") or 0) / SQFT_PER_ACRE
        per_acre, land_group, land_n = pick_land_price(land, p.get("municipality"), acres, econ["land_price"]["acre_bands"])
        price = per_acre * acres if per_acre is not None else None
        price_basis = "vacant_land_per_acre"
    p = p | {"_ratio": ratio, "_price": price}
    public = [value_option(o, p, comps, econ, basis, "public") for o in yield_options(p, rules, scoring_cfg)]
    septic = [value_option(o, p, comps, econ, basis, "septic")
              for o in septic_options(p, rules, scoring_cfg, econ["sewer"]["septic_min_lot_sf"])] \
        if basis == "none_outside" else []
    scored = septic if basis == "none_outside" else public
    b = best(scored, "by_right")
    if_sewer = best(public, "by_right") if basis == "none_outside" else None
    rez, crossed = best(public, "rezoning"), best(public, "if_crossed")

    category, reason = exclusion(p, econ["score"])
    commercial = any(fnmatch.fnmatchcase((p.get("land_use") or "").upper(), pat)
                     for pat in econ["score"]["commercial_land_uses"])
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
        "ratio_group": group, "ratio_sales": n, "puv_enrolled": bool(p.get("puv_enrolled")),
        "deferred_value": p.get("deferred_value"), "land_price_per_acre": per_acre, "land_price_group": land_group,
        "land_price_sales": land_n, "price": price, "price_basis": price_basis,
        "score": score, "exclusion_category": category, "excluded_reason": reason,
        "redevelopment_watch": category in econ["score"]["redevelopment_watch"], "existing_commercial_use": commercial,
        "addresses": p.get("addresses"), "occupiable_addresses": p.get("occupiable_addresses"),
        "asset_address_uses": p.get("asset_address_uses"),
        "owner_name": p.get("owner_name"), "occupiable_address_uses": p.get("occupiable_address_uses"),
        "utility_address": bool(asset_uses(p) & set(econ["score"]["utility_address_uses"])),
        "institutional_address": bool(institutional_categories(address_uses(p), econ["score"])) and not reason,
        "verify_sewer": basis == "assumed_in_town", "sewer_low_confidence": low_conf,
        "sewer_assumed_new_units": low_conf and adds_units(b, p),
        "thin_comps": bool(b and b["thin_comps"]), "comp_proxy": bool(b and b["comp_proxy"]),
        "lot_band_fallback": bool(b and b["lot_band_fallback"]), "over_project_cap": bool(b and b["over_project_cap"]),
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
       c.septic_dominant_rating, h.existing_heated_sf, coalesce(h.puv_enrolled, false) AS puv_enrolled, h.deferred_value,
       coalesce(ad.addresses, 0) AS addresses, coalesce(ad.occupiable_addresses, 0) AS occupiable_addresses,
       ad.asset_address_uses, ad.occupiable_address_uses,
       (SELECT min(owner_name) FROM {s}.parcel_owners o WHERE o.pin = y.pin) AS owner_name
FROM {s}.parcel_yield y JOIN {s}.parcel_features f USING (pin)
LEFT JOIN {s}.parcel_constraints c USING (pin)
LEFT JOIN LATERAL (SELECT sum(heated_sf) AS existing_heated_sf, bool_or(exemption = :puv) AS puv_enrolled,
                          sum(market_value - total_value) FILTER (WHERE exemption = :puv) AS deferred_value
                   FROM {s}.parcel_cama a WHERE a.pin = y.pin) h ON true
LEFT JOIN LATERAL (SELECT count(*) AS addresses, count(*) FILTER (WHERE address_model IS DISTINCT FROM 'ASSET')
                          AS occupiable_addresses,
                          string_agg(DISTINCT address_use, '; ' ORDER BY address_use)
                              FILTER (WHERE address_model = 'ASSET') AS asset_address_uses,
                          string_agg(DISTINCT coalesce(address_use, 'USE CODE ' || use_code), '; '
                                     ORDER BY coalesce(address_use, 'USE CODE ' || use_code))
                              FILTER (WHERE address_model IS DISTINCT FROM 'ASSET') AS occupiable_address_uses
                   FROM {s}.parcel_addresses a WHERE a.pin = y.pin) ad ON true
WHERE y.yield_basis = 'by_right' AND y.pin BETWEEN :lo AND :hi
ORDER BY y.pin
"""


def _records(df: pd.DataFrame) -> list[dict]:
    return [{k: (None if v is None or (not isinstance(v, str) and pd.isna(v)) else v) for k, v in r.items()}
            for r in df.drop(columns=["row_hash", "valid_from"], errors="ignore").to_dict("records")]


def comps_by_pin(df: pd.DataFrame, lot: pd.DataFrame | None = None, county: dict | None = None) -> dict[str, dict]:
    """{pin: {product: parcel_comps row, "lot": {band: parcel_comps_lot row}, "county_bands": {...}}}"""
    out: dict[str, dict] = {}
    for r in _records(df):
        out.setdefault(r["pin"], {"county_bands": county or {}})[r["product"]] = r
    for r in _records(lot) if lot is not None else []:
        out.setdefault(r["pin"], {"county_bands": county or {}}).setdefault("lot", {})[r["lot_band"]] = r
    return out


def run_rlv(engine: Engine, econ: dict, scoring_cfg: dict, rules: dict, ratios: pd.DataFrame, run_date: date,
            s: str = "public", chunk: int = 2_000, land: pd.DataFrame | None = None) -> tuple[pd.DataFrame, dict]:
    """Value every option of every by-right parcel and store both tables. Returns (parcel_rlv, changes).
    Parcels are read `chunk` at a time by pin range: the whole input does not fit in this PC's memory."""
    with engine.connect() as conn:
        pins = [r[0] for r in conn.execute(text(f"SELECT pin FROM {s}.parcel_yield WHERE yield_basis = 'by_right' ORDER BY pin"))]
        county = {r["lot_band"]: r for r in _records(pd.read_sql(text(f"SELECT * FROM {s}.comp_lot_bands"), conn))}
    ranges = [(pins[i], pins[min(i + chunk, len(pins)) - 1]) for i in range(0, len(pins), chunk)] or [("", "")]
    with engine.begin() as conn:
        for i, (lo, hi) in enumerate(ranges):
            bounds = {"lo": lo, "hi": hi}
            inputs = pd.read_sql(text(INPUT_SQL.format(s=s)), conn,
                                 params=bounds | {"puv": econ["land_price"]["puv_exemption"]})
            read = lambda t: pd.read_sql(text(f"SELECT * FROM {s}.{t} WHERE pin BETWEEN :lo AND :hi"), conn, params=bounds)
            comps = comps_by_pin(read("parcel_comps"), read("parcel_comps_lot"), county)
            rows, options = [], []
            for p in _records(inputs):
                row, opts = score_parcel(p, comps.get(p["pin"], {}), rules, econ, scoring_cfg, ratios, land)
                rows.append(row)
                options += opts
            # both tables go to staging batch by batch: nothing accumulates in memory
            _store(conn, _frame(rows, PARCEL_COLS), f"{s}.parcel_rlv", ["pin"], run_date, append=i > 0, finish=False)
            _store(conn, _frame(options, OPTION_COLS), f"{s}.parcel_rlv_options", ["pin", "option", "sewer"],
                   run_date, append=i > 0, finish=False)
            del inputs, comps, rows, options
            gc.collect()
        changes = {"parcel_rlv_options": apply_versioned(conn, f"{s}.parcel_rlv_options", ["pin", "option", "sewer"], run_date),
                   "parcel_rlv": apply_versioned(conn, f"{s}.parcel_rlv", ["pin"], run_date)}
        cols = ["sewer_basis", "best_option", "best_rlv", "score", "exclusion_category", "redevelopment_watch", *FLAG_COLS]
        df = pd.read_sql(text(f"SELECT {', '.join(cols)} FROM {s}.parcel_rlv"), conn)
    return df, changes


# ---------------------------------------------------------------- printing

def _money(x) -> str:
    return "blank" if x is None or pd.isna(x) else f"-${-x:,.0f}" if x < 0 else f"${x:,.0f}"


def _n(x, fmt: str = ",.0f") -> str:
    return "-" if x is None or pd.isna(x) else format(x, fmt)


FLAG_COLS = ["verify_sewer", "sewer_low_confidence", "sewer_assumed_new_units", "thin_comps", "comp_proxy",
             "lot_band_fallback", "over_project_cap", "rlv_incomplete", "verify_acreage", "septic_soils_very_limited",
             "puv_enrolled", "existing_commercial_use", "utility_address", "institutional_address"]


def explain(p: pd.Series, o: pd.Series | None, econ: dict) -> list[str]:
    """Every input behind a parcel's score, as printable lines."""
    costs, sw, sub, proj = econ["costs"], econ["site_work"], econ["subdivision"], econ["project"]
    fin = econ.get("financing") or {}
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
    band = "" if pd.isna(o["lot_band"]) else (
        f" (all lots; size from {o['lot_band']} sf lots countywide)" if o["lot_band_fallback"] else f" on {o['lot_band']} sf lots")
    comp = (f"    comps: {o['comp_count']} {o['comp_product']} sales{band} within {_n(o['comp_radius_mi'], 'g')} mi"
            f"{' (THIN)' if o['thin_comps'] else ''}"
            f"{' x duplex_discount ' + str(econ['plex']['duplex_discount']) + ' (PROXY)' if o['comp_proxy'] else ''}: "
            f"median ${_n(o['price_per_sf'])}/sf, {_n(o['comp_median_sf'])} sf, sale {_money(o['comp_median_price'])}"
            f"  [comp_sales; comps_for('{p['pin']}', '{o['comp_product']}')]")
    house = (f"    lot {_n(o['lot_sf'])} sf, {_n(o['lot_width_ft'], ',.1f')} x {_n(o['lot_depth_ft'], ',.1f')} ft; "
             f"envelope {_n(o['envelope_sf'])} sf, coverage limit {_n(o['coverage_sf'])} sf -> footprint "
             f"{_n(o['footprint_sf'])} sf x {o['stories']} floors (height {_n(o['height_ft'], 'g')} ft) = zoning max "
             f"{_n(o['zoning_max_sf'])} sf")
    size = (f"    house size {_n(o['size_sf'])} sf per building (smaller of comps' median and zoning max) x "
            f"${_n(o['price_per_sf'])}/sf = home {_money(o['home_price'])}")
    if o["model"] == "lot":
        lines += [house, comp, size,
                  f"    finished lot price {sub['lot_to_home_ratio']} x home {_money(o['home_price'])} "
                  f"= {_money(o['lot_price'])}",
                  f"    lot sales  {o['units']} x {_money(o['lot_price'])} = {_money(o['revenue'])}",
                  f"    development {o['units']} x {_money(sub.get('development_cost_per_lot'))} = "
                  f"-{_money(o['development_cost'])}"]
    else:
        b = o["buildings"]
        lines += [house, comp, size + f", unit {_money(o['unit_price'])}",
                  f"    revenue    {b:g} x {_n(o['size_sf'])} sf x ${_n(o['price_per_sf'])} = {_money(o['revenue'])}",
                  f"    build cost {b:g} x {_n(o['size_sf'])} sf x "
                  f"{_money(costs['build_cost_per_sf'].get(BUILD_COST_KEY[o['product']]))} "
                  f"({BUILD_COST_KEY[o['product']]}) = -{_money(o['build_cost'])}",
                  f"    soft cost  {costs['soft_cost_pct']}% of build = -{_money(o['soft_cost'])}",
                  f"    selling    {(econ.get('selling') or {}).get('selling_cost_pct')}% of revenue = "
                  f"-{_money(o['selling_cost'])}",
                  f"    financing  loan {fin.get('loan_to_cost')} x (build + soft + land {_money(o['land_in_loan'])}) = "
                  f"{_money(o['loan_amount'])}; interest at {fin.get('construction_rate_pct')}% x "
                  f"({fin.get('build_months')} mo / 12 x {fin.get('avg_draw_during_build')} + "
                  f"{fin.get('months_to_sell')} mo / 12) = {_money(o['financing_interest'])}; closing "
                  f"{fin.get('loan_closing_pct')}% = {_money(o['financing_closing'])} -> -{_money(o['financing_cost'])}",
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
        f"    obligations {o['affordable_units']} affordable units = -{_money(o['obligation_cost'])}"
        + (f"; present-use rollback tax on {_money(p['deferred_value'])} deferred = -{_money(o['rollback_cost'])}"
           if p["puv_enrolled"] else ""),
        f"    RLV = {_money(o['rlv'])}" + (f"  (upper bound: blank {o['blank_inputs']})" if pd.notna(o["blank_inputs"]) else ""),
        f"  price: {_n(p['area_sf'] / SQFT_PER_ACRE, ',.2f')} ac x {_money(p['land_price_per_acre'])}/ac vacant land "
        f"({p['land_price_group']}, {p['land_price_sales']} sales) = {_money(p['price'])}  (present-use value parcel; "
        f"assessed {_money(p['assessed_value'])})  [land_prices]"
        if p["price_basis"] == "vacant_land_per_acre" else
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
    land, land_changes = build_land_prices(engine, econ, sales_as_of, run_date)
    print("\n" + format_land(land, econ["land_price"]["min_sales"]))
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

    log = {"ratios": ratio_changes, "land_prices": land_changes, **comp_changes, "economics": econ}
    missing = blank_core(econ)
    if missing:
        print(f"\nRLV not computed: fill in {', '.join(missing)} under `costs` in config/economics.yaml.")
    else:
        df, rlv_changes = run_rlv(engine, econ, scoring_cfg, load_rules(), table, run_date, land=land)
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
