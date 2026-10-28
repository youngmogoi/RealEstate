"""Write `changes` between the two latest county parcel snapshots.

Run with `make changes` (after the ingests). The value threshold lives under `changes` in
config/features.yaml.

A snapshot is a county parcel load that changed something (ingest_runs, source
'county_parcels', snapshot_written). The state of a table on date D is rebuilt from its
current and history tables (ingest/versioning.py), so no full copies are kept.

Change types (each row: pin, change_type, old_value, new_value, prev_date, cur_date, source):
- new_pin / retired_pin: in one snapshot but not the other (splits, new subdivisions)
- owner_change: parcels.owner_name differs (single-account parcels)
- new_sale: a sale row first seen in the newer snapshot (parcel_sales)
- value_change: parcels.total_value moved by more than value_change_pct
- zoning_change: parcel_zoning.zone_code differs between the two latest zoning loads

Rows are keyed by (pin, change_type, prev_date, cur_date) and never rewritten, so
rerunning adds nothing.
"""

import json
from datetime import date
from pathlib import Path

import yaml
from sqlalchemy import Engine, text

from ingest.db import get_engine

ROOT = Path(__file__).resolve().parent.parent


def as_of(table: str, cols: str, schema: str = "public") -> str:
    """SQL for `table` as it stood on :d (bind :d)."""
    t = f"{schema}.{table}"
    return (f"(SELECT {cols} FROM {t} WHERE valid_from <= CAST(:d AS date) "
            f"UNION ALL SELECT {cols} FROM {t}_history WHERE valid_from <= CAST(:d AS date) AND valid_to > CAST(:d AS date))")


def snapshot_dates(engine: Engine, source: str, schema: str = "public") -> list[date]:
    with engine.connect() as conn:
        return [r[0] for r in conn.execute(text(
            f"SELECT DISTINCT snapshot_date FROM {schema}.ingest_runs WHERE source = :src AND snapshot_written "
            f"ORDER BY 1"), {"src": source}).all()]


def _snap(table: str, cols: str, d: date, schema: str) -> str:
    return as_of(table, cols, schema).replace(":d", f"'{d.isoformat()}'")


