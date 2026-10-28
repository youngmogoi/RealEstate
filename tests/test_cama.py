"""Tests for ingest/cama.py against real CAMA rows (tests/fixtures/meck_cama/cama.csv).

Expected values were read by hand from the county download of 2026-10-28.
"""

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import text

from ingest.cama import build_accounts, read_cama
from ingest.county_parcels import load_table
from ingest.db import get_engine

FIXTURE = Path(__file__).parent / "fixtures" / "meck_cama" / "cama.csv"


@pytest.fixture(scope="module")
def built():
    return build_accounts(read_cama(FIXTURE))


def acct(df, account_pin):
    rows = df[df["account_pin"] == account_pin]
    assert len(rows) == 1
    return rows.iloc[0]


def test_report(built):
    _, r = built
    # 25 rows; 05101114 repeats 7 of its rows exactly
    assert (r["rows"], r["exact_duplicate_rows_dropped"], r["accounts"]) == (25, 7, 6)
    assert r["accounts_inconsistent_across_rows"] == 0
    assert r["tax_years"] == {"2026": 5, "2027": 1}


def test_two_houses_on_one_account_12708101(built):
    a = acct(built[0], "12708101")
    # rows: 1932 house 2,171 sf finished and 2017 house 918 sf; heatedarea 3,089 on both rows
    assert a["building_count"] == 2 and a["heated_sf"] == 3089
    assert (a["year_built"], a["year_built_newest"], a["year_built_oldest"]) == (1932, 2017, 1932)   # largest is 1932
    assert a["finished_sf"] == 2171 + 918 and a["total_value"] == 1098400


def test_buildings_differing_only_in_area_count_twice_00102115(built):
    a = acct(built[0], "00102115")
    assert a["building_count"] == 2 and a["finished_sf"] == 938 + 768 and a["municipality"] == "HUNTERSVILLE"


def test_repeated_rows_count_once_05101114(built):
    a = acct(built[0], "05101114")
    assert a["building_count"] == 11
    assert a["bldg_types"] == "APARTMENT-GARDEN <=3 STORIES; APARTMENT-TOWNHOUSE; CLUB-LODGE"
    assert a["heated_sf"] == 17148 and a["finished_sf"] == 119963   # heated is not the account total here


def test_duplex_and_accounts_without_buildings(built):
    df = built[0]
    d = acct(df, "12704403")
    assert (d["bldg_type"], d["heated_sf"], d["year_built"], d["res_units"]) == ("DUPLEX-TRIPLEX-QUADRAPLEX", 2080, 1931, 2)
    new = acct(df, "02968182")
    assert new["tax_year"] == 2027 and new["building_count"] == 0 and new["heated_sf"] != new["heated_sf"]   # NaN
    mf = acct(df, "19923706B")
    assert mf["pin"] == "19923706" and mf["building_count"] == 0 and mf["heated_sf"] == 0


def test_store_in_parcel_cama(built):
    s = "test_cama"
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {s} CASCADE; CREATE SCHEMA {s}"))
    try:
        df = built[0]
        assert load_table(df, "parcel_cama", ["account_pin"], engine, date(2026, 10, 28), s)["added"] == 6
        assert load_table(df, "parcel_cama", ["account_pin"], engine, date(2026, 11, 4), s) == {
            "added": 0, "changed": 0, "removed": 0, "current": 6}
        with engine.connect() as conn:
            assert conn.execute(text(f"SELECT year_built_newest FROM {s}.parcel_cama WHERE account_pin = '12708101'")).scalar_one() == 2017
    finally:
        with engine.begin() as conn:
            conn.execute(text(f"DROP SCHEMA {s} CASCADE"))
