"""Tests for features/pieces.py against real Mecklenburg rows (tests/fixtures/meck_pieces/).

Expected values were read from the 2026-10-28 run around these parcels; the fixture keeps every
parcel, constraint shape, street and slope pixel near them, so the results must match.
"""

from datetime import date
from pathlib import Path

import pandas as pd
import pyogrio
import pytest
import shapely
import yaml
from sqlalchemy import text

from features.pieces import ROOT, build
from ingest.db import copy_dataframe, get_engine

FIXTURE = Path(__file__).parent / "fixtures" / "meck_pieces"
S = "test_pieces"
AS_OF = date(2026, 10, 28)
CFG = yaml.safe_load((ROOT / "config" / "features.yaml").read_text())
CCFG = yaml.safe_load((ROOT / "config" / "constraints.yaml").read_text())


def _wkb(gdf):
    return shapely.to_wkb(shapely.set_srid(gdf.geometry.values, 2264), hex=True, include_srid=True)


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    engine = get_engine()
    parcels = pyogrio.read_dataframe(FIXTURE / "parcels.gpkg")
    feats = pyogrio.read_dataframe(FIXTURE / "features.gpkg")
    streets = pyogrio.read_dataframe(FIXTURE / "streets.gpkg")
    with engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {S} CASCADE; CREATE SCHEMA {S}"))
        copy_dataframe(conn, pd.DataFrame({"pin": parcels["pin"], "owner_name": parcels["owner_name"], "geom": _wkb(parcels)}),
                       f"{S}.parcels", {"geom": "geometry(MultiPolygon, 2264)"})
        copy_dataframe(conn, pd.DataFrame({"layer": feats["layer"], "class": feats["class"], "geom": _wkb(feats)}),
                       f"{S}.constraint_features", {"geom": "geometry(Geometry, 2264)"})
        copy_dataframe(conn, pd.DataFrame({"objectid": streets["objectid"], "thoroughfare": streets["thoroughfare"],
                                           "lifecycle": streets["lifecycle"], "geom": _wkb(streets)}),
                       f"{S}.streets", {"geom": "geometry(MultiLineString, 2264)"})
    changes = []
    for tif in sorted(FIXTURE.glob("slope_*.tif")):
        # one target per raster window: build on each, keep that target's pieces
        changes.append(build(engine, CFG, CCFG, tif, AS_OF, S))
        with engine.begin() as conn:
            conn.execute(text(f"CREATE TABLE IF NOT EXISTS {S}.kept AS SELECT * FROM {S}.parcel_pieces WITH NO DATA"))
            conn.execute(text(f"INSERT INTO {S}.kept SELECT * FROM {S}.parcel_pieces WHERE pin = :p"),
                         {"p": tif.stem.removeprefix("slope_")})
    with engine.connect() as conn:
        df = pd.read_sql(text(f"SELECT * FROM {S}.kept"), conn).set_index(["pin", "piece_no"])
    rerun = build(engine, CFG, CCFG, FIXTURE / "slope_21311502.tif", AS_OF, S)
    yield df, changes, rerun
    with engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA {S} CASCADE"))


def test_sardis_branch_cuts_off_the_back_of_21311502(result):
    df, _, _ = result
    front, back = df.loc[("21311502", 1)], df.loc[("21311502", 2)]
    # front: 134,807.7 sf outside the buffers, 31,980.8 sf of it over 25% slope
    assert front["area_sf"] == pytest.approx(134807.7, abs=1)
    assert front["slope_unbuildable_sf"] == pytest.approx(31980.8, abs=1)
    assert front["net_sf"] == pytest.approx(102826.9, abs=1)
    assert front["touches_street"] and front["creek"]
    # back: beyond the creek, on no street and no land of the same owner
    assert back["net_sf"] == pytest.approx(42428.9, abs=1)
    assert not back["touches_street"] and not back["touches_same_owner"] and back["street_contact_ft"] == 0
    assert len(df.loc["21311502"]) == 2


def test_piece_reached_through_same_owner_land_03513301(result):
    # 715 Dale Av: no street frontage; the piece behind the creek touches 236.6 ft of the same
    # owner's adjoining parcel (assemblage access), the other piece touches nothing
    df, _, _ = result
    p1, p2 = df.loc[("03513301", 1)], df.loc[("03513301", 2)]
    assert p1["net_sf"] == pytest.approx(53478.9, abs=1) and not p1["touches_street"] and not p1["touches_same_owner"]
    assert p2["net_sf"] == pytest.approx(51612.9, abs=1) and p2["owner_contact_ft"] == pytest.approx(236.6, abs=0.5)
    assert p2["touches_same_owner"] and not p2["touches_street"]


def test_rerun_stores_no_changes(result):
    _, _, rerun = result
    assert rerun["added"] == rerun["changed"] == rerun["removed"] == 0
