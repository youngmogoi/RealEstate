"""Phase 2.2: what each parcel can hold under its zoning, stored in `parcel_yield`.

Run with `make yield` (after `make features`). Every option is stored; nothing here picks a
winner (RLV, Phase 2.3, does). Rules come from config/zoning_rules.yaml; product and ratio
settings from config/scoring.yaml (`yield`, `townhome`).

The module name is a Python keyword: import it with importlib.import_module("scoring.yield").

Per parcel (one row, keyed by pin; history in parcel_yield_history):
- cur_<type>: units on the lot as it is, for sfh, duplex, triplex, quadraplex, if the lot meets
  THAT type's lot conditions. Lot area is GROSS (the whole lot, as zoning measures it; the
  smaller of shape and assessor acres when they disagree, flag verify_acreage) and lot width is
  parcel_features.lot_width_ft. Lot standards apply to the parent parcel, not sublots.
- split_lots / split_<type>: conforming lots the parcel divides into (by gross area and by lot
  width), and per-type units on those lots. 1 = no split possible.
- sub_lots: standard subdivision, NET buildable acres x usable_ratio / min lot size.
  sub_conservation_lots, sub_compact_lots, sub_mixed_income_lots: the UDO 4.5 options, each
  only where the district offers it and the site qualifies; their obligations are in
  `obligations` (JSON), with quadraplex affordability.
- townhome_units: by right where allowed; otherwise the rezoning yield in the district the
  2040 Policy Map place type points to (needs_rezoning, speculative, target stored).
- adu_allowed: yes/no only (it adds value to the main unit; never a unit to sell).
- Flags: no_frontage, setback_varies, reduced_depth_possible, multi_dwelling_possible,
  arterial_frontage, apartments_possible, verify_acreage; site_plan_governs or no_zoning_rule
  when there is no by-right yield (all yield columns NULL). `notes` lists checks to make by
  hand (through-lot quadraplex, compact exclusion areas, split zoning).
- Traceability: the inputs used (zone_code, rule_status, area_sf, net_sf, lot_width_ft,
  frontage_ft, place_type) are stored with the yields; rule_source names the ordinance.
"""

import json
import math
from datetime import date
from pathlib import Path

import pandas as pd
import yaml
from sqlalchemy import Engine, text

from ingest.db import copy_dataframe, get_engine
from ingest.versioning import apply_versioned, row_hash
from scoring.zoning_rules import by_right_usable, load_rules, parse_code

ROOT = Path(__file__).resolve().parent.parent
SQFT_PER_ACRE = 43_560
TYPES = ["sfh", "duplex", "triplex", "quadraplex"]
ALLOWED = {True: "yes", False: "no"}

COLUMNS = [
    "pin", "jurisdiction", "zone_code", "base_district", "rule_status", "rule_source", "yield_basis",
    "area_sf", "net_sf", "lot_width_ft", "frontage_ft", "place_type",
    *[f"cur_{t}" for t in TYPES], "split_lots", *[f"split_{t}" for t in TYPES],
    "sub_lots", "sub_conservation_lots", "sub_compact_lots", "sub_mixed_income_lots",
    "sub_mixed_income_affordable_units",
    "townhome_units", "townhome_needs_rezoning", "townhome_target_district", "townhome_speculative",
    "adu_allowed",
    "no_frontage", "setback_varies", "reduced_depth_possible", "multi_dwelling_possible", "arterial_frontage",
    "apartments_possible", "verify_acreage", "site_plan_governs", "no_zoning_rule",
    "obligations", "notes",
]
INT_COLS = [*[f"cur_{t}" for t in TYPES], "split_lots", *[f"split_{t}" for t in TYPES], "sub_lots",
            "sub_conservation_lots", "sub_compact_lots", "sub_mixed_income_lots",
            "sub_mixed_income_affordable_units", "townhome_units"]
BOOL_COLS = ["townhome_needs_rezoning", "townhome_speculative", "adu_allowed", "no_frontage", "setback_varies",
             "reduced_depth_possible", "multi_dwelling_possible", "arterial_frontage", "apartments_possible",
             "verify_acreage", "site_plan_governs", "no_zoning_rule"]


