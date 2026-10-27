import os
from collections.abc import Callable

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import Connection, Engine, create_engine, text


def database_url() -> str:
    load_dotenv()
    return (
        f"postgresql+psycopg://{os.environ['POSTGRES_USER']}:{os.environ['POSTGRES_PASSWORD']}"
        f"@{os.environ.get('POSTGRES_HOST', 'localhost')}:{os.environ.get('POSTGRES_PORT', '5433')}"
        f"/{os.environ['POSTGRES_DB']}"
    )


def get_engine() -> Engine:
    return create_engine(database_url())


def _sql_type(dtype) -> str:
    if pd.api.types.is_bool_dtype(dtype):
        return "boolean"
    if pd.api.types.is_integer_dtype(dtype):
        return "bigint"
    if pd.api.types.is_float_dtype(dtype):
        return "double precision"
    if pd.api.types.is_datetime64_any_dtype(dtype):
        return "date"
    return "text"


def copy_dataframe(
    conn: Connection,
    df: pd.DataFrame,
    table: str,
    types: dict[str, str] | None = None,
    prepare: Callable[[pd.DataFrame], pd.DataFrame] | None = None,
    chunk_rows: int = 50_000,
    append: bool = False,
) -> None:
    """(Re)create `table` as an unlogged staging table and bulk-load `df` with COPY.

    `prepare` runs on each chunk before writing (e.g. geometry to WKB, adding a hash), so
    large derived columns never exist for the whole frame at once. `append` adds to an
    existing table instead of recreating it.
    Blank strings must already be nulls: an empty CSV field loads as NULL.
    Dates load as dates (no time). `types` overrides the SQL type per column.
    """
    types, prepare = types or {}, prepare or (lambda c: c)
    if not append:
        sample = prepare(df.head(1))
        cols = ", ".join(f"{c} {types.get(c) or _sql_type(sample[c].dtype)}" for c in sample.columns)
        conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
        conn.execute(text(f"CREATE UNLOGGED TABLE {table} ({cols})"))
    raw = conn.connection.driver_connection
    with raw.cursor() as cur, cur.copy(f"COPY {table} FROM STDIN WITH (FORMAT csv, NULL '')") as copy:
        for start in range(0, len(df), chunk_rows):
            chunk = prepare(df.iloc[start:start + chunk_rows])
            copy.write(chunk.to_csv(index=False, header=False, date_format="%Y-%m-%d"))
