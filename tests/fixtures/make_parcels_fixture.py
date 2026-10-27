"""Rebuild tests/fixtures/meck_parcels_sample.gpkg from the NC OneMap download.

Run: uv run python tests/fixtures/make_parcels_fixture.py

Source: data/raw/mecklenburg-parcels-01-07-2026 (published 2026-01-07). Each PIN was
chosen to cover one case the ingest handles; SRC_FID is the row in the source shapefile.
"""

from pathlib import Path

import pyogrio

from ingest.nconemap_parcels import FIELDS, ROOT

SOURCE = ROOT / "data/raw/mecklenburg-parcels-01-07-2026/nc_mecklenburg_parcels_poly.shp"
OUT = Path(__file__).with_name("meck_parcels_sample.gpkg")

PINS = {
    "00101305": "single-family house",
    "00101108": "SFR acreage tract",
    "00102326": "sale date 1975-01-01 placeholder",
    "00101342": "sale date 1900-01-01 placeholder, no structure",
    "00110975": "multipolygon",
    "00110455": "condominium",
    "00101111": "five buildings, five stacked rows",
}

if __name__ == "__main__":
    where = "PARNO IN ({})".format(",".join(f"'{p}'" for p in PINS))
    gdf = pyogrio.read_dataframe(SOURCE, columns=list(FIELDS), where=where, fid_as_index=True)
    gdf["SRC_FID"] = gdf.index
    pyogrio.write_dataframe(gdf, OUT, layer="parcels")
    print(f"Wrote {len(gdf)} rows to {OUT}")