def _allowed(e: dict | None) -> bool:
    v = (e or {}).get("allowed")
    return ALLOWED.get(v, v) in ("yes", "conditional") if isinstance(v, bool | str) else False


def _floor(x: float) -> int:
    return int(math.floor(x + 1e-9))   # 10,000 sf / 10,000 sf is one lot despite float noise


def district_rule(rules: dict, jurisdiction: str, base: str) -> dict | None:
    rule = ((rules.get("districts") or {}).get(jurisdiction) or {}).get(base)
    return rule if by_right_usable(rule) else None


def type_standards(rules: dict, jurisdiction: str, rule: dict, t: str) -> tuple[float, float]:
    """(min lot sf, min lot width ft) a housing type must meet: its district's, or those of the
    district it must be built to (`requires.develop_as`, e.g. N2-B duplexes use N1-E)."""
    req = (rule["types"].get(t) or {}).get("requires") or {}
    std = district_rule(rules, jurisdiction, req["develop_as"]) if req.get("develop_as") else rule
    if std is None:
        raise ValueError(f"{jurisdiction} {req.get('develop_as')}: develop_as district has no usable rule")
    return std["min_lot_sf"]["value"], std["min_lot_width_ft"]["value"] or 0


def lots(area_sf: float, width_ft: float | None, lot_sf: float, lot_width: float) -> int:
    """Conforming lots by area and, when a width is known, by width (0 if not even one)."""
    by_area = _floor(area_sf / lot_sf)
    if width_ft is None or not lot_width:
        return by_area
    return min(by_area, _floor(width_ft / lot_width))


def coverage_pct(rule: dict, lot_sf: float) -> float:
    c = rule.get("max_building_coverage_pct") or {}
    if "value" in c:
        return c["value"]
    return c["lots_10000_sf_plus"] if lot_sf >= 10_000 else c["smaller_lots"]


def townhome_units(rule: dict, site_sf: float, width_ft: float | None, th: dict) -> int:
    """Townhome (MF attached) units the rule allows on `site_sf` of usable land: the least of
    the per-building lot area, principal-structure, building coverage and open space limits."""
    req = rule["types"]["townhome"].get("requires") or {}
    min_width = req.get("min_lot_width_ft") or 0
    if site_sf <= 0 or (width_ft is not None and width_ft < min_width):
        return 0
    per_building = _floor(req["max_building_length_ft"] / th["unit_width_ft"])
    footprint = th["unit_width_ft"] * th["unit_depth_ft"]
    bounds = [_floor(site_sf * coverage_pct(rule, site_sf) / 100 / footprint)]
    if req.get("min_lot_sf_per_building"):
        bounds.append(_floor(site_sf / req["min_lot_sf_per_building"]) * per_building)
    if req.get("max_principal_structures"):
        n_lots = max(_floor(site_sf / rule["min_lot_sf"]["value"]), 0)
        bounds.append(n_lots * req["max_principal_structures"] * per_building)
    if req.get("open_space_sf_per_unit"):
        bounds.append(_floor(site_sf / (footprint + req["open_space_sf_per_unit"])))
    return max(min(bounds), 0)


def type_conditions(e: dict, p: dict) -> tuple[bool, list[str]]:
    """Does the parcel meet a type's lot conditions beyond area and width? (ok, notes)"""
    req = e.get("requires") or {}
    if req.get("arterial_frontage") and not p["arterial_frontage"]:
        return False, []
    if req.get("through_lot_both_frontages_arterial") and p.get("through_lot"):
        return True, ["quadraplex: through lot, check both frontages are arterial"]
    return True, []


