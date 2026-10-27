"""Rebuild tests/fixtures/meck_zoning/ from the county and Charlotte zoning downloads.

Run: uv run python tests/fixtures/make_zoning_fixture.py

Source: the files named under `zoning` and `county.boundaries` in config/ingest.yaml,
downloaded 2026-10-26/27. Parcel shapes and district shapes are copied unchanged: every
district shape that touches one of the parcels below is kept, so overlap precedence
inside these parcels is the same as in the full data.
"""

from pathlib import Path

import pandas as pd
import pyogrio
import shapely
import yaml

from ingest.county_parcels import ROOT

OUT = Path(__file__).with_name("meck_zoning")

PINS = {
    "00101102": "Huntersville GR, whole parcel",
    "00119106": "Huntersville SP(CD): conditional district",
    "01106231": "split between Huntersville TR and Cornelius RP (about 50/50)",
    "10938108": "split between Mint Hill R and Charlotte R-12MF(CD)",
    "13709398": "inside both Mint Hill R and Charlotte N2-B: the town wins",
    "16914126": "inside both Charlotte ML-2 and CAC-2, same rezone date: smaller shape wins",
    "07101101": "Charlotte N2-B(HDO) with historic district overlay",
    "02907118": "split within Charlotte: N1-A 67% / I-1(CD) 33%",
    "03309301": "Charlotte N1-A here; the county's Oct 15 list says N1-D(CD)",
    "08920128": "443 Dawn Cir, Charlotte N1-B",
    "19511201": "Stallings R-20 (town mostly in Union County)",
    "02301127": "submerged land with no zoning",
}

if __name__ == "__main__":
    cfg = yaml.safe_load((ROOT / "config" / "ingest.yaml").read_text())
    z = cfg["zoning"]
    OUT.mkdir(exist_ok=True)

    where = "pid IN ({})".format(",".join(f"'{p}'" for p in PINS))
    parcels = pyogrio.read_dataframe(ROOT / cfg["county"]["boundaries"], columns=["pid"], where=where)
    assert len(parcels) == len(PINS), set(PINS) - set(parcels["pid"])
    pyogrio.write_dataframe(parcels, OUT / "parcels.gpkg", layer="parcels")
    area = shapely.union_all(parcels.geometry.values)

    clt = pyogrio.read_dataframe(ROOT / z["charlotte"])
    clt = clt[shapely.intersects(shapely.make_valid(clt.geometry.values), area)]
    pyogrio.write_dataframe(clt, OUT / "charlotte.gpkg", layer="Zoning")

    towns = pyogrio.read_dataframe(ROOT / z["towns"], layer=z["towns_layer"])
    towns = towns[shapely.intersects(towns.geometry.values, area)]
    pyogrio.write_dataframe(towns, OUT / "towns.gpkg", layer=z["towns_layer"])

    check = pd.read_csv(ROOT / z["county_check"], dtype=str, encoding="utf-8-sig", keep_default_na=False)
    check[check["pid"].isin(PINS)].to_csv(OUT / "county_check.csv", index=False)
    print(f"Wrote {len(parcels)} parcels, {len(clt)} Charlotte and {len(towns)} town district shapes to {OUT}")
