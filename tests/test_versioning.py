"""Tests for ingest/versioning.py: change-only history, and columns added after the first load."""

from datetime import date

import pandas as pd
import pytest
from sqlalchemy import text

from ingest.db import copy_dataframe, get_engine
from ingest.versioning import apply_versioned, row_hash

S = "test_versioning"
D1, D2, D3 = date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1)


@pytest.fixture
def engine():
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {S} CASCADE; CREATE SCHEMA {S}"))
    yield engine
    with engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA {S} CASCADE"))


def _load(engine, df, as_of):
    with engine.begin() as conn:
        copy_dataframe(conn, df.assign(row_hash=row_hash(df)), f"{S}.t_staging")
        return apply_versioned(conn, f"{S}.t", ["pin"], as_of)


def _rows(engine, table):
    with engine.connect() as conn:
        return pd.read_sql(text(f"SELECT * FROM {S}.{table} ORDER BY pin"), conn)


def test_new_column_filled_in_place(engine):
    _load(engine, pd.DataFrame({"pin": ["A", "B"], "zone": ["N1-A", "N1-B"]}), D1)
    # place_type appears; B's zone also changes in the same load
    c = _load(engine, pd.DataFrame({"pin": ["A", "B"], "zone": ["N1-A", "N1-C"],
                                    "place_type": ["Neighborhood 1", "Neighborhood 2"]}), D2)
    assert c == {"added": 0, "changed": 1, "removed": 0, "current": 2, "columns_added": ["place_type"]}
    cur = _rows(engine, "t")
    assert cur["place_type"].tolist() == ["Neighborhood 1", "Neighborhood 2"]
    assert cur["valid_from"].tolist() == [D1, D2]          # A unchanged: keeps its date
    hist = _rows(engine, "t_history")
    assert hist[["pin", "zone"]].values.tolist() == [["B", "N1-B"]] and hist["place_type"].isna().all()
    # a rerun with the same data changes nothing
    c = _load(engine, pd.DataFrame({"pin": ["A", "B"], "zone": ["N1-A", "N1-C"],
                                    "place_type": ["Neighborhood 1", "Neighborhood 2"]}), D3)
    assert c == {"added": 0, "changed": 0, "removed": 0, "current": 2}


def test_dropped_column_refused(engine):
    _load(engine, pd.DataFrame({"pin": ["A"], "zone": ["N1-A"], "place_type": ["Neighborhood 1"]}), D1)
    with pytest.raises(ValueError, match="place_type"):
        _load(engine, pd.DataFrame({"pin": ["A"], "zone": ["N1-A"]}), D2)