def compute_yield(p: dict, rules: dict, cfg: dict) -> dict:
    """Yield for one parcel. `p` holds the parcel_features inputs (see INPUT_SQL)."""
    j, code = p["jurisdiction"], p["zone_code"]
    out = dict.fromkeys(COLUMNS)
    out |= {"pin": p["pin"], "jurisdiction": j, "zone_code": code, "lot_width_ft": p.get("lot_width_ft"),
            "frontage_ft": p.get("frontage_ft"), "place_type": p.get("place_type")}
    out |= dict.fromkeys(BOOL_COLS, False)
    out["no_frontage"] = not (p.get("frontage_ft") or 0) > 0
    out["arterial_frontage"] = bool(p.get("arterial_frontage"))
    notes: list[str] = ["split zoning: main district only"] if p.get("split_zoning") else []

    area_sf = (p.get("gross_acres") or 0) * SQFT_PER_ACRE
    if p.get("acres_mismatch") and p.get("assessor_acres"):
        area_sf = min(area_sf, p["assessor_acres"] * SQFT_PER_ACRE)
        out["verify_acreage"] = True
    net_sf = min((p.get("net_buildable_acres") or 0) * SQFT_PER_ACRE, area_sf)
    out["area_sf"], out["net_sf"] = round(area_sf, 1), round(net_sf, 1)

    if code is None:
        out |= {"yield_basis": "no_zoning_rule", "no_zoning_rule": True, "notes": "; ".join(notes) or None}
        return out
    base, mods = parse_code(j, code, rules["code_parsing"])
    raw = ((rules.get("districts") or {}).get(j) or {}).get(base)
    out["base_district"] = base
    out["rule_status"] = (raw or {}).get("status")
    if any(k == "site_plan" for _, k in mods) or (raw or {}).get("housing") == "site_plan":
        out |= {"yield_basis": "site_plan_governs", "site_plan_governs": True,
                "rule_source": (raw or {}).get("source"), "notes": "; ".join(notes) or None}
        return out
    rule = district_rule(rules, j, base)
    if rule is None:
        out |= {"yield_basis": "no_zoning_rule", "no_zoning_rule": True, "notes": "; ".join(notes) or None}
        return out
    out["yield_basis"] = "by_right"
    out["rule_source"] = rule.get("ordinance")
    y, th = cfg["yield"], cfg["townhome"]
    width = p.get("lot_width_ft")
    types, alts = rule["types"], rule.get("alternatives") or {}
    obligations: dict = {}

    # The lot as it is, and lot splits, per housing type
    lot_sf, lot_width = type_standards(rules, j, rule, "sfh") if _allowed(types.get("sfh")) else (
        rule["min_lot_sf"]["value"], rule["min_lot_width_ft"]["value"] or 0)
    out["split_lots"] = lots(area_sf, width, lot_sf, lot_width)
    for t in TYPES:
        e = types.get(t)
        out[f"cur_{t}"] = out[f"split_{t}"] = 0
        if not _allowed(e):
            continue
        ok, t_notes = type_conditions(e, out | p)
        if not ok:
            continue
        t_lot, t_width = type_standards(rules, j, rule, t)
        n = lots(area_sf, width, t_lot, t_width)
        if n >= 1:
            out[f"cur_{t}"] = e["units_per_building"]
            out[f"split_{t}"] = n * e["units_per_building"]
            notes += t_notes
            req = e.get("requires") or {}
            if req.get("affordable_units"):
                obligations[t] = {"affordable_units_per_building": req["affordable_units"],
                                  "ami_pct": req.get("affordable_ami_pct"), "years": req.get("affordable_years")}

    # Subdivision options (NET buildable land)
    usable_sf = net_sf * y["usable_ratio"]
    out["sub_lots"] = _floor(usable_sf / lot_sf)
    acres = area_sf / SQFT_PER_ACRE
    if (c := alts.get("conservation")) and acres >= c["min_site_acres"]:
        reduced = lot_sf * (1 - c["lot_area_and_width_reduction_pct"] / 100)
        out["sub_conservation_lots"] = min(_floor(area_sf / lot_sf), _floor(usable_sf / reduced))
        obligations["conservation"] = {"conservation_area_pct": c["conservation_area_pct"],
                                       "source": c["source"], "not_combined_with": c["not_combined_with"]}
    if (c := alts.get("compact")) and acres >= c["min_parcel_acres"]:
        out["sub_compact_lots"] = _floor(usable_sf * (1 - c["common_open_space_pct"] / 100) / c["min_lot_sf"])
        obligations["compact"] = {"common_open_space_pct": c["common_open_space_pct"],
                                  "max_lots_fronting_open_space_pct": c["max_lots_fronting_open_space_pct"],
                                  "source": c["source"], "not_combined_with": c["not_combined_with"]}
        notes.append("compact: check airport 65 DNL and critical/protected watershed exclusions")
    if (c := alts.get("voluntary_mixed_income")) and acres >= c["min_site_acres"]:
        target = district_rule(rules, j, c["develop_as"])
        if target:
            total = _floor(usable_sf / target["min_lot_sf"]["value"])
            bonus = total - out["sub_lots"]
            if bonus >= 1:
                affordable = max(2, math.ceil(bonus / 2))
                out["sub_mixed_income_lots"] = total
                out["sub_mixed_income_affordable_units"] = affordable
                obligations["mixed_income"] = {
                    "develop_as": c["develop_as"], "bonus_lots": bonus, "affordable_units": affordable,
                    "rule": c["affordable"], "source": c["source"], "not_combined_with": c["not_combined_with"]}

    # Townhome: by right, or the rezoning yield in the place type's target district
    if _allowed(types.get("townhome")):
        out["townhome_units"] = townhome_units(rule, usable_sf, width, th)
    else:
        out["townhome_units"] = 0
        tgt = (((rules.get("rezoning_targets") or {}).get(j) or {}).get("townhome") or {})
        target_code = (tgt.get("by_place_type") or {}).get(p.get("place_type"))
        target = district_rule(rules, j, target_code) if target_code else None
        if (types.get("townhome") or {}).get("rezoning", {}).get("needs_rezoning") and target:
            out |= {"townhome_units": townhome_units(target, usable_sf, width, th), "townhome_needs_rezoning": True,
                    "townhome_target_district": target_code, "townhome_speculative": bool(tgt.get("speculative"))}

    out["adu_allowed"] = _allowed(types.get("adu")) and any(
        out[f"cur_{t}"] for t in (types["adu"].get("requires") or {}).get("accessory_to", []) if t in TYPES)

    # Flags
    flags = rule.get("flags") or {}
    out["setback_varies"] = "setback_varies" in flags and (p.get("blockface_dwellings") or 0) >= y["blockface_min_dwellings"]
    out["reduced_depth_possible"] = "reduced_depth_possible" in flags and out["arterial_frontage"]
    mdd = types.get("multi_dwelling_development")
    out["multi_dwelling_possible"] = _allowed(mdd) and area_sf >= 2 * lot_sf
    out["apartments_possible"] = _allowed(types.get("multifamily_stacked"))
    out["obligations"] = json.dumps(obligations, sort_keys=True) if obligations else None
    out["notes"] = "; ".join(dict.fromkeys(notes)) or None
    return out


