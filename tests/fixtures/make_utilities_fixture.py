"""Rebuild tests/fixtures/meck_utilities/ for tests/test_utilities.py.

Run (database up, after `make ingest` and `make utilities`):
    uv run python tests/fixtures/make_utilities_fixture.py

Sources, copied unchanged for the parcels below and every parcel sharing their plats
(shapes included, since the fallback uses each plat's median lot size):
- parcel shapes: county "Tax Parcel Boundaries" (config county.boundaries)
- legal descriptions: county "Tax Parcel Ownership Data" (config county.owners)
- year built: the NC OneMap 2026-01-07 snapshot in nconemap_parcels_snapshots (its raw
  download was deleted to free disk space)
- capital projects within 1,500 ft: the newest data/raw/cip/charlotte_cip_polygons_*.geojson
"""

import json
from pathlib import Path

import pandas as pd
import pyogrio
import shapely
import yaml
from sqlalchemy import text

from ingest.county_parcels import ROOT
from ingest.db import get_engine

OUT = Path(__file__).with_name("meck_utilities")

PINS = {
    "08920128": "443 Dawn Cir: plat M73-876, first home 2023 -> served (low)",
    "03141608": "2137 Tom Sadler Rd: plat M69-548, no home in the Jan 2026 snapshot; in Charlotte, small lots -> fallback",
    "04104279": "3200 Auten Ave: plat M76-221, no home in the Jan 2026 snapshot; in Charlotte, small lots -> fallback",
    "05511524": "in-town infill plat M25-160: no homes, Charlotte, median lot 0.13 ac -> fallback",
    "23114109": "large-lot rural plat M21-428: no homes, unincorporated, 3.3 ac lots -> unknown",
    "13533311": "plat M25-763 inside Mint Hill but 1.4 ac lots -> unknown",
    "00716240": "plat M44-513 (2006) -> served; 317 ft from the Sam Furr Road sewer extension",
    "00102115": "recent plat M74-885 that includes a 1940 house -> unknown",
    "00319105": "plat M25-695 whose first home was built in 1980 exactly -> unknown (cutoff is after 1980)",
    "00101113": "no plat reference -> unknown",
}

if __name__ == "__main__":
    cfg = yaml.safe_load((ROOT / "config" / "ingest.yaml").read_text())
    OUT.mkdir(exist_ok=True)

    owners = pd.read_csv(ROOT / cfg["county"]["owners"], dtype=str, encoding="utf-8-sig", keep_default_na=False,
                         usecols=["pid", "camapid", "txt_legaldesc", "municipality_desc"])
    owners["ref"] = owners["txt_legaldesc"].str.extract(r"M(\d+-\d+)", expand=False)
    refs = set(owners.loc[owners["pid"].isin(PINS), "ref"].dropna())
    keep = owners[owners["pid"].isin(PINS) | owners["ref"].isin(refs)].drop(columns="ref")
    keep.to_csv(OUT / "owners.csv", index=False)
    pins = sorted(set(keep["pid"]))

    # Every parcel on the same plats too: the plat's median lot size is measured from shapes
    where = "pid IN ({})".format(",".join(f"'{p}'" for p in pins))
    parcels = pyogrio.read_dataframe(ROOT / cfg["county"]["boundaries"], columns=["pid"], where=where)
    assert set(PINS) <= set(parcels["pid"]), set(PINS) - set(parcels["pid"])
    pyogrio.write_dataframe(parcels, OUT / "parcels.gpkg", layer="parcels")

    with get_engine().connect() as conn:
        built = pd.read_sql(text(
            "SELECT pin, year_built, snapshot_date FROM nconemap_parcels_snapshots WHERE pin = ANY(:p) ORDER BY pin"
        ), conn, params={"p": pins})
    built.to_csv(OUT / "year_built.csv", index=False)

    cip_path = sorted((ROOT / cfg["utilities"]["cip_download_dir"]).glob("charlotte_cip_polygons_*.geojson"))[-1]
    cip = pyogrio.read_dataframe(cip_path).to_crs(2264)
    near = shapely.buffer(shapely.union_all(parcels.geometry.values), 1500)
    cip = cip[shapely.intersects(cip.geometry.values, near)].to_crs(4326)
    (OUT / "cip.geojson").write_text(cip.to_json(drop_id=True))
    print(f"Wrote {len(parcels)} parcels, {len(keep)} plat accounts, {len(built)} year-built rows, "
          f"{len(cip)} capital projects (from {cip_path.name}) to {OUT}")
