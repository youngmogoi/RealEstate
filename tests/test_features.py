"""Tests for features/build.py and features/changes.py on real Mecklenburg rows.

- Owner types: real owner names from the county ownership file (2026-10-26).
- Changes: the county fixture (tests/fixtures/meck_county/) loaded as a first snapshot, then a
  second snapshot a week later with one change of each kind.
- Frontage: real parcels and street centerlines (tests/fixtures/meck_features/), with expected
  values read from the full 2026-10-27 build.
"""

from datetime import date
from pathlib import Path

import pandas as pd
import pyogrio
import pytest
import shapely
import yaml
from sqlalchemy import text

from features.build import ROOT, build_frontage, owner_type
from features.changes import detect
from ingest.county_parcels import load
from ingest.db import copy_dataframe, get_engine
from ingest.streets import load as load_streets
from ingest.versioning import apply_versioned, row_hash
from tests.test_county_parcels import read_fixture

CFG = yaml.safe_load((ROOT / "config" / "features.yaml").read_text())
OUTSIDE = yaml.safe_load((ROOT / "config" / "ingest.yaml").read_text())["utilities"]["plat_fallback"]["outside_municipality"]
D1, D2 = date(2026, 10, 26), date(2026, 11, 2)


@pytest.mark.parametrize("name, expected", [
    ("CITY OF CHARLOTTE", "government"),
    ("MECKLENBURG COUNTY", "government"),
    ("10 WEST REAL ESTATE LLC", "llc_corporate"),          # "ESTATE" inside REAL ESTATE is not an estate
    ("123 S CHURCH ST LLC", "llc_corporate"),              # a company named after Church St
    ("BELTON EMMA JACKSON HEIRS", "estate_heirs"),
    ("BLACK CLINT M (HEIRS)", "estate_heirs"),
    ("ADAMS METROPOLITAN AFRICAN METH EPISC CH TR", "church_nonprofit"),  # church trustees
    ("CHRISTOPHER P HOWARD TRUST", "trust"),
    ("RUTH B GOINES REVOCABLE TRUST", "trust"),
    ("AUTEN WOODS PROPERTY OWNERS ASSOCIATION INC", "church_nonprofit"),
    ("PINE GROVE RESIDENTIAL FUNDING I LLC", "llc_corporate"),
    ("MCH SFR NC OWNER 1 LP", "llc_corporate"),
    ("DUKE POWER CO", "llc_corporate"),
    ("KELLER GRANT", "individual"),
    ("LOPEZ-FERREIRA RAMON ANTONIO", "individual"),
    (None, None),
])
def test_owner_type(name, expected):
    assert owner_type(name, CFG["owner_type_rules"]) == expected


def _zoning(engine, schema, rows, as_of):
    df = pd.DataFrame(rows, columns=["pin", "zone_code"])
    with engine.begin() as conn:
        copy_dataframe(conn, df.assign(row_hash=row_hash(df)), f"{schema}.parcel_zoning_staging")
        apply_versioned(conn, f"{schema}.parcel_zoning", ["pin"], as_of)


@pytest.fixture
def two_snapshots():
    s = "test_changes"
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {s} CASCADE; CREATE SCHEMA {s}"))
    parcels, owners, sales, _ = read_fixture()
    load(parcels, owners, sales, engine, D1, {}, {}, schema=s)
    _zoning(engine, s, [("08920128", "N1-B"), ("03141608", "N1-B")], D1)

    p2 = parcels[parcels["pin"] != "00101A01"].copy()                       # retired
    new = p2[p2["pin"] == "04104279"].copy().assign(pin="04104299")          # a new PIN (as after a split)
    p2 = pd.concat([p2, new], ignore_index=True)
    p2.loc[p2["pin"] == "08920128", "owner_name"] = "NEW OWNER LLC"          # owner change
    p2.loc[p2["pin"] == "03141608", "total_value"] *= 1.25                   # +25%: a value change
    p2.loc[p2["pin"] == "04104212", "total_value"] *= 1.05                   # +5%: below the threshold
    s2 = pd.concat([sales, sales[sales["transfer_id"] == "1245970"].assign(transfer_id="9999999")], ignore_index=True)
    load(p2, owners, s2, engine, D2, {}, {}, schema=s)
    _zoning(engine, s, [("08920128", "N1-C"), ("03141608", "N1-B")], D2)
    yield engine, s
    with engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA {s} CASCADE"))