def detect(engine: Engine, prev: date, cur: date, value_pct: float, zoning: tuple[date, date] | None = None,
           schema: str = "public") -> dict:
    """Insert changes between prev and cur; returns counts of rows added per change type."""
    s = schema
    p0 = _snap("parcels", "pin, owner_name, total_value", prev, s)
    p1 = _snap("parcels", "pin, owner_name, total_value", cur, s)
    queries = {
        "new_pin": f"SELECT b.pin, NULL, b.pin FROM {p1} b LEFT JOIN {p0} a USING (pin) WHERE a.pin IS NULL",
        "retired_pin": f"SELECT a.pin, a.pin, NULL FROM {p0} a LEFT JOIN {p1} b USING (pin) WHERE b.pin IS NULL",
        "owner_change": f"SELECT a.pin, a.owner_name, b.owner_name FROM {p0} a JOIN {p1} b USING (pin) "
                        f"WHERE a.owner_name IS NOT NULL AND b.owner_name IS NOT NULL AND a.owner_name <> b.owner_name",
        "value_change": f"SELECT a.pin, a.total_value::text, b.total_value::text FROM {p0} a JOIN {p1} b USING (pin) "
                        f"WHERE a.total_value > 0 AND abs(b.total_value - a.total_value) / a.total_value * 100 > {float(value_pct)}",
        "new_sale": f"SELECT s.pin, NULL, concat_ws(' ', s.sale_date::date, s.sale_price, s.deed_ref) "
                    f"FROM {_snap('parcel_sales', 'pin, transfer_id, property_id, sale_date, sale_price, deed_ref', cur, s)} s "
                    f"LEFT JOIN {_snap('parcel_sales', 'pin, transfer_id, property_id', prev, s)} o "
                    f"USING (pin, transfer_id, property_id) WHERE o.pin IS NULL",
    }
    sources = {"new_pin": "parcels", "retired_pin": "parcels", "owner_change": "parcels", "value_change": "parcels",
               "new_sale": "parcel_sales"}
    dates = {k: (prev, cur) for k in queries}
    if zoning:
        z0, z1 = (_snap("parcel_zoning", "pin, zone_code", d, s) for d in zoning)
        queries["zoning_change"] = (f"SELECT a.pin, a.zone_code, b.zone_code FROM {z0} a JOIN {z1} b USING (pin) "
                                    f"WHERE a.zone_code IS DISTINCT FROM b.zone_code")
        sources["zoning_change"] = "parcel_zoning"
        dates["zoning_change"] = zoning

    added = {}
    with engine.begin() as conn:
        conn.execute(text(
            f"CREATE TABLE IF NOT EXISTS {s}.changes (pin text NOT NULL, change_type text NOT NULL, old_value text, "
            f"new_value text, prev_date date NOT NULL, cur_date date NOT NULL, source text, "
            f"detected_at timestamptz DEFAULT now(), PRIMARY KEY (pin, change_type, prev_date, cur_date))"))
        for kind, q in queries.items():
            d0, d1 = dates[kind]
            added[kind] = conn.execute(text(
                f"INSERT INTO {s}.changes (pin, change_type, old_value, new_value, prev_date, cur_date, source) "
                f"SELECT q.pin, CAST(:t AS text), q.old_value::text, q.new_value::text, CAST(:d0 AS date), "
                f"CAST(:d1 AS date), CAST(:src AS text) "
                f"FROM ({q}) q(pin, old_value, new_value) "
                f"ON CONFLICT (pin, change_type, prev_date, cur_date) DO NOTHING"),
                {"t": kind, "d0": d0, "d1": d1, "src": sources[kind]}).rowcount
    return added


def counts(engine: Engine, pairs: dict[str, tuple[date, date]], schema: str = "public") -> dict:
    """Total change rows per type for the snapshot pair each type was detected on."""
    with engine.connect() as conn:
        return {kind: conn.execute(text(
            f"SELECT count(*) FROM {schema}.changes WHERE change_type = :t AND prev_date = :p AND cur_date = :c"),
            {"t": kind, "p": p, "c": c}).scalar_one() for kind, (p, c) in pairs.items()}


def main() -> None:
    cfg = yaml.safe_load((ROOT / "config" / "features.yaml").read_text())["changes"]
    engine = get_engine()
    parcel_dates = snapshot_dates(engine, "county_parcels")
    zoning_dates = snapshot_dates(engine, "zoning")
    if len(parcel_dates) < 2:
        msg = (f"Only {len(parcel_dates)} county parcel snapshot(s) so far ({', '.join(map(str, parcel_dates))}); "
               "changes start with the next county download.")
        print(msg)
        result = {"snapshots": [str(d) for d in parcel_dates], "added": {}, "note": msg}
    else:
        prev, cur = parcel_dates[-2], parcel_dates[-1]
        zoning = (zoning_dates[-2], zoning_dates[-1]) if len(zoning_dates) >= 2 else None
        added = detect(engine, prev, cur, cfg["value_change_pct"], zoning)
        pairs = {k: (zoning if k == "zoning_change" else (prev, cur)) for k in added}
        totals = counts(engine, pairs)
        print(f"Changes {prev} -> {cur}: " + ", ".join(f"{k} {totals[k]:,} (+{v:,} new this run)" for k, v in added.items()))
        result = {"prev": str(prev), "cur": str(cur), "added": added, "totals": totals}
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO ingest_runs (source, snapshot_date, source_file, snapshot_written, report) "
                          "VALUES ('changes', CURRENT_DATE, 'parcels, parcel_sales, parcel_zoning', :w, CAST(:r AS jsonb))"),
                     {"w": bool(result["added"] and any(result["added"].values())), "r": json.dumps(result)})


if __name__ == "__main__":
    main()
