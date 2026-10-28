"""Rebuild tests/fixtures/meck_features/ for the frontage tests in tests/test_features.py.

Run (database up, after `make streets` and `make features`):
    uv run python tests/fixtures/make_features_fixture.py

Sources: parcel shapes from `parcels`; every street segment (from `streets`, the city's
CountyData/Streets layer, downloaded 2026-10-27) within 200 ft of these parcels, unchanged.
"""

from pathlib import Path

import geopandas as gpd
import pandas as pd
import shapely
from sqlalchemy import text

from ingest.db import get_engine

OUT = Path(__file__).with_name("meck_features")

PINS = {
    "08920128": "443 Dawn Cir: mid-block lot, one street",
    "03141608": "2137 Tom Sadler Rd: mid-block lot, one street",
    "01101413": "1521 Yellow Springs Dr: new lot fronting only a street the city still marks Proposed",
    "00101429": "14017 NC 73 Hwy: lot line 50 ft from a major road's centerline (163 ft of frontage)",
}
# Added from the full build: a corner lot, a through lot, a flag lot, and a landlocked parcel
EXTRA_SQL = {
    "corner": "SELECT pin FROM parcel_features WHERE corner_lot AND gross_acres BETWEEN 0.15 AND 0.4 "
              "AND land_use = 'SINGLE FAMILY RESIDENTIAL' AND private_frontage_ft = 0 ORDER BY pin LIMIT 1",
    "through": "SELECT pin FROM parcel_features WHERE through_lot AND gross_acres BETWEEN 0.15 AND 0.6 "
               "AND land_use = 'SINGLE FAMILY RESIDENTIAL' AND private_frontage_ft = 0 ORDER BY pin LIMIT 1",
    "flag": "SELECT pin FROM parcel_features WHERE flag_lot AND gross_acres BETWEEN 0.5 AND 3 "
            "AND land_use = 'SINGLE FAMILY RESIDENTIAL' ORDER BY pin LIMIT 1",
    "landlocked": "SELECT pin FROM parcel_features WHERE landlocked AND gross_acres BETWEEN 1 AND 10 "
                  "AND land_use LIKE 'SINGLE FAMILY%' ORDER BY pin LIMIT 1",
}

if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    engine = get_engine()
    with engine.connect() as conn:
        pins = list(PINS) + [conn.execute(text(q)).scalar_one() for q in EXTRA_SQL.values()]
        rows = conn.execute(text("SELECT pin, ST_AsBinary(geom) FROM parcels WHERE pin = ANY(:p)"), {"p": pins}).all()
        parcels = gpd.GeoDataFrame({"pin": [r[0] for r in rows]},
                                   geometry=shapely.from_wkb([bytes(r[1]) for r in rows]), crs=2264)
        near = shapely.to_wkb(shapely.buffer(shapely.union_all(parcels.geometry.values), 200))
        streets = gpd.read_postgis(text(
            "SELECT objectid, street_code, name, street_class, thoroughfare, lifecycle, jurisdiction, source_file, geom "
            "FROM streets WHERE ST_Intersects(geom, ST_GeomFromWKB(:g, 2264))"), conn, geom_col="geom", params={"g": near})
    parcels.to_file(OUT / "parcels.gpkg", layer="parcels")
    streets.to_file(OUT / "streets.gpkg", layer="streets")
    labels = dict(zip(pins, list(PINS.values()) + list(EXTRA_SQL)))
    pd.Series(labels, name="case").rename_axis("pin").to_csv(OUT / "cases.csv")
    print(f"Wrote {len(parcels)} parcels ({', '.join(f'{p}={c}' for p, c in labels.items())}) and {len(streets)} street segments")
