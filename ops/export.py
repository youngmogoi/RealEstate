"""Excel review workbook of the ranked lists: Build, negotiate, check_land and likely_hoa from `scores`.

Run with `make export` (after `make route`), or `python -m ops.export --actions build skip`.
Writes exports/scores_<date>.xlsx (gitignored: it holds owner names): one sheet per action, ranked
(negotiate by profit at the target offer, the others by score). Every sheet starts with your review
columns, my_verdict (ok / no, a dropdown) and my_note, filled in where config/land_checks.yaml already
has your verdict; then the PIN as text (Excel would drop a leading 0 from a number), a link to the
county's Polaris page and a satellite view. Type your verdicts and run `make import-checks`.

Every number comes from `scores` / `parcel_rlv` / `parcel_features`; `python -m scoring.rlv --explain
<pin>` shows the inputs behind one parcel.
"""

import argparse
from datetime import date, datetime
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from sqlalchemy import text

from ingest.db import get_engine
from scoring.route import load_verdicts

ROOT = Path(__file__).resolve().parent.parent
POLARIS = "https://polaris3g.mecklenburgcountync.gov/pid/{pin}"
SATELLITE = "https://www.google.com/maps/@?api=1&map_action=map&center={lat:.6f},{lon:.6f}&zoom=19&basemap=satellite"
SHEETS = {"build": "Build", "negotiate": "Negotiate", "check_land": "Check land", "likely_hoa": "Likely HOA"}
SORT = {"negotiate": "profit_pct_at_offer"}           # everything else by score

SQL = """
SELECT s.action, s.pin, s.site_address, s.municipality, r.zone_code, round(s.acres::numeric, 2) AS acres,
       round(f.frontage_ft::numeric) AS frontage_ft, s.option, s.units,
       round(s.price_per_sf::numeric) AS comps_per_sf, round(s.arv::numeric) AS arv, round(s.price::numeric) AS price,
       r.price_basis, round(s.land_pct_of_arv::numeric, 1) AS land_pct_of_arv,
       round(s.build_cost::numeric) AS build_cost, round(s.cash_needed::numeric) AS cash_needed,
       round(s.target_offer::numeric) AS target_offer, round(s.profit_pct_at_price::numeric, 1) AS profit_pct_at_price,
       round(s.profit_pct_at_offer::numeric, 1) AS profit_pct_at_offer,
       round(s.rlv::numeric) AS rlv, round(s.score::numeric) AS score,
       round(s.land_value_ratio_pct::numeric) AS land_vs_neighbors_pct, s.held_action,
       s.creek_on_lot, round(s.stream_length_ft::numeric) AS creek_ft, s.power_line_near,
       round(s.power_line_ft::numeric) AS power_line_ft,
       s.likely_hoa, s.hoa_basis, s.hoa_name, s.hoa_pin, s.plat_year, s.pre_1980_subdivision,
       s.verify_sewer, r.sewer_basis, s.rail_on_parcel, s.creek_divides, s.rear_lot_front_pin,
       s.best_option, round(s.best_price_per_sf::numeric) AS best_comps_per_sf,
       r.owner_name, r.owner_type, f.land_use, s.reason, s.valid_from AS scored_on,
       ST_Y(ST_Transform(ST_PointOnSurface(p.geom), 4326)) AS lat, ST_X(ST_Transform(ST_PointOnSurface(p.geom), 4326)) AS lon
FROM {s}.scores s
LEFT JOIN {s}.parcel_rlv r USING (pin)
LEFT JOIN {s}.parcel_features f USING (pin)
LEFT JOIN {s}.parcels p USING (pin)
WHERE s.action = ANY(:actions)
"""

WHOLE = ["frontage_ft", "comps_per_sf", "arv", "price", "build_cost", "cash_needed", "rlv", "score",
         "land_vs_neighbors_pct", "best_comps_per_sf", "units", "creek_ft", "power_line_ft", "target_offer", "plat_year"]
MONEY = ["arv", "price", "build_cost", "cash_needed", "target_offer", "rlv", "score"]
WIDTH = {"my_verdict": 11, "my_note": 32, "pin": 10, "site_address": 34, "polaris": 9, "satellite": 10,
         "reason": 80, "owner_name": 30, "hoa_name": 30}


