"""Tests for ingest/utilities.py against real Mecklenburg rows (tests/fixtures/meck_utilities/).

Expected plat years, lot sizes and project distances were read from the full 2026-10-27 run;
see tests/fixtures/make_utilities_fixture.py for what each parcel covers. Thresholds come
from config/ingest.yaml, so these tests check the rules as configured.
"""

from datetime import date
from pathlib import Path

import pandas as pd
import pyogrio
import pytest
import shapely
import yaml
from sqlalchemy import text

from ingest.db import copy_dataframe, get_engine
from ingest.utilities import ROOT, assign_parcels, check_restricted, load_cip, quality_report, read_cip

FIXTURE = Path(__file__).parent / "fixtures" / "meck_utilities"
S = "test_utilities"
AS_OF = date(2026, 10, 27)
OWNERS_DATE = date(2026, 10, 26)
CFG = yaml.safe_load((ROOT / "config" / "ingest.yaml").read_text())["utilities"]


def test_restricted_files_stop_the_run(tmp_path):
    check_restricted(tmp_path / "missing")  # no folder: fine
    check_restricted(tmp_path)  # empty folder: fine
    (tmp_path / "sewer_lines.gpkg").write_bytes(b"")
    with pytest.raises(SystemExit, match="restricted"):
        check_restricted(tmp_path)


def test_read_cip_keeps_only_water_and_sewer():
    cip = read_cip(FIXTURE / "cip.geojson", CFG["cip_types"], CFG["cip_planned_phases"])
    assert set(cip["project_type"]) == {"Sewer"}
    assert dict(zip(cip["project_id"], cip["planned"])) == {
        "DNC-40-19-004": True, "DNC-40-23-300": False, "DNC-40-24-329": False,
    }
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
        copy_dataframe(conn, pd.DataFrame({
            "pin": owners["pid"], "account_pin": owners["camapid"], "legal_description": owners["txt_legaldesc"],
            "municipality": owners["municipality_desc"].replace("", None),
            "valid_from": pd.Timestamp(OWNERS_DATE),
        }), f"{S}.parcel_owners")
        copy_dataframe(conn, pd.DataFrame({
            "pin": built["pin"], "year_built": built["year_built"].astype("Int64"),
            "snapshot_date": pd.to_datetime(built["snapshot_date"]),
        }), f"{S}.nconemap_parcels_snapshots")
    cip = read_cip(FIXTURE / "cip.geojson", CFG["cip_types"], CFG["cip_planned_phases"])
    changes = {
        "cip_projects": load_cip(cip, engine, AS_OF, S),
        "parcel_utilities": assign_parcels(engine, AS_OF, CFG, S),
    }
    with engine.connect() as conn:
        result = pd.read_sql(text(f"SELECT * FROM {S}.parcel_utilities"), conn).set_index("pin")
    yield engine, changes, result
    with engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA {S} CASCADE"))


def test_one_row_per_parcel(loaded):
    _, changes, u = loaded
    assert len(u) == 29 and changes["parcel_utilities"]["added"] == 29


def test_first_home_rule(loaded):
    r = loaded[2].loc["08920128"]  # 443 Dawn Cir
    assert (r.sewer_status, r.sewer_confidence, r.water_status, r.water_confidence) == ("served", "low", "served", "low")
    assert (r.sewer_source, r.sewer_source_date) == ("plat_first_home", date(2026, 1, 7))
    assert (r.plat_ref, r.plat_year, r.plat_homes, r.plat_first_home_pin) == ("73-876", 2023, 3, "08920112")
    assert pd.isna(r.sewer_distance_ft) and pd.isna(r.water_distance_ft)


def test_fallback_in_town_infill_plat(loaded):
    r = loaded[2].loc["05511524"]  # plat M25-160, Charlotte, no homes yet
    assert (r.plat_book, r.plat_lots, r.municipality, r.in_municipal_limit) == (25, 4, "CHARLOTTE", True)
    assert pd.isna(r.plat_year) and r.plat_median_lot_acres == pytest.approx(0.126, abs=0.001)
    assert (r.sewer_status, r.sewer_confidence, r.sewer_source) == ("served", "low", "plat_age_fallback")
    assert r.sewer_source_date == OWNERS_DATE
    assert (r.water_status, r.water_source) == ("served", "plat_age_fallback")


def test_fallback_skips_large_lot_rural_plat(loaded):
    r = loaded[2].loc["23114109"]  # plat M21-428, unincorporated, ~3.3 ac lots
    assert (r.plat_book, r.in_municipal_limit) == (21, False)
    assert r.plat_median_lot_acres == pytest.approx(3.35, abs=0.01)
    assert r.sewer_status == "unknown" and pd.isna(r.sewer_source) and pd.isna(r.sewer_confidence)


def test_fallback_needs_small_lots_even_in_town(loaded):
    r = loaded[2].loc["13533311"]  # plat M25-763 inside Mint Hill, ~1.4 ac lots
    assert (r.municipality, r.in_municipal_limit) == ("MINT HILL", True)
    assert r.plat_median_lot_acres > CFG["plat_fallback"]["max_median_lot_acres"]
    assert r.sewer_status == "unknown"


def test_new_plats_from_your_addresses_use_fallback(loaded):
    u = loaded[2]
    for pin, ref in (("03141608", "69-548"), ("04104279", "76-221")):  # Tom Sadler, Auten Ave
        r = u.loc[pin]
        assert (r.plat_ref, r.sewer_status, r.sewer_source) == (ref, "served", "plat_age_fallback")
        assert pd.isna(r.plat_year)


def test_old_house_on_recent_plat_stays_unknown(loaded):
    r = loaded[2].loc["00102115"]  # plat has a 1940 house, so the fallback doesn't apply either
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
    assert (r.sewer_status, r.sewer_source, r.plat_year) == ("served", "plat_first_home", 2006)
    assert r.planned_project_nearby and r.planned_project_id == "DNC-40-19-004"
    assert r.planned_project_name == "Sanitary Sewer extension for Sam Furr Road"
    assert (r.planned_project_type, r.planned_project_phase) == ("Sewer", "Active")
    assert r.planned_project_distance_ft == pytest.approx(317, abs=1)
    # completed sewer projects nearby are never "planned"
    assert set(u.loc[u["planned_project_nearby"], "planned_project_id"]) == {"DNC-40-19-004"}


def test_report_counts(loaded):
    r = quality_report(loaded[0], S)
    sources = dict(r["by_source"])
    assert sum(sources.values()) == 29
    assert sources["plat_age_fallback"] >= 3 and sources["plat_first_home"] >= 2
    statuses = {(s, c) for s, c, _ in r["sewer_by_status_confidence"]}
    assert statuses <= {("served", "low"), ("unknown", "-")}


def test_rerun_stores_no_changes(loaded):
    engine = loaded[0]
    assert assign_parcels(engine, AS_OF, CFG, S) == {"added": 0, "changed": 0, "removed": 0, "current": 29}
