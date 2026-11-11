"""Likely HOA parcels: land on a recorded plat, or next to it, that an HOA owns or keeps in common.

Run with `make hoa` (after `make features` and `make utilities`). Settings live under `hoa` in
config/features.yaml. Decided 2026-11-10:

- HOA land: owner type hoa (features.yaml owner_type_rules) or a common-area land use.
- same_plat: every parcel on the same recorded plat (legal description M<book>-<page>,
  parcel_utilities.plat_ref) as HOA land is likely_hoa.
- touches_same_subdivision: a parcel touching HOA land (within touch_ft) that has the same NC OneMap
  subdivision name (the assessor's neighborhood; too broad to group by on its own).
- hoa_land: the HOA land itself.
Evidence: the HOA's owner name and PIN (an HOA-owned parcel before a common-area one, then the
lowest PIN). plat_year (first home on the plat, parcel_utilities) and pre_1980_subdivision are
stored for every parcel.

-> parcel_hoa (versioned): pin, likely_hoa, hoa_basis, hoa_name, hoa_pin, hoa_land, plat_ref,
plat_year, pre_1980_subdivision, subdivision.
"""

import fnmatch
import json

import pandas as pd
import yaml
from sqlalchemy import Engine, text

from features.build import ROOT, as_of_date
from ingest.db import copy_dataframe, get_engine
from ingest.versioning import apply_versioned, row_hash

INPUT_SQL = """
SELECT p.pin, u.plat_ref, u.plat_year, f.owner_type, f.land_use, coalesce(p.owner_name,
       (SELECT min(o.owner_name) FROM {s}.parcel_owners o WHERE o.pin = p.pin)) AS owner_name,
       s.subdivision
FROM {s}.parcels p
LEFT JOIN {s}.parcel_utilities u USING (pin)
LEFT JOIN {s}.parcel_features f USING (pin)
LEFT JOIN (SELECT DISTINCT ON (pin) pin, subdivision FROM {s}.nconemap_parcels_snapshots
           WHERE subdivision IS NOT NULL ORDER BY pin, snapshot_date DESC) s USING (pin)
"""

TOUCH_SQL = """
SELECT h.pin AS hoa_pin, n.pin
FROM {s}.parcels h JOIN {s}.parcels n ON n.pin <> h.pin AND ST_DWithin(n.geom, h.geom, :ft)
WHERE h.pin = ANY(:pins)
"""

COLS = ["pin", "likely_hoa", "hoa_basis", "hoa_name", "hoa_pin", "hoa_land", "plat_ref", "plat_year",
        "pre_1980_subdivision", "subdivision"]


def _matches(value: str | None, patterns: list[str]) -> bool:
    return bool(value) and any(fnmatch.fnmatchcase(value.upper(), p) for p in patterns)


def flag_hoa(parcels: pd.DataFrame, touches: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """parcel_hoa rows. `parcels`: pin, plat_ref, plat_year, owner_type, land_use, owner_name,
    subdivision; `touches`: hoa_pin, pin (parcels touching HOA land)."""
    df = parcels.copy()
    df["subdivision"] = [None if _matches(x, cfg["ignore_subdivision_names"]) else x for x in df["subdivision"]]
    common = pd.Series([_matches(x, cfg["common_land_uses"]) for x in df["land_use"]], index=df.index, dtype=bool)
    df["hoa_land"] = (df["owner_type"] == "hoa") | common
    land = df[df["hoa_land"]].assign(not_hoa_owner=lambda d: d["owner_type"] != "hoa") \
        .sort_values(["not_hoa_owner", "pin"])
    by_plat = land.dropna(subset=["plat_ref"]).drop_duplicates("plat_ref").set_index("plat_ref")
    evidence = {r.pin: (r.owner_name, r.pin) for r in land.itertuples()}

    out = df[["pin", "plat_ref", "plat_year", "subdivision", "hoa_land"]].copy()
    # touching HOA land in the same named subdivision: {pin: the HOA land's pin}
    sub = dict(zip(df["pin"], df["subdivision"]))
    same = [sub.get(a) is not None and sub.get(a) == sub.get(b) for a, b in zip(touches["hoa_pin"], touches["pin"])]
    t = touches[pd.Series(same, index=touches.index, dtype=bool)]
    touch = t.sort_values(["pin", "hoa_pin"]).drop_duplicates("pin").set_index("pin")["hoa_pin"].to_dict()
    plat_pin = by_plat["pin"].to_dict()
    # first basis that applies: the HOA land itself, the same plat, touching in the same subdivision
    basis, hpin = [], []
    for pin, land_, plat in zip(out["pin"], out["hoa_land"], out["plat_ref"]):
        if land_:
            basis.append("hoa_land"); hpin.append(pin)
        elif plat is not None and plat in plat_pin:
            basis.append("same_plat"); hpin.append(plat_pin[plat])
        elif pin in touch:
            basis.append("touches_same_subdivision"); hpin.append(touch[pin])
        else:
            basis.append(None); hpin.append(None)
    out["hoa_basis"], out["hoa_pin"] = basis, hpin
    out["hoa_name"] = [evidence[h][0] if h is not None else None for h in hpin]
    out["likely_hoa"] = out["hoa_basis"].notna()
    out["pre_1980_subdivision"] = out["plat_year"].notna() & (out["plat_year"] < cfg["pre_subdivision_year"])
    out["plat_year"] = out["plat_year"].astype("Int64")
    return out[COLS]


def build(engine: Engine, cfg: dict, s: str = "public") -> tuple[pd.DataFrame, dict]:
    as_of = as_of_date(engine, s)
    with engine.connect() as conn:
        parcels = pd.read_sql(text(INPUT_SQL.format(s=s)), conn)
        parcels = parcels.astype(object).where(parcels.notna(), None)
        land = [p for p, o, u in zip(parcels["pin"], parcels["owner_type"], parcels["land_use"])
                if o == "hoa" or _matches(u, cfg["common_land_uses"])]
        touches = pd.read_sql(text(TOUCH_SQL.format(s=s)), conn, params={"pins": land, "ft": cfg["touch_ft"]})
    df = flag_hoa(parcels, touches, cfg)
    with engine.begin() as conn:
        copy_dataframe(conn, df, f"{s}.parcel_hoa_staging", prepare=lambda c: c.assign(row_hash=row_hash(c)))
        changes = apply_versioned(conn, f"{s}.parcel_hoa", ["pin"], as_of)
    return df, changes


def main() -> None:
    cfg = yaml.safe_load((ROOT / "config" / "features.yaml").read_text(encoding="utf-8"))["hoa"]
    engine = get_engine()
    df, changes = build(engine, cfg)
    basis = df["hoa_basis"].value_counts()
    report = {"parcels": len(df), "likely_hoa": int(df["likely_hoa"].sum()),
              "by_basis": {k: int(v) for k, v in basis.items()},
              "pre_1980_subdivision": int(df["pre_1980_subdivision"].sum()), "changes": changes}
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO ingest_runs (source, snapshot_date, source_file, snapshot_written, report) "
                          "VALUES ('hoa', :d, 'parcel_hoa', :w, CAST(:r AS jsonb))"),
                     {"d": as_of_date(engine), "w": bool(changes["added"] or changes["changed"] or changes["removed"]),
                      "r": json.dumps(report)})
    print(f"HOA: {report['likely_hoa']:,} of {report['parcels']:,} parcels likely HOA "
          f"({', '.join(f'{k} {v:,}' for k, v in report['by_basis'].items())}); "
          f"pre-1980 subdivisions {report['pre_1980_subdivision']:,}; {changes}")


if __name__ == "__main__":
    main()
