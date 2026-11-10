"""CSV export of the ranked list: Build, check_land and likely_hoa parcels (or any actions) from `scores`.

Run with `make export` (after `make route`), or `python -m ops.export --actions build check_land skip`.
Writes exports/scores_<date>.csv (gitignored: it holds owner names), opens in Excel as is.
Rows are ranked by score within each action. Every number comes from `scores` / `parcel_rlv` /
`parcel_features`; `python -m scoring.rlv --explain <pin>` shows the inputs behind one parcel.
"""

import argparse
from datetime import date, datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import text

from ingest.db import get_engine

ROOT = Path(__file__).resolve().parent.parent

SQL = """
SELECT s.action, s.pin, s.site_address, s.municipality, r.zone_code, round(s.acres::numeric, 2) AS acres,
       round(f.frontage_ft::numeric) AS frontage_ft, s.option, s.units,
       round(s.price_per_sf::numeric) AS comps_per_sf, round(s.arv::numeric) AS arv, round(s.price::numeric) AS price,
       r.price_basis, round(s.land_pct_of_arv::numeric, 1) AS land_pct_of_arv,
       round(s.build_cost::numeric) AS build_cost, round(s.cash_needed::numeric) AS cash_needed,
       round(s.rlv::numeric) AS rlv, round(s.score::numeric) AS score,
       round(s.land_value_ratio_pct::numeric) AS land_vs_neighbors_pct, s.held_action,
       s.creek_on_lot, round(s.stream_length_ft::numeric) AS creek_ft, s.power_line_near,
       round(s.power_line_ft::numeric) AS power_line_ft, s.land_check, s.land_check_note,
       s.likely_hoa, s.hoa_basis, s.hoa_name, s.hoa_pin, s.plat_year, s.pre_1980_subdivision,
       s.verify_sewer, r.sewer_basis, s.rail_on_parcel, s.creek_divides, s.rear_lot_front_pin,
       s.best_option, round(s.best_price_per_sf::numeric) AS best_comps_per_sf,
       r.owner_name, r.owner_type, f.land_use, s.reason, s.valid_from AS scored_on
FROM {s}.scores s
LEFT JOIN {s}.parcel_rlv r USING (pin)
LEFT JOIN {s}.parcel_features f USING (pin)
WHERE s.action = ANY(:actions)
ORDER BY array_position(CAST(:actions AS text[]), s.action), s.score DESC NULLS LAST, s.pin
"""


def export(engine, actions: list[str], out_dir: Path, today: date, s: str = "public") -> tuple[Path, pd.DataFrame]:
    with engine.connect() as conn:
        df = pd.read_sql(text(SQL.format(s=s)), conn, params={"actions": actions})
    whole = ["frontage_ft", "comps_per_sf", "arv", "price", "build_cost", "cash_needed", "rlv", "score",
             "land_vs_neighbors_pct", "best_comps_per_sf", "units",
             "creek_ft", "power_line_ft"]
    df[whole] = df[whole].astype("Float64").round().astype("Int64")     # whole dollars / feet in Excel
    df.insert(1, "rank", df.groupby("action").cumcount() + 1)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"scores_{today:%Y-%m-%d}.csv"
    try:
        df.to_csv(path, index=False, encoding="utf-8-sig")     # the BOM lets Excel read it as UTF-8
    except PermissionError:                                    # open in Excel, which locks it
        path = out_dir / f"scores_{today:%Y-%m-%d}_{datetime.now():%H%M}.csv"
        df.to_csv(path, index=False, encoding="utf-8-sig")
    return path, df


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--actions", nargs="+", default=["build", "check_land", "likely_hoa"])
    args = ap.parse_args()
    path, df = export(get_engine(), args.actions, ROOT / "exports", date.today())
    counts = ", ".join(f"{a} {n:,}" for a, n in df["action"].value_counts().items())
    print(f"Wrote {len(df):,} rows ({counts}) -> {path.relative_to(ROOT).as_posix()}")


if __name__ == "__main__":
    main()
