"""Tests for ingest/zoning.py against real Mecklenburg parcels and districts (tests/fixtures/meck_zoning/).

Expected shares were measured in PostGIS against the published shapes on 2026-10-27; the
fixture keeps every district shape touching these parcels, so the results must match.
"""

from datetime import date
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyogrio
import pytest
import shapely
from shapely.geometry import box
from sqlalchemy import text

from ingest.db import copy_dataframe, get_engine
from ingest.zoning import (
    assign_parcels, build_districts, cross_check, district_counts, load_districts, quality_report,
    read_charlotte, read_towns, resolve_overlaps,
)

FIXTURE = Path(__file__).parent / "fixtures" / "meck_zoning"
SPLIT = 0.05
AS_OF = date(2026, 10, 27)
S = "test_zoning"


def fixture_districts(towns_edit=None):
    towns = read_towns(FIXTURE / "towns.gpkg", "Zoning_County_Towns")
    if towns_edit:
        towns = towns_edit(towns)
    return build_districts(read_charlotte(FIXTURE / "charlotte.gpkg"), towns)


def square(x, y, size=100):
    return shapely.MultiPolygon([box(x, y, x + size, y + size)])


def test_precedence_town_then_newest_then_smallest():
    d = gpd.GeoDataFrame({
        "jurisdiction": ["CHARLOTTE", "MINT HILL", "CHARLOTTE", "CHARLOTTE", "CHARLOTTE"],
        "zone_code": ["OLD", "TOWN", "NEW", "BIG", "SMALL"],
        "rezone_date": pd.to_datetime(["2001-01-01", None, "2024-01-01", "2023-06-01", "2023-06-01"]),
    }, geometry=[square(0, 0), square(50, 0), square(0, 50), square(500, 0, 200), square(550, 50)], crs=2264)
    r = resolve_overlaps(d).set_index("zone_code")
    assert r.loc["TOWN", "geom_effective"].area == 100 * 100  # towns always win
    assert r.loc["NEW", "geom_effective"].area == 100 * 100 - 50 * 50  # loses the corner the town covers
    assert r.loc["OLD", "geom_effective"].area == 50 * 50  # loses to the town and the newer rezoning
    assert not r.loc["OLD", "overlap_conflict"]
    assert r.loc["SMALL", "geom_effective"].area == 100 * 100  # same date: smaller wins
    assert r.loc["BIG", "overlap_conflict"] and r.loc["BIG", "conflict_geom"].area == 100 * 100


def test_districts_read_from_both_layers():
    d = fixture_districts()
    assert set(d["jurisdiction"]) == {"CHARLOTTE", "HUNTERSVILLE", "CORNELIUS", "MINT HILL", "STALLINGS"}
    sp = d[d["zone_code"] == "SP(CD)"].iloc[0]
    assert sp["conditional"] and sp["base_code"] == "SP"
    assert d.loc[d["zone_code"] == "N2-B(HDO)", "overlay"].iloc[0] == "HDO"
    assert d["district_id"].is_unique


@pytest.fixture(scope="module")
def loaded():
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {S} CASCADE; CREATE SCHEMA {S}"))
        parcels = pyogrio.read_dataframe(FIXTURE / "parcels.gpkg").rename(columns={"pid": "pin"})
        rows = pd.DataFrame({
            "pin": parcels["pin"],
            "geom": shapely.to_wkb(shapely.set_srid(parcels.geometry.values, 2264), hex=True, include_srid=True),
        })
        copy_dataframe(conn, rows, f"{S}.parcels", {"geom": "geometry(MultiPolygon, 2264)"})
    changes = {
        "zoning_districts": load_districts(fixture_districts(), engine, AS_OF, S),
        "parcel_zoning": assign_parcels(engine, AS_OF, SPLIT, S),
    }
    with engine.connect() as conn:
        result = pd.read_sql(text(f"SELECT * FROM {S}.parcel_zoning"), conn).set_index("pin")
    yield engine, changes, result
    with engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA {S} CASCADE"))


