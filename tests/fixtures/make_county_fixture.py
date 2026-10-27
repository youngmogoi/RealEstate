"""Rebuild tests/fixtures/meck_county/ from the Mecklenburg County downloads.

Run: uv run python tests/fixtures/make_county_fixture.py

Source: the three county files named in config/ingest.yaml, downloaded 2026-10-26. Rows are
copied unchanged for the PINs below, each chosen to cover one case the ingest handles.
"""

from pathlib import Path

import pandas as pd
import pyogrio
import yaml

from ingest.county_parcels import BOUNDARY_FIELDS, ROOT

OUT = Path(__file__).with_name("meck_county")

PINS = {
    "03141608": "2137 Tom Sadler Rd: sold 2026-09-04, house added since NC OneMap",
    "08920128": "443 Dawn Cir: sold 2026-06-25, ACRE land unit",
    "04104279": "3200 Auten Ave: LOT land unit, missing from NC OneMap",
    "04104212": "SQUARE FEET land unit",
    "20909C99": "condo building: one shape, 61 unit accounts",
    "05126204": "last sale date 2026-11-17, after the download",
    "12303C98": "last sale date 9997-02-05",
    "00102326": "last sale date 1975-01-01 placeholder",
    "00101A01": "shape with no owner record",
}

if __name__ == "__main__":
    cfg = yaml.safe_load((ROOT / "config" / "ingest.yaml").read_text())["county"]
    OUT.mkdir(exist_ok=True)
    where = "pid IN ({})".format(",".join(f"'{p}'" for p in PINS))
    shapes = pyogrio.read_dataframe(ROOT / cfg["boundaries"], columns=list(BOUNDARY_FIELDS), where=where)
    pyogrio.write_dataframe(shapes, OUT / "boundaries.gpkg", layer="Parcels_NoData")

    read = lambda p: pd.read_csv(ROOT / p, dtype=str, encoding="utf-8-sig", keep_default_na=False)
    owners = read(cfg["owners"])
    owners[owners["pid"].isin(PINS)].to_csv(OUT / "owners.csv", index=False)
    sales = read(cfg["sales"])
    sales[sales["parcelid"].isin(PINS)].to_csv(OUT / "sales.csv", index=False)
    print(f"Wrote {len(shapes)} shapes, {owners['pid'].isin(PINS).sum()} accounts, "
          f"{sales['parcelid'].isin(PINS).sum()} sales to {OUT}")
