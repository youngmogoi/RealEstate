from sqlalchemy import text

from ingest.db import get_engine


def test_postgis_is_available():
    with get_engine().connect() as conn:
        version = conn.execute(text("SELECT PostGIS_Version()")).scalar_one()
    assert version.startswith("3.")
