"""Rebuild tests/fixtures/meck_constraints/ for tests/test_constraints.py.

Run (database up, after `make constraints`):
    uv run python tests/fixtures/make_constraints_fixture.py

Sources: parcel shapes from `parcels`, constraint shapes from `constraint_features` (as loaded
from the downloads in config/constraints.yaml), each clipped once to the boxes 50 ft around the
parcels, which doesn't change anything inside them; the slope band raster cut to the same
boxes; sewer_status from `parcel_utilities`. All from the 2026-10-27 run.
"""

from pathlib import Path

import geopandas as gpd
import pandas as pd
import rasterio
import shapely
import yaml
from rasterio.windows import from_bounds
from sqlalchemy import text

from ingest.constraints import ROOT
from ingest.db import get_engine

OUT = Path(__file__).with_name("meck_constraints")

PINS = {
    "10101119": "floodway, encroachment, buffers, floodplains, NWI, stream, steep: all inside the encroachment area; unknown sewer",
    "00110299": "stream buffer and >25% slope that partly overlap (0.473 ac counted once)",
    "10102103": "steep slope only; unknown sewer (septic rated)",
    "08920128": "443 Dawn Cir: small city lot, a sliver of 15-25% slope",
    "03141608": "2137 Tom Sadler Rd: no constraints",
}

if __name__ == "__main__":
    cfg = yaml.safe_load((ROOT / "config" / "constraints.yaml").read_text())
    OUT.mkdir(exist_ok=True)
    engine = get_engine()
    pins = list(PINS)
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT pin, ST_AsBinary(geom) FROM parcels WHERE pin = ANY(:p)"), {"p": pins}).all()
        parcels = gpd.GeoDataFrame({"pin": [r[0] for r in rows]},
                                   geometry=shapely.from_wkb([bytes(r[1]) for r in rows]), crs=2264)
        # each shape once, clipped to the union of the boxes (clipping per box would duplicate shapes
        # near two parcels and double count stream length and septic acres)
        boxes = shapely.union_all([shapely.box(*shapely.buffer(g, 50).bounds) for g in parcels.geometry])
        feats = conn.execute(text(
            "SELECT layer, class, rating, source, source_date, attrs, "
            "ST_AsBinary(ST_Intersection(geom, ST_GeomFromWKB(:b, 2264))) "
            "FROM constraint_features WHERE ST_Intersects(geom, ST_GeomFromWKB(:b, 2264))"),
            {"b": shapely.to_wkb(boxes)}).all()
        utilities = pd.read_sql(text("SELECT pin, sewer_status FROM parcel_utilities WHERE pin = ANY(:p)"), conn,
                                params={"p": pins})
    f = gpd.GeoDataFrame(
        pd.DataFrame([r[:6] for r in feats], columns=["layer", "class", "rating", "source", "source_date", "attrs"]),
        geometry=shapely.from_wkb([bytes(r[6]) for r in feats]), crs=2264)
    f = f[~f.geometry.is_empty].reset_index(drop=True)
    f.to_file(OUT / "features.gpkg", layer="features")
    parcels.to_file(OUT / "parcels.gpkg", layer="parcels")
    utilities.to_csv(OUT / "utilities.csv", index=False)

    raster = ROOT / cfg["derived_dir"] / f"slope_bands_{int(cfg['slope']['resolution_ft'])}ft.tif"
    with rasterio.open(raster) as src:
        for pin, geom in zip(parcels["pin"], parcels.geometry):
            w = from_bounds(*shapely.buffer(geom, 50).bounds, transform=src.transform).round_offsets().round_lengths()
            profile = {**src.profile, "width": w.width, "height": w.height, "transform": src.window_transform(w),
                       "tiled": False, "compress": "deflate"}
            profile.pop("blockxsize", None), profile.pop("blockysize", None)
            with rasterio.open(OUT / f"slope_{pin}.tif", "w", **profile) as dst:
                dst.write(src.read(1, window=w), 1)
    print(f"Wrote {len(parcels)} parcels, {len(f)} constraint shapes, {len(parcels)} slope windows to {OUT}")