def review_frame(df: pd.DataFrame, verdicts: dict[str, dict]) -> pd.DataFrame:
    """Your review columns first (prefilled from land_checks.yaml), then pin, address, links, the rest."""
    df = df.copy()
    df[WHOLE] = df[WHOLE].astype("Float64").round().astype("Int64")      # whole dollars / feet
    df.insert(0, "my_verdict", [verdicts.get(p, {}).get("verdict") for p in df["pin"]])
    df.insert(1, "my_note", [verdicts.get(p, {}).get("note") for p in df["pin"]])
    df.insert(2, "rank", df.groupby("action").cumcount() + 1)
    links = pd.DataFrame({"polaris": [POLARIS.format(pin=p) for p in df["pin"]],
                          "satellite": [None if pd.isna(a) or pd.isna(o) else SATELLITE.format(lat=a, lon=o)
                                        for a, o in zip(df["lat"], df["lon"])]}, index=df.index)
    first = ["my_verdict", "my_note", "rank", "pin", "site_address"]
    rest = [c for c in df.columns if c not in first + ["lat", "lon"]]
    return pd.concat([df[first], links, df[rest]], axis=1)


def ranked(df: pd.DataFrame, actions: list[str]) -> dict[str, pd.DataFrame]:
    """{action: its rows, ranked} in the order asked."""
    out = {}
    for a in actions:
        d = df[df["action"] == a]
        out[a] = d.sort_values([SORT.get(a, "score"), "pin"], ascending=[False, True], na_position="last")
    return out


def write_sheet(ws, df: pd.DataFrame) -> None:
    ws.append(list(df.columns))
    for c in ws[1]:
        c.font = Font(bold=True)
    cols = {name: i + 1 for i, name in enumerate(df.columns)}
    link = Font(color="0563C1", underline="single")
    for row in df.itertuples(index=False):
        ws.append([None if (not isinstance(v, str) and pd.isna(v)) else (v.item() if hasattr(v, "item") else v)
                   for v in row])
        r = ws.max_row
        ws.cell(r, cols["pin"]).number_format = "@"           # text: keep the leading 0
        for name, label in (("polaris", "Polaris"), ("satellite", "Satellite")):
            c = ws.cell(r, cols[name])
            if c.value:
                c.hyperlink, c.value, c.font = c.value, label, link
        for name in MONEY:
            ws.cell(r, cols[name]).number_format = "$#,##0"
    yellow = PatternFill("solid", fgColor="FFF2CC")
    for r in range(1, ws.max_row + 1):
        for name in ("my_verdict", "my_note"):
            ws.cell(r, cols[name]).fill = yellow
    dv = DataValidation(type="list", formula1='"ok,no"', allow_blank=True)
    ws.add_data_validation(dv)
    dv.add(f"A2:A{max(ws.max_row, 2)}")
    for name, i in cols.items():
        ws.column_dimensions[get_column_letter(i)].width = WIDTH.get(name, max(10, min(len(name) + 2, 22)))
    ws.freeze_panes = "F2"                                    # review columns, rank, pin, address stay in view
    ws.auto_filter.ref = ws.dimensions


def export(engine, actions: list[str], out_dir: Path, today: date, verdicts: dict[str, dict],
           s: str = "public") -> tuple[Path, pd.DataFrame]:
    with engine.connect() as conn:
        df = pd.read_sql(text(SQL.format(s=s)), conn, params={"actions": actions})
    sheets = {a: review_frame(d, verdicts) for a, d in ranked(df, actions).items()}
    wb = Workbook()
    wb.remove(wb.active)
    for a, d in sheets.items():
        write_sheet(wb.create_sheet(SHEETS.get(a, a)[:31]), d)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"scores_{today:%Y-%m-%d}.xlsx"
    try:
        wb.save(path)
    except PermissionError:                                   # open in Excel, which locks it
        path = out_dir / f"scores_{today:%Y-%m-%d}_{datetime.now():%H%M}.xlsx"
        wb.save(path)
    return path, pd.concat(sheets.values()) if sheets else df


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--actions", nargs="+", default=list(SHEETS))
    args = ap.parse_args()
    path, df = export(get_engine(), args.actions, ROOT / "exports", date.today(), load_verdicts())
    counts = ", ".join(f"{a} {n:,}" for a, n in df["action"].value_counts().items())
    print(f"Wrote {len(df):,} rows ({counts}) -> {path.relative_to(ROOT).as_posix()}")


if __name__ == "__main__":
    main()