# Helper tables first (indexed), then one row of inputs per parcel.
PREP_SQL = [
    # arterial frontage: a street-fronting parcel within reach of an arterial centerline
    """CREATE TEMP TABLE yield_arterial ON COMMIT DROP AS
       SELECT DISTINCT p.pin FROM {s}.parcels p
       JOIN {s}.parcel_features f USING (pin)
       JOIN {s}.streets st ON st.thoroughfare = ANY(:arterial) AND ST_DWithin(p.geom, st.geom, :reach)
       WHERE f.frontage_ft > 0""",
    """CREATE TEMP TABLE yield_developed ON COMMIT DROP AS
       SELECT pin, ST_PointOnSurface(geom) AS g FROM {s}.parcels WHERE coalesce(building_value, 0) > 0""",
    "CREATE INDEX ON yield_developed USING gist (g)",
    "ANALYZE yield_developed",
    # developed parcels near each parcel: the established-blockface approximation
    """CREATE TEMP TABLE yield_blockface ON COMMIT DROP AS
       SELECT p.pin, count(*) AS n FROM {s}.parcels p
       JOIN yield_developed d ON d.pin <> p.pin AND ST_DWithin(ST_PointOnSurface(p.geom), d.g, :radius)
       GROUP BY p.pin""",
]

INPUT_SQL = """
SELECT f.pin, f.zoning_jurisdiction AS jurisdiction, f.zone_code, f.split_zoning, f.gross_acres,
       p.assessor_acres, coalesce(p.acres_mismatch, false) AS acres_mismatch, f.net_buildable_acres,
       f.lot_width_ft, f.frontage_ft, f.through_lot, f.place_type,
       a.pin IS NOT NULL AS arterial_frontage, coalesce(b.n, 0) AS blockface_dwellings
FROM {s}.parcel_features f
JOIN {s}.parcels p USING (pin)
LEFT JOIN yield_arterial a USING (pin)
LEFT JOIN yield_blockface b USING (pin)
ORDER BY f.pin
"""


