"""Tests for ingest/constraints.py against real Mecklenburg rows (tests/fixtures/meck_constraints/).

Expected acres were read from the full 2026-10-27 run (`make constraints`); the fixture keeps
every constraint shape and slope pixel around these parcels, so the results must match.
Classes come from config/constraints.yaml, so these tests check the rules as configured.
"""

from datetime import date
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import pytest
import shapely
import yaml
from sqlalchemy import text

from ingest.constraints import ROOT, _db_lookups, assign_parcels, load_features, slope_acres, slope_bands
from ingest.db import copy_dataframe, get_engine

FIXTURE = Path(__file__).parent / "fixtures" / "meck_constraints"
S = "test_constraints"
AS_OF = date(2026, 10, 27)
CFG = yaml.safe_load((ROOT / "config" / "constraints.yaml").read_text())
TOL = 0.002  # acres


def test_slope_band_edges():
    bands = CFG["slope"]["bands"]
    pct = np.array([[0, 15, 15.01, 25], [25.01, 60, np.nan, 14.99]])
    assert slope_bands(pct, bands).tolist() == [[0, 0, 1, 1], [2, 2, 255, 0]]


def test_wetlands_are_a_flag_and_slope_bands_as_decided():
    assert CFG["layers"]["possible_unmapped_wetland"]["class"] == "flag"
    assert CFG["layers"]["water_quality_buffers"]["exclude"] == {"type": ["Piped - No Buffer"]}
    assert {n: b["class"] for n, b in CFG["slope"]["bands"].items()} == {"slope_15_25": "flag", "slope_over_25": "unbuildable"}


@pytest.fixture(scope="module")
def result():
    engine = get_engine()
    parcels = pyogrio.read_dataframe(FIXTURE / "parcels.gpkg")
    features = pyogrio.read_dataframe(FIXTURE / "features.gpkg")
    features["source_date"] = pd.to_datetime(features["source_date"]).dt.date
    utilities = pd.read_csv(FIXTURE / "utilities.csv", dtype={"pin": str})
    with engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {S} CASCADE; CREATE SCHEMA {S}"))
        copy_dataframe(conn, pd.DataFrame({
            "pin": parcels["pin"],
            "geom": shapely.to_wkb(shapely.set_srid(parcels.geometry.values, 2264), hex=True, include_srid=True),
        }), f"{S}.parcels", {"geom": "geometry(MultiPolygon, 2264)"})
        copy_dataframe(conn, utilities, f"{S}.parcel_utilities")
    load_features(gpd.GeoDataFrame(features, geometry="geometry", crs=2264), engine, AS_OF, S)

    parcels_in, unbuildable_in, _ = _db_lookups(engine, S)
    parts = [slope_acres(tif, parcels_in, unbuildable_in, CFG["slope"]["bands"], log=lambda *a: None)
             for tif in sorted(FIXTURE.glob("slope_*.tif"))]
    slope = pd.concat(parts).groupby("pin", as_index=False).sum()
    dates = {l: AS_OF for l in [*CFG["layers"], "septic_soils", *CFG["slope"]["bands"]]}
    changes = assign_parcels(engine, AS_OF, CFG, slope, dates, S)
    with engine.connect() as conn:
        df = pd.read_sql(text(f"SELECT * FROM {S}.parcel_constraints"), conn).set_index("pin")
    yield engine, slope, dates, changes, df
    with engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA {S} CASCADE"))


def check(row, **expected):
    for col, value in expected.items():
        assert row[col] == pytest.approx(value, abs=TOL), col


def test_net_is_never_negative_or_above_gross(result):
    df = result[4]
    assert len(df) == 5
    assert (df["net_buildable_acres"] >= 0).all() and (df["net_buildable_acres"] <= df["gross_acres"]).all()
    assert np.allclose(df["net_buildable_acres"] + df["unbuildable_acres"], df["gross_acres"])


def test_overlaps_inside_encroachment_count_once(result):
    r = result[4].loc["10101119"]
    check(r, gross_acres=9.795, community_encroachment_acres=8.219, fema_floodway_acres=7.131,
          water_quality_buffers_acres=4.202, slope_over_25_acres=0.233, unbuildable_acres=8.219, net_buildable_acres=1.576)
    # unbuildable equals the encroachment area: floodway, buffers and steep slope all lie inside it
    assert r.unbuildable_acres < r.community_encroachment_acres + r.fema_floodway_acres + r.water_quality_buffers_acres


def test_flags_are_measured_not_deducted(result):
    r = result[4].loc["10101119"]
    check(r, fema_floodplain_acres=8.871, community_floodplain_acres=8.912, floodplain_flag_acres=0.694,
          possible_unmapped_wetland_acres=0.519, slope_15_25_acres=0.592)
    assert r.stream_length_ft == pytest.approx(904, abs=1)


def test_buffer_and_steep_slope_overlap_once(result):
    r = result[4].loc["00110299"]
    check(r, water_quality_buffers_acres=2.016, slope_over_25_acres=1.127, slope_15_25_acres=0.967, unbuildable_acres=2.671)
    overlap = r.water_quality_buffers_acres + r.slope_over_25_acres - r.unbuildable_acres
    assert overlap == pytest.approx(0.473, abs=TOL)


def test_slope_only_parcel(result):
    r = result[4].loc["10102103"]
    check(r, slope_over_25_acres=0.433, slope_15_25_acres=0.372, unbuildable_acres=0.433, net_buildable_acres=7.186,
          water_quality_buffers_acres=0)


def test_septic_only_where_sewer_not_served(result):
    df = result[4]
    unserved = df.loc["10101119"]
    assert unserved.septic_checked and unserved.septic_dominant_rating == "Very limited"
    check(unserved, septic_very_limited_acres=5.214)
    assert df.loc["10102103"].septic_dominant_rating == "Somewhat limited"
    served = df.loc["08920128"]
    assert not served.septic_checked and pd.isna(served.septic_very_limited_acres) and pd.isna(served.septic_dominant_rating)


def test_your_city_lots(result):
    df = result[4]
    check(df.loc["08920128"], gross_acres=0.180, unbuildable_acres=0, net_buildable_acres=0.180, slope_15_25_acres=0.002)
    check(df.loc["03141608"], gross_acres=0.264, unbuildable_acres=0, net_buildable_acres=0.264, slope_15_25_acres=0)


def test_dates_stored_per_layer(result):
    r = result[4].loc["10101119"]
    assert r.community_encroachment_date == AS_OF and r.slope_over_25_date == AS_OF and r.septic_soils_date == AS_OF


def test_rerun_stores_no_changes(result):
    engine, slope, dates, changes, _ = result
    assert changes["added"] == 5
    assert assign_parcels(engine, AS_OF, CFG, slope, dates, S) == {"added": 0, "changed": 0, "removed": 0, "current": 5}
