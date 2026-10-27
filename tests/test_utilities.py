"""Tests for ingest/utilities.py against real Mecklenburg rows (tests/fixtures/meck_utilities/).

Expected plat years and project distances were read from the full 2026-10-27 run; see
tests/fixtures/make_utilities_fixture.py for what each parcel covers.
"""

from datetime import date
from pathlib import Path

import pandas as pd
import pyogrio
import pytest
import shapely
from sqlalchemy import text

from ingest.db import copy_dataframe, get_engine
from ingest.utilities import assign_parcels, check_restricted, load_cip, quality_report, read_cip

FIXTURE = Path(__file__).parent / "fixtures" / "meck_utilities"
S = "test_utilities"
AS_OF = date(2026, 10, 27)
TYPES = ["Sewer", "Water"]
PLANNED = ["Planning", "Design", "Design-Build", "Procurement Services", "Construction", "Active", "On Hold"]


def test_restricted_files_stop_the_run(tmp_path):
    check_restricted(tmp_path / "missing")  # no folder: fine
    check_restricted(tmp_path)  # empty folder: fine
    (tmp_path / "sewer_lines.gpkg").write_bytes(b"")
    with pytest.raises(SystemExit, match="restricted"):
        check_restricted(tmp_path)


def test_read_cip_keeps_only_water_and_sewer():
    cip = read_cip(FIXTURE / "cip.geojson", TYPES, PLANNED)
    assert set(cip["project_type"]) == {"Sewer"}
    assert len(cip) == 3
    phases = dict(zip(cip["project_id"], cip["planned"]))
    assert phases == {"DNC-40-19-004": True, "DNC-40-23-300": False, "DNC-40-24-329": False}
    assert cip.crs.to_epsg() == 2264


@pytest.fixture(scope="module")
def loaded():
    engine = get_engine()
    parcels = pyogrio.read_dataframe(FIXTURE / "parcels.gpkg").rename(columns={"pid": "pin"})
    owners = pd.read_csv(FIXTURE / "owners.csv", dtype=str, keep_default_na=False)
    built = pd.read_csv(FIXTURE / "year_built.csv", dtype={"pin": str})
    with engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {S} CASCADE; CREATE SCHEMA {S}"))
        copy_dataframe(conn, pd.DataFrame({
            "pin": parcels["pin"],
            "geom": shapely.to_wkb(shapely.set_srid(parcels.geometry.values, 2264), hex=True, include_srid=True),
        }), f"{S}.parcels", {"geom": "geometry(MultiPolygon, 2264)"})
        copy_dataframe(conn, pd.DataFrame({"pin": owners["pid"], "account_pin": owners["camapid"],
                                           "legal_description": owners["txt_legaldesc"]}), f"{S}.parcel_owners")
        copy_dataframe(conn, pd.DataFrame({
            "pin": built["pin"], "year_built": built["year_built"].astype("Int64"),
            "snapshot_date": pd.to_datetime(built["snapshot_date"]),
        }), f"{S}.nconemap_parcels_snapshots")
    changes = {
        "cip_projects": load_cip(read_cip(FIXTURE / "cip.geojson", TYPES, PLANNED), engine, AS_OF, S),
        "parcel_utilities": assign_parcels(engine, AS_OF, 1980, 1000, S),
    }
    with engine.connect() as conn:
        result = pd.read_sql(text(f"SELECT * FROM {S}.parcel_utilities"), conn).set_index("pin")
    yield engine, changes, result
    with engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA {S} CASCADE"))


def test_one_row_per_parcel(loaded):
    _, changes, u = loaded
    assert len(u) == 7 and changes["parcel_utilities"]["added"] == 7


def test_recent_plat_is_served_low_confidence(loaded):
    r = loaded[2].loc["08920128"]  # 443 Dawn Cir
    assert (r.sewer_status, r.sewer_confidence, r.water_status, r.water_confidence) == ("served", "low", "served", "low")
    assert (r.plat_ref, r.plat_year, r.plat_homes, r.plat_first_home_pin) == ("73-876", 2023, 3, "08920112")
    assert r.sewer_source == "proxy: plat after 1980" and r.sewer_source_date == date(2026, 1, 7)
    assert pd.isna(r.sewer_distance_ft) and pd.isna(r.water_distance_ft)


def test_new_plat_without_homes_is_unknown(loaded):
    u = loaded[2]
    for pin, ref in (("03141608", "69-548"), ("04104279", "76-221")):
        r = u.loc[pin]
        assert (r.sewer_status, r.plat_ref) == ("unknown", ref)
        assert pd.isna(r.plat_year) and pd.isna(r.sewer_confidence) and pd.isna(r.sewer_source)


def test_old_house_on_recent_plat_stays_unknown(loaded):
    r = loaded[2].loc["00102115"]
    assert (r.plat_ref, r.plat_year, r.sewer_status) == ("74-885", 1940, "unknown")


def test_cutoff_is_strictly_after(loaded):
    r = loaded[2].loc["00319105"]
    assert (r.plat_year, r.sewer_status) == (1980, "unknown")


def test_no_plat_reference(loaded):
    r = loaded[2].loc["00101113"]
    assert pd.isna(r.plat_ref) and r.sewer_status == "unknown" and r.water_status == "unknown"


def test_planned_project_nearby(loaded):
    u = loaded[2]
    r = u.loc["00716240"]
    assert r.sewer_status == "served" and r.plat_year == 2006
    assert r.planned_project_nearby and r.planned_project_id == "DNC-40-19-004"
    assert r.planned_project_name == "Sanitary Sewer extension for Sam Furr Road"
    assert (r.planned_project_type, r.planned_project_phase) == ("Sewer", "Active")
    assert r.planned_project_distance_ft == pytest.approx(317, abs=1)
    # completed sewer projects nearby are not "planned"
    assert not u.drop(index="00716240")["planned_project_nearby"].any()


def test_report_counts(loaded):
    r = quality_report(loaded[0], S)
    assert dict(((s, c), n) for s, c, n in r["sewer_by_status_confidence"]) == {("served", "low"): 2, ("unknown", "-"): 5}
    assert r["parcels_near_planned_project"] == [("Sewer", 1)]


def test_rerun_stores_no_changes(loaded):
    engine = loaded[0]
    assert assign_parcels(engine, AS_OF, 1980, 1000, S) == {"added": 0, "changed": 0, "removed": 0, "current": 7}
