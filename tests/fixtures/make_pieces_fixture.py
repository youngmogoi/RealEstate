"""Rebuild tests/fixtures/meck_pieces/ for tests/test_pieces.py.

Run (database up, after `make constraints` and `make streets`):
    uv run python tests/fixtures/make_pieces_fixture.py

Sources: parcel shapes and owner names from `parcels` (the targets and every parcel touching the
boxes 150 ft around them), constraint shapes from `constraint_features` and street centerlines
from `streets`, clipped to those boxes (wider than any buffer or street reach used inside them);
the slope band raster cut to each target's box. All from the 2026-10-27/28 runs.
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

OUT = Path(__file__).with_name("meck_pieces")
BOX_FT = 150

PINS = {
    "21311502": "8625 Sardis Rd: Sardis Branch cuts off 0.97 ac behind the creek",
    "03513301": "715 Dale Av: the piece behind the creek touches the same owner's land, no street",
}


def _frame(rows, cols):
    return gpd.GeoDataFrame(pd.DataFrame([r[:-1] for r in rows], columns=cols),
                            geometry=shapely.from_wkb([bytes(r[-1]) for r in rows]), crs=2264)


if __name__ == "__main__":
    cfg = yaml.safe_load((ROOT / "config" / "constraints.yaml").read_text())
    OUT.mkdir(exist_ok=True)
    engine = get_engine()
    with engine.connect() as conn:
        targets = _frame(conn.execute(text("SELECT pin, ST_AsBinary(geom) FROM parcels WHERE pin = ANY(:p)"),
                                      {"p": list(PINS)}).all(), ["pin"])
        boxes = shapely.union_all([shapely.box(*shapely.buffer(g, BOX_FT).bounds) for g in targets.geometry])
        b = {"b": shapely.to_wkb(boxes)}
        parcels = _frame(conn.execute(text(
            "SELECT pin, owner_name, ST_AsBinary(geom) FROM parcels WHERE ST_Intersects(geom, ST_GeomFromWKB(:b, 2264))"),
            b).all(), ["pin", "owner_name"])
        feats = _frame(conn.execute(text(
            "SELECT layer, class, ST_AsBinary(ST_Intersection(geom, ST_GeomFromWKB(:b, 2264))) FROM constraint_features "
            "WHERE (class = 'unbuildable' OR layer = 'streams') AND ST_Intersects(geom, ST_GeomFromWKB(:b, 2264))"),
            b).all(), ["layer", "class"])
        streets = _frame(conn.execute(text(
            "SELECT objectid, thoroughfare, lifecycle, ST_AsBinary(ST_Multi(ST_Intersection(geom, ST_GeomFromWKB(:b, 2264)))) "
            "FROM streets WHERE ST_Intersects(geom, ST_GeomFromWKB(:b, 2264))"), b).all(),
            ["objectid", "thoroughfare", "lifecycle"])
    feats = feats[~feats.geometry.is_empty & feats.intersects(boxes)].reset_index(drop=True)
    parcels.to_file(OUT / "parcels.gpkg", layer="parcels")
    feats.to_file(OUT / "features.gpkg", layer="features")
    streets.to_file(OUT / "streets.gpkg", layer="streets")

    raster = ROOT / cfg["derived_dir"] / f"slope_bands_{int(cfg['slope']['resolution_ft'])}ft.tif"
    with rasterio.open(raster) as src:
        for pin, geom in zip(targets["pin"], targets.geometry):
            w = from_bounds(*shapely.buffer(geom, BOX_FT).bounds, transform=src.transform).round_offsets().round_lengths()
            profile = {**src.profile, "width": w.width, "height": w.height, "transform": src.window_transform(w),
                       "tiled": False, "compress": "deflate"}
            profile.pop("blockxsize", None), profile.pop("blockysize", None)
            with rasterio.open(OUT / f"slope_{pin}.tif", "w", **profile) as dst:
                dst.write(src.read(1, window=w), 1)
    print(f"Wrote {len(parcels)} parcels, {len(feats)} constraint shapes, {len(streets)} streets to {OUT}")