def test_every_parcel_gets_one_row(loaded):
    _, changes, z = loaded
    assert len(z) == 12
    assert changes["parcel_zoning"] == {"added": 12, "changed": 0, "removed": 0, "current": 12}


def test_whole_parcel_in_one_town_district(loaded):
    r = loaded[2].loc["00101102"]
    assert (r.jurisdiction, r.zone_code, r.zone_share, r.district_count, r.split) == ("HUNTERSVILLE", "GR", 1, 1, False)


def test_split_within_charlotte(loaded):
    r = loaded[2].loc["02907118"]
    assert (r.zone_code, r.second_zone_code) == ("N1-A", "I-1(CD)")
    assert r.zone_share == pytest.approx(0.670, abs=0.001) and r.second_share == pytest.approx(0.330, abs=0.001)
    assert r.split and not r.crosses_jurisdiction


def test_split_across_jurisdictions(loaded):
    z = loaded[2]
    two_towns = z.loc["01106231"]
    assert (two_towns.jurisdiction, two_towns.zone_code) == ("HUNTERSVILLE", "TR")
    assert (two_towns.second_jurisdiction, two_towns.second_zone_code) == ("CORNELIUS", "RP")
    assert two_towns.zone_share == pytest.approx(0.505, abs=0.001) and two_towns.crosses_jurisdiction
    town_city = z.loc["10938108"]
    assert (town_city.jurisdiction, town_city.zone_code) == ("MINT HILL", "R")
    assert (town_city.second_jurisdiction, town_city.second_zone_code) == ("CHARLOTTE", "R-12MF(CD)")
    assert town_city.zone_share == pytest.approx(0.511, abs=0.001) and town_city.split


def test_town_overrides_charlotte(loaded):
    r = loaded[2].loc["13709398"]  # inside both Mint Hill R and Charlotte N2-B
    assert (r.jurisdiction, r.zone_code, r.zone_share) == ("MINT HILL", "R", pytest.approx(1, abs=0.001))
    assert r.second_share < 0.001 and not r.split  # only an edge sliver of N2-B is left


def test_same_date_tie_is_flagged(loaded):
    r = loaded[2].loc["16914126"]  # inside both ML-2 and CAC-2, both 2023-06-01
    assert r.zone_code == "CAC-2" and r.overlap_conflict and r.tie_share == pytest.approx(1)
    assert not loaded[2].drop(index="16914126")["overlap_conflict"].any()


def test_sliver_below_threshold_is_not_a_split(loaded):
    r = loaded[2].loc["08920128"]  # 443 Dawn Cir: 0.1% touches TOD-TR
    assert (r.zone_code, r.second_zone_code) == ("N1-B", "TOD-TR")
    assert r.second_share < 0.01 and r.district_count == 1 and not r.split


def test_conditional_overlay_stallings_and_unzoned(loaded):
    z = loaded[2]
    assert z.loc["00119106"].conditional and z.loc["00119106"].base_code == "SP"
    assert z.loc["07101101"].overlays == "HDO"
    assert (z.loc["19511201"].jurisdiction, z.loc["19511201"].zone_code) == ("STALLINGS", "R-20")
    unzoned = z.loc["02301127"]  # submerged land
    assert pd.isna(unzoned.zone_code) and unzoned.unzoned_share == pytest.approx(1)


def test_district_counts_include_every_code(loaded):
    engine = loaded[0]
    counts = district_counts(engine, S).set_index(["jurisdiction", "zone_code"])
    assert counts.loc[("MINT HILL", "R"), "parcels"] == 2
    assert counts.loc[("CHARLOTTE", "N2-B"), "parcels"] == 0  # present, but overridden by Mint Hill
    assert counts.loc[("CHARLOTTE", "N1-A"), "split_parcels"] == 1


def test_cross_check_against_county_list(loaded):
    z = loaded[2].reset_index()[["pin", "zone_code"]].dropna()
    r = cross_check(z, FIXTURE / "county_check.csv")
    assert r["check_parcels_compared"] == 11
    disagree = {d["pin"] for d in r["check_disagree_sample"]}
    assert disagree == {"03309301", "02907118"}  # the county's list is newer for these two