def load_inputs(engine: Engine, cfg: dict, schema: str = "public") -> pd.DataFrame:
    y = cfg["yield"]
    params = {"arterial": y["arterial_thoroughfare"], "reach": y["arterial_reach_ft"], "radius": y["blockface_radius_ft"]}
    with engine.begin() as conn:
        for stmt in PREP_SQL:
            sql = stmt.format(s=schema)
            conn.execute(text(sql), {k: v for k, v in params.items() if f":{k}" in sql})
        return pd.read_sql(text(INPUT_SQL.format(s=schema)), conn)


def build_frame(inputs: pd.DataFrame, rules: dict, cfg: dict) -> pd.DataFrame:
    records = [compute_yield({k: (None if pd.isna(v) else v) for k, v in r.items()}, rules, cfg)
               for r in inputs.to_dict("records")]
    df = pd.DataFrame(records, columns=COLUMNS)
    for c in INT_COLS:
        df[c] = df[c].astype("Int64")
    for c in BOOL_COLS:
        df[c] = df[c].astype("boolean")
    return df


def store(df: pd.DataFrame, engine: Engine, as_of: date, schema: str = "public") -> dict:
    table = f"{schema}.parcel_yield"
    with engine.begin() as conn:
        copy_dataframe(conn, df.assign(row_hash=row_hash(df)), f"{table}_staging", {"obligations": "jsonb"})
        return apply_versioned(conn, table, ["pin"], as_of)


def summary(df: pd.DataFrame) -> str:
    by = df["yield_basis"].value_counts()
    br = df[df["yield_basis"] == "by_right"]
    lines = [f"Parcel yield: {len(df):,} parcels",
             "  basis: " + ", ".join(f"{k} {v:,}" for k, v in by.items()),
             "  by-right lots that conform today: " + ", ".join(
                 f"{t} {int((br[f'cur_{t}'] > 0).sum()):,}" for t in TYPES),
             f"  lot split possible (2+ lots): {int((br['split_lots'] >= 2).sum()):,}",
             f"  subdivision 2+ lots: standard {int((br['sub_lots'] >= 2).sum()):,}, conservation "
             f"{int((br['sub_conservation_lots'] >= 2).sum()):,}, compact {int((br['sub_compact_lots'] >= 2).sum()):,}, "
             f"mixed-income {int((br['sub_mixed_income_lots'] >= 2).sum()):,}",
             f"  townhome by right: {int(((br['townhome_units'] > 0) & ~br['townhome_needs_rezoning']).sum()):,} parcels; "
             f"by rezoning (speculative): {int(((br['townhome_units'] > 0) & br['townhome_needs_rezoning']).sum()):,}",
             "  flags: " + ", ".join(f"{c} {int(df[c].sum()):,}" for c in
                                     ["no_frontage", "setback_varies", "reduced_depth_possible",
                                      "multi_dwelling_possible", "arterial_frontage", "verify_acreage"])]
    return "\n".join(lines)


def main() -> None:
    cfg = yaml.safe_load((ROOT / "config" / "scoring.yaml").read_text(encoding="utf-8"))
    rules = load_rules()
    engine = get_engine()
    with engine.connect() as conn:
        as_of = conn.execute(text("SELECT max(valid_from) FROM parcel_features")).scalar_one()
    inputs = load_inputs(engine, cfg)
    df = build_frame(inputs, rules, cfg)
    changes = store(df, engine, as_of)
    report = summary(df)
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO ingest_runs (source, snapshot_date, source_file, snapshot_written, report) "
                 "VALUES ('parcel_yield', :d, :f, :w, CAST(:r AS jsonb))"),
            {"d": as_of, "f": "parcel_features, parcels, streets; config/zoning_rules.yaml, config/scoring.yaml",
             "w": bool(changes["added"] or changes["changed"] or changes["removed"]),
             "r": json.dumps({"summary": report, "changes": changes}, default=str)})
    print(report)
    print(f"  parcel_yield: {changes}")


if __name__ == "__main__":
    main()
