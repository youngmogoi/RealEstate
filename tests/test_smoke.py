import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, text


def database_url() -> str:
    load_dotenv()
    return (
        f"postgresql+psycopg://{os.environ['POSTGRES_USER']}:{os.environ['POSTGRES_PASSWORD']}"
        f"@{os.environ.get('POSTGRES_HOST', 'localhost')}:{os.environ.get('POSTGRES_PORT', '5433')}"
        f"/{os.environ['POSTGRES_DB']}"
    )


def test_postgis_is_available():
    engine = create_engine(database_url())
    with engine.connect() as conn:
        version = conn.execute(text("SELECT PostGIS_Version()")).scalar_one()
    assert version.startswith("3.")