def test_quality_report(loaded):
    r = quality_report(loaded[0], SPLIT, FIXTURE / "county_check.csv", S)
    assert r["parcels"] == 12 and r["parcels_unzoned"] == 1
    assert r["parcels_split"] == 3 and r["parcels_crossing_jurisdictions"] == 2
    assert r["parcels_overlap_conflict"] == 1


def test_rerun_and_rezoning_only_store_changes(loaded):
    engine = loaded[0]
    assert load_districts(fixture_districts(), engine, AS_OF, S)["changed"] == 0
    assert assign_parcels(engine, AS_OF, SPLIT, S) == {"added": 0, "changed": 0, "removed": 0, "current": 12}

    # Next week Huntersville rezones the GR district: one district shape and its parcel change
    rezone = lambda t: t.assign(zone_code=t["zone_code"].replace({"GR": "GR(CD)"}))
    later = date(2026, 11, 3)
    districts = load_districts(fixture_districts(rezone), engine, later, S)
    assert districts["added"] + districts["changed"] >= 1 and districts["removed"] >= 1
    parcels = assign_parcels(engine, later, SPLIT, S)
    assert parcels["changed"] == 1
    with engine.connect() as conn:
        old = conn.execute(text(
            f"SELECT zone_code, valid_from, valid_to FROM {S}.parcel_zoning_history WHERE pin = '00101102'"
        )).one()
        new = conn.execute(text(f"SELECT zone_code FROM {S}.parcel_zoning WHERE pin = '00101102'")).scalar_one()
    assert old == ("GR", AS_OF, later) and new == "GR(CD)"


def test_petition_of_main_district(loaded):
    z = loaded[2]
    assert z.loc[z["jurisdiction"] != "CHARLOTTE", "petition"].isna().all()  # towns publish none
    # 02907118's petitioned I-1(CD) part is only its second district, so no main petition
    assert pd.isna(z.loc["02907118", "petition"]) and z.loc["02907118", "second_zone_code"] == "I-1(CD)"


def test_petition_from_largest_shape_of_main_district():
    # one MX-2 parcel over two MX-2 rezonings (70% / 30%) and a sliver of N1-A
    s = "test_zoning_petition"
    engine = get_engine()
    d = gpd.GeoDataFrame({
        "jurisdiction": "CHARLOTTE", "zone_code": ["MX-2", "MX-2", "N1-A"],
        "zone_class": None, "overlay": None, "spa": pd.array([pd.NA] * 3, dtype="boolean"),
        "petition": ["2007-098", "1999-074(C)", None],
        "rezone_date": pd.to_datetime(["2007-09-17", "2000-01-11", "2023-06-01"]),
        "source_file": "synthetic", "source_row": [0, 1, 2],
    }, geometry=[shapely.MultiPolygon([box(0, 0, 70, 100)]), shapely.MultiPolygon([box(70, 0, 100, 100)]),
                 square(100, 0)], crs=2264)
    towns = d.iloc[:0].copy()
    with engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {s} CASCADE; CREATE SCHEMA {s}"))
        parcel = shapely.set_srid(shapely.MultiPolygon([box(0, 0, 101, 100)]), 2264)
        copy_dataframe(conn, pd.DataFrame({"pin": ["P1"], "geom": [shapely.to_wkb(parcel, hex=True, include_srid=True)]}),
                       f"{s}.parcels", {"geom": "geometry(MultiPolygon, 2264)"})
    try:
        load_districts(build_districts(d, towns), engine, AS_OF, s)
        assign_parcels(engine, AS_OF, SPLIT, s)
        with engine.connect() as conn:
            r = conn.execute(text(f"SELECT zone_code, petition FROM {s}.parcel_zoning")).one()
        assert r == ("MX-2", "2007-098")
    finally:
        with engine.begin() as conn:
            conn.execute(text(f"DROP SCHEMA {s} CASCADE"))