def test_change_detection(two_snapshots):
    engine, s = two_snapshots
    added = detect(engine, D1, D2, CFG["changes"]["value_change_pct"], zoning=(D1, D2), schema=s)
    assert added == {"new_pin": 1, "retired_pin": 1, "owner_change": 1, "value_change": 1, "new_sale": 1,
                     "zoning_change": 1}
    with engine.connect() as conn:
        rows = {r[0]: r[1:] for r in conn.execute(text(
            f"SELECT change_type, pin, old_value, new_value, prev_date, cur_date FROM {s}.changes")).all()}
    assert rows["new_pin"][0] == "04104299" and rows["retired_pin"][0] == "00101A01"
    assert rows["owner_change"] == ("08920128", "KELLER GRANT", "NEW OWNER LLC", D1, D2)
    assert rows["zoning_change"][1:3] == ("N1-B", "N1-C")
    assert rows["value_change"][0] == "03141608"
    # rerunning finds the same changes and records nothing new
    assert set(detect(engine, D1, D2, CFG["changes"]["value_change_pct"], zoning=(D1, D2), schema=s).values()) == {0}


FRONTAGE_FIXTURE = Path(__file__).parent / "fixtures" / "meck_features"


@pytest.fixture(scope="module")
def frontage():
    s = "test_frontage"
    engine = get_engine()
    parcels = pyogrio.read_dataframe(FRONTAGE_FIXTURE / "parcels.gpkg")
    streets = pyogrio.read_dataframe(FRONTAGE_FIXTURE / "streets.gpkg").rename_geometry("geom")
    streets["street_code"] = streets["street_code"].astype("Int64")
    with engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {s} CASCADE; CREATE SCHEMA {s}"))
        copy_dataframe(conn, pd.DataFrame({
            "pin": parcels["pin"],
            "geom": shapely.to_wkb(shapely.set_srid(parcels.geometry.values, 2264), hex=True, include_srid=True),
        }), f"{s}.parcels", {"geom": "geometry(MultiPolygon, 2264)"})
    load_streets(streets, engine, D1, schema=s)
    with engine.begin() as conn:
        build_frontage(conn, CFG, s)
        df = pd.read_sql(text(f"SELECT * FROM {s}.feature_frontage"), conn).set_index("pin")
    yield df
    with engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA {s} CASCADE"))


def test_mid_block_frontage_equals_lot_width(frontage):
    # side lot lines near the street are not counted, so frontage matches the lot's width
    for pin, ft in (("08920128", 52.0), ("03141608", 60.5)):  # 443 Dawn Cir (52 ft wide), 2137 Tom Sadler Rd (60 ft)
        r = frontage.loc[pin]
        assert r.frontage_ft == pytest.approx(ft, abs=0.5) and r.street_frontages == 1 and not r.corner


def test_corner_lot(frontage):
    r = frontage.loc["00103404"]  # 17119 Island View Dr, Cornelius
    assert r.corner and r.street_frontages == 3 and r.frontage_ft == pytest.approx(175.9, abs=0.5)


def test_through_lot_is_not_a_corner(frontage):
    r = frontage.loc["00103301"]  # 16815 Lake Shore Dr, Cornelius: streets on opposite sides
    assert r.street_frontages == 2 and not r.corner and r.frontage_ft == pytest.approx(167.5, abs=0.5)


def test_flag_lot_and_landlocked(frontage):
    flag = frontage.loc["00101429"]  # 14017 NC 73 Hwy: 30 ft of frontage on a 153 ft wide lot
    assert flag.frontage_ft == pytest.approx(30.4, abs=0.5)
    assert flag.frontage_ft < CFG["frontage"]["flag_lot_max_frontage_ft"]
    assert flag.frontage_ft < CFG["frontage"]["flag_lot_max_frontage_to_width"] * 152.8
    assert "00101325" not in frontage.index  # 14051 Island Dr: no street within reach, so no frontage row
