"""Tests for the review workbook (ops/export.py) and the verdict import (ops/import_checks.py)."""

from datetime import date

import pandas as pd
import yaml

from ops.export import ranked, review_frame, write_sheet
from ops.import_checks import merge, read_verdicts
from openpyxl import Workbook, load_workbook


def _scores():
    cols = ["frontage_ft", "comps_per_sf", "arv", "price", "build_cost", "cash_needed", "rlv", "score",
            "land_vs_neighbors_pct", "best_comps_per_sf", "units", "creek_ft", "power_line_ft", "target_offer",
            "plat_year"]
    df = pd.DataFrame({"action": ["build", "build", "negotiate"], "pin": ["02708315", "21309211", "15701809"],
                       "site_address": ["12607 CARDINAL POINT RD", "OLD BELL RD", "3010 SAM DRENAN RD"],
                       "profit_pct_at_offer": [None, None, 21.5], "reason": ["r1", "r2", "r3"],
                       "lat": [35.05, 35.14, 35.20], "lon": [-80.70, -80.79, -80.80]})
    for c in cols:
        df[c] = 1.0
    df["score"] = [100.0, 900.0, 50.0]
    return df


def test_workbook_keeps_leading_zero_links_and_your_verdicts(tmp_path):
    verdicts = {"21309211": {"verdict": "ok", "note": "seems good (Old Bell Rd)"}}
    sheets = {a: review_frame(d, verdicts) for a, d in ranked(_scores(), ["build", "negotiate"]).items()}
    b = sheets["build"]
    assert list(b.columns[:7]) == ["my_verdict", "my_note", "rank", "pin", "site_address", "polaris", "satellite"]
    assert list(b["pin"]) == ["21309211", "02708315"]                    # by score
    assert b.iloc[0]["my_verdict"] == "ok" and pd.isna(b.iloc[1]["my_verdict"])
    assert b.iloc[1]["polaris"] == "https://polaris3g.mecklenburgcountync.gov/pid/02708315"
    assert "center=35.050000,-80.700000" in b.iloc[1]["satellite"] and "basemap=satellite" in b.iloc[1]["satellite"]
    wb = Workbook(); wb.remove(wb.active)
    write_sheet(wb.create_sheet("Build"), b)
    path = tmp_path / "scores.xlsx"
    wb.save(path)
    ws = load_workbook(path)["Build"]
    assert ws["D3"].value == "02708315" and ws["D3"].number_format == "@"     # the 0 survives
    assert ws["F3"].value == "Polaris" and ws["F3"].hyperlink.target.endswith("/pid/02708315")


def test_import_adds_updates_and_keeps_the_file(tmp_path):
    xlsx = tmp_path / "scores.xlsx"
    with pd.ExcelWriter(xlsx) as w:
        pd.DataFrame({"my_verdict": ["no", "OK", None, "maybe"], "my_note": ["creek in back", None, "x", "y"],
                      "pin": ["21309211", "02708315", "11111111", "22222222"]}).to_excel(w, sheet_name="Build", index=False)
    new = read_verdicts(xlsx)
    assert new == {"21309211": {"verdict": "no", "note": "creek in back", "sheet": "Build"},
                   "02708315": {"verdict": "ok", "note": None, "sheet": "Build"}}
    old_text = ('# my checks\n"21309211": {verdict: "ok", note: "seems good (Old Bell Rd)", checked: 2026-11-09}\n')
    existing = {"21309211": {"verdict": "ok", "note": "seems good (Old Bell Rd)"}}
    text_, counts = merge(old_text, existing, new, date(2026, 11, 10), "scores.xlsx")
    assert counts == {"added": 1, "updated": 1, "unchanged": 0}
    y = yaml.safe_load(text_)
    assert y["21309211"] == {"verdict": "no", "note": "creek in back", "checked": date(2026, 11, 10)}
    assert y["02708315"] == {"verdict": "ok", "note": "", "checked": date(2026, 11, 10)}
    assert text_.startswith("# my checks") and "# Imported 2026-11-10 from scores.xlsx" in text_
