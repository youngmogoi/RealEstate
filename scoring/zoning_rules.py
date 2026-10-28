"""Zoning rules from config/zoning_rules.yaml: base-district parsing and the coverage check.

Run `make zoning-rules` (python -m scoring.zoning_rules) to list every district used by
parcels that has no confirmed rule. It exits non-zero until the rules cover them all.
"""

import re
import sys
from functools import lru_cache
from pathlib import Path

import yaml
from sqlalchemy import text

ROOT = Path(__file__).resolve().parent.parent
RULES_PATH = ROOT / "config" / "zoning_rules.yaml"

HOUSING = {"by_right", "site_plan", "none"}
BY_RIGHT_FIELDS = ["min_lot_sf", "min_lot_width_ft", "setbacks_ft", "density"]
HOUSING_TYPES = ["sfh", "duplex", "triplex", "quadraplex", "townhome", "adu"]
ALLOWED = {"yes", "conditional", "no"}
NEEDS_REVIEW = "NEEDS_REVIEW"


def _review_paths(v, path: str = "") -> list[str]:
    """Paths of values marked NEEDS_REVIEW anywhere inside a rule."""
    if isinstance(v, dict):
        return [p for k, x in v.items() for p in _review_paths(x, f"{path}.{k}" if path else str(k))]
    if isinstance(v, list):
        return [p for i, x in enumerate(v) for p in _review_paths(x, f"{path}[{i}]")]
    return [path] if isinstance(v, str) and NEEDS_REVIEW in v else []


def load_rules(path: Path = RULES_PATH) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=None)
def _suffix_re(tokens: tuple[str, ...]) -> re.Pattern:
    alt = "|".join(re.escape(t) for t in sorted(tokens, key=len, reverse=True))
    # "(TOKEN)" or "(TOKEN" (truncated), or TOKEN after a space or a closing parenthesis
    return re.compile(rf"(?:\((?P<a>{alt})\)?|(?<=[\s)])(?P<b>{alt}))\s*$")


def parse_code(jurisdiction: str, code: str, parsing: dict) -> tuple[str, list[tuple[str, str]]]:
    """Return (base district, [(suffix, kind), ...]) for a zoning code, outermost suffix last."""
    s = " ".join(code.upper().split())
    s = (parsing.get("aliases", {}).get(jurisdiction) or {}).get(s, s)
    suffixes = parsing["suffixes"]
    pattern = _suffix_re(tuple(suffixes))
    mods = []
    while (m := pattern.search(s)) and m.start() > 0:
        tok = m["a"] or m["b"]
        mods.append((tok, suffixes[tok]))
        s = s[:m.start()].rstrip()
    return s, mods[::-1]


def _allowed(v) -> str:
    # YAML reads bare yes/no as booleans
    return {True: "yes", False: "no"}.get(v, v) if isinstance(v, bool) else v


def _type_problems(types: dict) -> list[str]:
    out = []
    for t in HOUSING_TYPES:
        e = types.get(t)
        if not isinstance(e, dict):
            out.append(f"types.{t} missing")
            continue
        allowed = _allowed(e.get("allowed"))
        if allowed not in ALLOWED:
            out.append(f"types.{t}.allowed must be one of {sorted(ALLOWED)}")
        elif allowed != "no" and not isinstance(e.get("units_per_building"), int) \
                and not (e.get("requires") or {}).get("max_building_length_ft"):
            # units per building are either fixed or set by a building-length limit
            out.append(f"types.{t} has no units_per_building or max_building_length_ft")
        if not e.get("source"):
            out.append(f"types.{t} has no source")
    return out


def check_rule(rule: dict | None) -> list[str]:
    """Problems with one district's rule entry (empty list = usable)."""
    if not rule:
        return ["no rule"]
    out = []
    if rule.get("status") != "confirmed":
        out.append(f"status is {rule.get('status')!r}, not confirmed")
    housing = rule.get("housing")
    if housing not in HOUSING:
        return out + [f"housing must be one of {sorted(HOUSING)}"]
    if housing == "by_right":
        for f in BY_RIGHT_FIELDS:
            v = rule.get(f)
            if not isinstance(v, dict):
                out.append(f"missing {f}")
            elif not v.get("source"):
                out.append(f"{f} has no source")
        types = rule.get("types")
        if not isinstance(types, dict):
            out.append("missing types")
        else:
            out += _type_problems(types)
    elif not rule.get("source"):
        out.append("no source")
    review = _review_paths(rule)
    if review:
        out.append("NEEDS_REVIEW: " + ", ".join(review))
    return out


def validate(rules: dict, used: dict[tuple[str, str], int]) -> list[tuple[str, str, int, list[str]]]:
    """Check every (jurisdiction, zone code) used by parcels against the rules.

    `used` maps (jurisdiction, zone_code) to a parcel count. Returns one row per base district
    with problems: (jurisdiction, base, parcels, problems), largest first.
    """
    parsing, districts = rules["code_parsing"], rules.get("districts") or {}
    bases: dict[tuple[str, str], int] = {}
    for (j, code), n in used.items():
        base, _ = parse_code(j, code, parsing)
        bases[(j, base)] = bases.get((j, base), 0) + n
    targets = rules.get("rezoning_targets") or {}
    out = []
    for (j, base), n in bases.items():
        rule = (districts.get(j) or {}).get(base)
        problems = check_rule(rule)
        for t, e in ((rule or {}).get("types") or {}).items():
            if isinstance(e, dict) and (e.get("rezoning") or {}).get("target") == "by_place_type" \
                    and not (targets.get(j) or {}).get(t):
                problems.append(f"types.{t}: no rezoning_targets.{j}.{t}")
        if problems:
            out.append((j, base, n, problems))
    return sorted(out, key=lambda r: (-r[2], r[0], r[1]))


def used_districts(conn) -> tuple[dict[tuple[str, str], int], int]:
    """Zone codes on current parcels (main district, plus the second district of split parcels)."""
    rows = conn.execute(text("""
        SELECT jurisdiction, zone_code, count(*) FROM parcel_zoning WHERE zone_code IS NOT NULL GROUP BY 1, 2
        UNION ALL
        SELECT second_jurisdiction, second_zone_code, count(*) FROM parcel_zoning
        WHERE split AND second_zone_code IS NOT NULL GROUP BY 1, 2""")).all()
    used: dict[tuple[str, str], int] = {}
    for j, code, n in rows:
        used[(j, code)] = used.get((j, code), 0) + n
    unzoned = conn.execute(text("SELECT count(*) FROM parcel_zoning WHERE zone_code IS NULL")).scalar_one()
    return used, unzoned


def main() -> None:
    from ingest.db import get_engine

    rules = load_rules()
    with get_engine().connect() as conn:
        used, unzoned = used_districts(conn)
    problems = validate(rules, used)
    n_bases = len({(j, parse_code(j, c, rules["code_parsing"])[0]) for j, c in used})
    print(f"{len(used)} zone codes on parcels -> {n_bases} base districts; {unzoned} parcels have no district")
    if not problems:
        print("OK: every district used by parcels has a confirmed rule")
        return
    print(f"FAIL: {len(problems)} districts need rules ({sum(p[2] for p in problems):,} parcel-districts)")
    for j, base, n, why in problems:
        print(f"  {j:<13} {base:<10} {n:>8,}  {'; '.join(why)}")
    sys.exit(1)


if __name__ == "__main__":
    main()
