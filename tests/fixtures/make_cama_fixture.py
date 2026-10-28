"""Rebuild tests/fixtures/meck_cama/cama.csv from the county CAMA download.

Run: uv run python tests/fixtures/make_cama_fixture.py

Source: the CAMA file named in config/ingest.yaml (county.cama), downloaded 2026-10-28. Rows are
copied unchanged for the accounts below, each chosen to cover one case the ingest handles.
"""

from pathlib import Path

import pandas as pd
import yaml

from ingest.county_parcels import ROOT

OUT = Path(__file__).with_name("meck_cama")

ACCOUNTS = {
    "12708101": "two houses (1932, 2017); heated 3,089 = 2,171 + 918 on both rows",
    "00102115": "two 1945 buildings that differ only in finished area (938 / 768)",
    "05101114": "apartment buildings, some rows repeated exactly",
    "12704403": "416 Clarice Av: one duplex, 2,080 heated sf",
    "02968182": "new 2026 townhome account, tax year 2027, no building yet",
    "19923706B": "account id with a letter suffix on shape 19923706, no heated area",
}

if __name__ == "__main__":
    cfg = yaml.safe_load((ROOT / "config" / "ingest.yaml").read_text())["county"]
    OUT.mkdir(exist_ok=True)
    rows = pd.concat(chunk[chunk["parcelid"].isin(ACCOUNTS)] for chunk in pd.read_csv(
        ROOT / cfg["cama"], dtype=str, encoding="utf-8-sig", keep_default_na=False, chunksize=50_000))
    rows.to_csv(OUT / "cama.csv", index=False)
    print(f"Wrote {len(rows)} rows for {rows['parcelid'].nunique()} accounts to {OUT}")
