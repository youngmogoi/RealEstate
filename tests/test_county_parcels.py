"""Tests for ingest/county_parcels.py against real Mecklenburg rows (tests/fixtures/meck_county/).

Expected values were read by hand from the county downloads of 2026-10-26.
"""

from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import text

from ingest.county_parcels import (
    OWNER_FIELDS, SALES_FIELDS, build_parcels, clean_owners, clean_sales, format_report, load, load_sales,
    quality_report, read_boundaries, read_table,
)
from ingest.db import get_engine

FIXTURE = Path(__file__).parent / "fixtures" / "meck_county"
PLACEHOLDERS = ["1900-01-01", "1975-01-01"]
THRESHOLD = 0.20
AS_OF = date(2026, 10, 26)


def read_fixture():
    owners = clean_owners(
        read_table(FIXTURE / "owners.csv", list(OWNER_FIELDS) + ["secownerlastname", "secownerfirstname"]),
        PLACEHOLDERS, AS_OF,
    )
    sales, dupes = clean_sales(read_table(FIXTURE / "sales.csv", list(SALES_FIELDS)), PLACEHOLDERS, AS_OF)
    parcels = build_parcels(read_boundaries(FIXTURE / "boundaries.gpkg"), owners, THRESHOLD)
    return parcels, owners, sales, dupes


@pytest.fixture(scope="module")
def data():
    return read_fixture()


def one(df, **where):
    rows = df
    for col, val in where.items():
        rows = rows[rows[col] == val]
    assert len(rows) == 1
    return rows.iloc[0]


def test_dawn_cir_current_owner_and_sale(data):
    parcels, *_ = data
    r = one(parcels, pin="08920128")
    assert r["owner_name"] == "KELLER GRANT"
    assert r["site_address"] == "443 DAWN CR CHARLOTTE NC"
    assert r["land_value"] == 50000
    assert r["building_value"] == 341600
    assert r["last_sale_date"] == pd.Timestamp("2026-06-25")
    assert r["last_sale_price"] == 385000
    assert r["assessor_acres"] == pytest.approx(0.18024)
    assert r["account_count"] == 1


def test_tom_sadler_house_now_valued(data):
    parcels, *_ = data
    r = one(parcels, pin="03141608")
    assert r["owner_name"] == "BENAVIDES IVAN ALEJANDRO JUEZ"
    assert r["building_value"] == 266500
    assert r["last_sale_date"] == pd.Timestamp("2026-09-04")
    assert r["last_sale_price"] == 360000


def test_assessor_acres_by_land_unit(data):
    parcels, *_ = data
    assert pd.isna(one(parcels, pin="04104279")["assessor_acres"])  # LOT: a lot count, not acres
    assert one(parcels, pin="04104212")["assessor_acres"] == pytest.approx(33759 / 43560)  # SQUARE FEET
    assert one(parcels, pin="03141608")["assessor_acres"] == pytest.approx(0.26)  # ACRE


def test_condo_building_is_one_parcel_with_unit_accounts(data):
    parcels, owners, *_ = data
    r = one(parcels, pin="20909C99")
    units = owners[owners["pin"] == "20909C99"]
    assert r["account_count"] == 61 == len(units)
    assert r["total_value"] == units["total_value"].sum()
    assert pd.isna(r["owner_name"])  # many owners: owner fields stay on parcel_owners
    assert units["account_pin"].is_unique


def test_bad_sale_dates_are_nulled_and_flagged(data):
    _, owners, *_ = data
    future = one(owners, account_pin="05126204")  # 11/17/2026 in source
    assert pd.isna(future["last_sale_date"]) and future["last_sale_date_after_as_of"]
    assert one(owners, account_pin="12303C98")["last_sale_date_after_as_of"]  # 2/5/9997
    placeholder = one(owners, account_pin="00102326")  # 1/1/1975
    assert pd.isna(placeholder["last_sale_date"]) and placeholder["last_sale_date_placeholder"]


def test_parcel_without_owner_record(data):
    parcels, *_ = data
    r = one(parcels, pin="00101A01")
    assert not r["has_owner_record"]
    assert r["account_count"] == 0


def test_sales_history(data):
    _, _, sales, dupes = data
    assert len(sales) == 25 and dupes == {"exact_duplicates_dropped": 0, "duplicate_keys_dropped": 0}
    dawn = sales[sales["pin"] == "08920128"].sort_values("sale_date")
    assert len(dawn) == 7
    assert dawn.iloc[-1]["sale_price"] == 385000
    assert pd.isna(dawn.iloc[-1]["validity_code"])  # recent sales are not yet reviewed
    split = one(sales, transfer_id="1139182")
    assert split["validity_code"] == "AS" and split["seller"] == "KIZIAH RONALD RAY"


