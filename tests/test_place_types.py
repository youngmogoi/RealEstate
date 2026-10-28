"""Tests for ingest/place_types.py: Policy Map shapes assigned to parcels by largest share."""

from datetime import date

import geopandas as gpd
import pandas as pd
import pytest
import shapely
import yaml
from shapely.geometry import box
from sqlalchemy import text

from ingest.db import copy_dataframe, get_engine
from ingest.place_types import ROOT, assign_parcels, load, read

S = "test_place_types"
AS_OF = date(2026, 10, 28)
CFG = yaml.safe_load((ROOT / "config" / "ingest.yaml").read_text(encoding="utf-8"))["policy_map"]


def _geojson(tmp_path):
    # Shapes as the layer serves them (WGS84); the second has the 2026-10 code mix-up (N2 named, N1 coded)
    shapes = gpd.GeoDataFrame({
        "OBJECTID": [1, 2, 3],
        "PlaceTypeFullTxt": ["Neighborhood 1", "Neighborhood 2", "Commercial"],
        "PlaceTypeCde": ["N1", "N1", "COMM"],
        "AdoptionType": "Comprehensive Plan", "AdoptionDate": [1648512000000] * 3, "CommunityPlanningArea": "East",
    }, geometry=[box(1440000, 540000, 1440100, 540100), box(1440100, 540000, 1440200, 540100),
                 box(1440200, 540000, 1440300, 540100)], crs=2264).to_crs(4326)
    path = tmp_path / "policy_map.geojson"
    path.write_text(shapes.to_json())
    return path


@pytest.fixture
def loaded(tmp_path):
    engine = get_engine()
    shapes = read(_geojson(tmp_path), CFG["codes"])
    parcels = {
        "N1": box(1440010, 540010, 1440090, 540090),     # inside Neighborhood 1
        "SPLIT": box(1440070, 540010, 1440130, 540090),  # half N1, half N2 (mismatched code)
        "EDGE": box(1440180, 540010, 1440202, 540090),   # 20 ft N2, 2 ft (9%) Commercial: split
        "OUT": box(1450000, 550000, 1450050, 550050),    # off the map (a town)
    }
    with engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {S} CASCADE; CREATE SCHEMA {S}"))
        rows = pd.DataFrame({"pin": list(parcels), "geom": [
            shapely.to_wkb(shapely.set_srid(shapely.MultiPolygon([g]), 2264), hex=True, include_srid=True)
            for g in parcels.values()]})
        copy_dataframe(conn, rows, f"{S}.parcels", {"geom": "geometry(MultiPolygon, 2264)"})
    changes = {"policy_map": load(shapes, engine, AS_OF, S),
               "parcels": assign_parcels(engine, AS_OF, CFG["split_min_share"], S)}
    with engine.connect() as conn:
        result = pd.read_sql(text(f"SELECT * FROM {S}.parcel_place_types"), conn).set_index("pin")
    yield engine, changes, result, shapes
    with engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA {S} CASCADE"))


def test_read_flags_code_mismatch(loaded):
    shapes = loaded[3].set_index("objectid")
    assert shapes["place_type_code_mismatch"].tolist() == [False, True, False]
    assert shapes.crs.to_epsg() == 2264 and shapes.loc[1, "geom"].area == pytest.approx(100 * 100, rel=1e-3)


def test_assign(loaded):
    z = loaded[2]
    assert z.loc["N1", "place_type"] == "Neighborhood 1" and not z.loc["N1", "place_type_split"]
    split = z.loc["SPLIT"]
    assert {split.place_type, split.second_place_type} == {"Neighborhood 1", "Neighborhood 2"} and split.place_type_split
    assert z.loc["EDGE", "place_type"] == "Neighborhood 2" and z.loc["EDGE", "place_type_code_mismatch"]
    assert z.loc["EDGE", "second_place_type"] == "Commercial" and z.loc["EDGE", "place_type_split"]
    assert pd.isna(z.loc["OUT", "place_type"]) and not z.loc["OUT", "place_type_split"]


def test_rerun_stores_nothing(loaded):
    engine = loaded[0]
    assert assign_parcels(engine, AS_OF, CFG["split_min_share"], S) == {
        "added": 0, "changed": 0, "removed": 0, "current": 4}
