"""Tests for ingest/nconemap_parcels.py against real Mecklenburg parcels (tests/fixtures/meck_parcels_sample.gpkg).

Expected values were read by hand from the source shapefile, 2026-01-07 download.
"""

from datetime import date
from pathlib import Path

import pandas as pd
import pytest
import shapely
from shapely.geometry import Polygon
from sqlalchemy import text

from ingest.db import get_engine
from ingest.nconemap_parcels import acres_mismatch, clean, format_report, load, quality_report, read_publication_date, read_source

FIXTURE = Path(__file__).parent / "fixtures" / "meck_parcels_sample.gpkg"
PLACEHOLDERS = ["1900-01-01", "1975-01-01"]
THRESHOLD = 0.20


@pytest.fixture(scope="module")
def parcels():
    return clean(read_source(FIXTURE), PLACEHOLDERS, THRESHOLD)


def row(df, pin):
    return df[df["pin"] == pin].iloc[0]


def test_reads_in_state_plane(parcels):
    assert parcels.crs.to_epsg() == 2264
    assert set(parcels.geom_type) <= {"Polygon", "MultiPolygon"}


def test_single_family_fields(parcels):
    r = row(parcels, "00101305")
    assert r["owner_name"] == "CODY WILLIAM FERGUSON"
    assert r["gis_acres"] == pytest.approx(0.830055, abs=1e-6)
    assert r["land_value"] == 170000
    assert r["building_value"] == 239200
    assert r["total_value"] == 410400
    assert r["parcel_use"] == "SINGLE FAMILY RESIDENTIAL"
    assert r["sale_date"] == pd.Timestamp("2021-07-01")
    assert r["year_built"] == 1976
    assert r["pin_row_count"] == 1


def test_placeholder_sale_dates_become_null(parcels):
    for pin in ("00102326", "00101342"):  # 1975-01-01 and 1900-01-01 in source
        r = row(parcels, pin)
        assert pd.isna(r["sale_date"])
        assert r["sale_date_placeholder"]
    assert not row(parcels, "00101305")["sale_date_placeholder"]


def test_year_built_zero_is_null(parcels):
    assert pd.isna(row(parcels, "00101342")["year_built"])


def test_multi_building_parcel_keeps_one_row_per_building(parcels):
    rows = parcels[parcels["pin"] == "00101111"]
    assert len(rows) == 5
    assert (rows["pin_row_count"] == 5).all()
    assert sorted(rows["year_built"]) == [1994, 2000, 2007, 2013, 2024]
    assert (rows["building_value"] == 7484300).all()  # parcel total repeated, not per building


def test_deeded_acres_absent_so_no_mismatch(parcels):
    assert parcels["deeded_acres"].isna().all()
    assert not parcels["acres_mismatch"].any()


def test_acres_mismatch_flags_over_threshold():
    gis = pd.Series([1.0, 1.0, 1.0, 1.0, 1.0])
    deeded = pd.Series([1.0, 1.2, 1.3, 0.8, None])
    # 0% diff; 1/6 = 16.7%; 0.3/1.3 = 23%; 0.2/0.8 = 25%; no deeded value
    assert acres_mismatch(gis, deeded, THRESHOLD).tolist() == [False, False, True, True, False]


def test_invalid_geometry_is_fixed():
    raw = read_source(FIXTURE).head(1).copy()
    bowtie = Polygon([(0, 0), (10, 10), (10, 0), (0, 10), (0, 0)])
    raw.loc[raw.index[0], "geometry"] = bowtie
    df = clean(raw, PLACEHOLDERS, THRESHOLD)
    assert df["geom_fixed"].iloc[0]
    assert shapely.is_valid(df.geometry.iloc[0])
    assert df.geometry.iloc[0].geom_type == "MultiPolygon"
    assert df.geometry.iloc[0].area == pytest.approx(50)


def test_quality_report(parcels):
    r = quality_report(parcels, THRESHOLD)
    assert r["rows"] == 11
    assert r["unique_pins"] == 7
    assert r["duplicate_pin_groups"] == 1
    assert r["duplicate_pin_rows"] == 5
    assert r["placeholder_sale_dates_nulled"] == 2
    assert r["invalid_geometries_fixed"] == 0
    assert r["null_rates"]["deeded_acres"] == 1.0
    assert r["null_rates"]["pin"] == 0.0
    assert "not checked" in format_report(r)


def test_publication_date_from_metadata(tmp_path):
    (tmp_path / "x_metadata.txt").write_text("      Publication_Date: 20260107\n")
    assert read_publication_date(tmp_path / "x_poly.shp") == date(2026, 1, 7)


@pytest.fixture
def test_schema():
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS test_ingest CASCADE; CREATE SCHEMA test_ingest"))
    yield engine
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA test_ingest CASCADE"))


def test_load_keeps_dated_snapshots(parcels, test_schema):
    engine = test_schema
    report = quality_report(parcels, THRESHOLD)
    src = "tests/fixtures/meck_parcels_sample.gpkg"

    assert load(parcels, engine, date(2026, 1, 7), src, report, schema="test_ingest")
    assert not load(parcels, engine, date(2026, 1, 7), src, report, schema="test_ingest")  # rerun: no duplicate
    assert load(parcels, engine, date(2026, 1, 14), src, report, schema="test_ingest")

    with engine.connect() as conn:
        q = lambda sql: conn.execute(text(sql)).all()
        assert q("SELECT count(*) FROM test_ingest.nconemap_parcels") == [(11,)]
        assert q(
            "SELECT snapshot_date, count(*) FROM test_ingest.nconemap_parcels_snapshots GROUP BY 1 ORDER BY 1"
        ) == [(date(2026, 1, 7), 11), (date(2026, 1, 14), 11)]
        assert q(
            "SELECT DISTINCT ST_SRID(geom), GeometryType(geom) FROM test_ingest.nconemap_parcels"
        ) == [(2264, "MULTIPOLYGON")]
        assert q(
            "SELECT land_value, sale_date, source_file FROM test_ingest.nconemap_parcels WHERE pin = '00101305'"
        ) == [(170000.0, date(2021, 7, 1), src)]
        assert q("SELECT count(*), sum(snapshot_written::int) FROM test_ingest.ingest_runs") == [(3, 2)]