def test_exact_duplicate_sales_dropped():
    raw = read_table(FIXTURE / "sales.csv", list(SALES_FIELDS))
    sales, dupes = clean_sales(pd.concat([raw, raw.head(3)]), PLACEHOLDERS, AS_OF)
    assert len(sales) == 25 and dupes["exact_duplicates_dropped"] == 3


def test_quality_report(data):
    r = quality_report(*data[:3], data[3], THRESHOLD)
    assert r["parcels"] == 9
    assert r["parcels_without_owner_record"] == 1
    assert r["multi_account_parcels"] == 2
    assert r["owner_sale_dates_after_as_of"] == 2
    assert r["latest_sale_date"] == "2026-09-04"
    assert "changes since last load" in format_report(r, {"parcels": {"added": 9, "changed": 0, "removed": 0, "current": 9}})


@pytest.fixture
def test_schema():
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS test_county CASCADE; CREATE SCHEMA test_county"))
    yield engine
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA test_county CASCADE"))


def test_load_sales_streams_and_dedupes_in_sql(test_schema, tmp_path):
    raw = pd.read_csv(FIXTURE / "sales.csv", dtype=str, keep_default_na=False)
    pd.concat([raw, raw.head(3)]).to_csv(tmp_path / "sales.csv", index=False)

    changes, report = load_sales(tmp_path / "sales.csv", PLACEHOLDERS, AS_OF, test_schema, "test_county", chunk_rows=10)
    assert changes == {"added": 25, "changed": 0, "removed": 0, "current": 25}
    assert report["sales"] == 25
    assert report["sales_exact_duplicates_dropped"] == 3
    assert report["sales_duplicate_keys_dropped"] == 0
    assert report["latest_sale_date"] == "2026-09-04"

    in_memory, _ = clean_sales(read_table(FIXTURE / "sales.csv", list(SALES_FIELDS)), PLACEHOLDERS, AS_OF)
    with test_schema.connect() as conn:
        price = conn.execute(text(
            "SELECT sale_price FROM test_county.parcel_sales WHERE transfer_id = '1245970'"
        )).scalar_one()
    assert price == 385000 == in_memory.loc[in_memory["transfer_id"] == "1245970", "sale_price"].item()


def test_load_keeps_only_changes_and_rebuilds_history(test_schema):
    engine = test_schema
    parcels, owners, sales, dupes = read_fixture()
    report = quality_report(parcels, owners, sales, dupes, THRESHOLD)
    run = lambda p, o, s, d: load(p, o, s, engine, d, {}, report, schema="test_county")

    first = run(parcels, owners, sales, date(2026, 10, 26))
    assert first["parcels"] == {"added": 9, "changed": 0, "removed": 0, "current": 9}
    assert first["parcel_sales"]["current"] == 25

    assert run(parcels, owners, sales, date(2026, 10, 26))["parcels"]["changed"] == 0  # rerun: nothing new

    # Next week: Dawn Cir changes hands, one parcel disappears, one sale gets its validity code
    p2 = parcels[parcels["pin"] != "00101A01"].copy()
    p2.loc[p2["pin"] == "08920128", "owner_name"] = "NEW OWNER"
    s2 = sales.copy()
    s2.loc[s2["transfer_id"] == "1245970", "validity_code"] = "Q"
    second = run(p2, owners, s2, date(2026, 11, 2))
    assert second["parcels"] == {"added": 0, "changed": 1, "removed": 1, "current": 8}
    assert second["parcel_sales"]["changed"] == 1
    assert second["parcel_owners"]["changed"] == 0

    with engine.connect() as conn:
        q = lambda sql, **p: conn.execute(text(sql), p).all()
        assert q("SELECT count(*) FROM test_county.parcels_history") == [(2,)]
        as_of = (
            "SELECT owner_name FROM (SELECT pin, owner_name FROM test_county.parcels WHERE valid_from <= :d "
            "UNION ALL SELECT pin, owner_name FROM test_county.parcels_history "
            "WHERE valid_from <= :d AND valid_to > :d) t WHERE pin = :pin"
        )
        assert q(as_of, d=date(2026, 10, 30), pin="08920128") == [("KELLER GRANT",)]
        assert q(as_of, d=date(2026, 11, 2), pin="08920128") == [("NEW OWNER",)]
        assert q(as_of, d=date(2026, 10, 30), pin="00101A01") == [(None,)]  # existed then, no owner record
        assert q(as_of, d=date(2026, 11, 2), pin="00101A01") == []
        assert q("SELECT DISTINCT ST_SRID(geom), GeometryType(geom) FROM test_county.parcels") == [(2264, "MULTIPOLYGON")]
