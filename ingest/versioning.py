"""Current tables with full history, storing only what changed.

`<table>` holds the current version of each key, with `valid_from`: the as-of date of the
load where the row first appeared in this exact form. When a later load changes or drops a
key, the old row moves to `<table>_history` with `valid_to` set to that load's as-of date.
History rows are never edited.

The table as it stood on date D:

    SELECT * FROM t WHERE valid_from <= D
    UNION ALL
    SELECT <same columns> FROM t_history WHERE valid_from <= D AND valid_to > D

A column that first appears in a load is added to both tables. Rows whose other columns did
not change get the new column filled in place (it was not tracked before, so there is no
older value to keep); history rows keep it NULL. Dropping a column is refused.
"""

from datetime import date

import pandas as pd
from sqlalchemy import Connection, text


def row_hash(df: pd.DataFrame) -> pd.Series:
    """64-bit hash of every column, for spotting changed rows. Pass geometry as WKB hex."""
    hashes = pd.util.hash_pandas_object(df, index=False, categorize=False).to_numpy(dtype="uint64")
    return pd.Series(hashes.view("int64"), index=df.index)


def create_staging_from_query(conn: Connection, staging: str, select_sql: str, params: dict | None = None) -> None:
    """Create `staging` from a SELECT, adding row_hash in the same statement.

    The hash must not be added with a later UPDATE: an index built in the same transaction
    after an UPDATE can't be used by that transaction, and apply_versioned then scans the
    whole staging table once per row.
    """
    conn.execute(text(f"DROP TABLE IF EXISTS {staging}"))
    conn.execute(text(
        f"CREATE UNLOGGED TABLE {staging} AS SELECT t.*, "
        f"('x' || substr(md5(t::text), 1, 16))::bit(64)::bigint AS row_hash FROM ({select_sql}) t"
    ), params or {})


def apply_versioned(conn: Connection, table: str, key: list[str], as_of: date) -> dict:
    """Merge `<table>_staging` (which must have a `row_hash` column) into `<table>`.

    Unchanged rows stay put. Changed and removed rows move to history. Returns counts.
    """
    staging, history = f"{table}_staging", f"{table}_history"
    keycols = ", ".join(key)
    match = " AND ".join(f"c.{k} = s.{k}" for k in key)

    dupes = conn.execute(text(
        f"SELECT count(*) FROM (SELECT 1 FROM {staging} GROUP BY {keycols} HAVING count(*) > 1) d"
    )).scalar_one()
    if dupes:
        raise ValueError(f"{staging} has {dupes} duplicate keys on ({keycols})")
    conn.execute(text(f"CREATE INDEX ON {staging} ({keycols})"))
    conn.execute(text(f"ANALYZE {staging}"))

    if not conn.execute(text("SELECT to_regclass(:t) IS NOT NULL"), {"t": table}).scalar_one():
        conn.execute(text(f"CREATE TABLE {table} (LIKE {staging})"))
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN valid_from date NOT NULL"))
        conn.execute(text(f"CREATE TABLE {history} (LIKE {table})"))
        conn.execute(text(f"ALTER TABLE {history} ADD COLUMN valid_to date NOT NULL"))
        conn.execute(text(f"CREATE UNIQUE INDEX ON {table} ({keycols})"))
        conn.execute(text(f"CREATE INDEX ON {history} ({keycols}, valid_from)"))

    cols = _columns(conn, staging)
    added = _add_new_columns(conn, table, history, staging, cols, match)

    in_staging = f"EXISTS (SELECT 1 FROM {staging} s WHERE {match})"
    unchanged = f"EXISTS (SELECT 1 FROM {staging} s WHERE {match} AND s.row_hash = c.row_hash)"
    removed, changed = conn.execute(text(
        f"SELECT count(*) FILTER (WHERE NOT {in_staging}), "
        f"count(*) FILTER (WHERE {in_staging} AND NOT {unchanged}) FROM {table} c"
    )).one()

    params = {"d": as_of}
    names = ", ".join(cols)
    conn.execute(text(
        f"INSERT INTO {history} ({names}, valid_from, valid_to) "
        f"SELECT {', '.join(f'c.{x}' for x in cols)}, c.valid_from, :d FROM {table} c WHERE NOT {unchanged}"
    ), params)
    conn.execute(text(f"DELETE FROM {table} c WHERE NOT {unchanged}"))
    inserted = conn.execute(text(
        f"INSERT INTO {table} ({names}, valid_from) SELECT {', '.join(f's.{x}' for x in cols)}, :d FROM {staging} s "
        f"WHERE NOT EXISTS (SELECT 1 FROM {table} c WHERE {match})"
    ), params).rowcount
    conn.execute(text(f"DROP TABLE {staging}"))

    current = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
    out = {"added": inserted - changed, "changed": changed, "removed": removed, "current": current}
    return out | ({"columns_added": added} if added else {})


def _columns(conn: Connection, table: str) -> list[str]:
    return [r[0] for r in conn.execute(text(
        "SELECT attname FROM pg_attribute WHERE attrelid = CAST(:t AS regclass) AND attnum > 0 "
        "AND NOT attisdropped ORDER BY attnum"), {"t": table}).all()]


def _add_new_columns(conn: Connection, table: str, history: str, staging: str, cols: list[str],
                     match: str) -> list[str]:
    """Add staging columns missing from `table` (and history); fill them on otherwise-unchanged rows."""
    have = set(_columns(conn, table)) - {"valid_from"}
    dropped = sorted(have - set(cols))
    if dropped:
        raise ValueError(f"{staging} lacks columns {dropped} that {table} has; drop them by hand if intended")
    new = [c for c in cols if c not in have]
    if not new:
        return []
    types = dict(conn.execute(text(
        "SELECT attname, format_type(atttypid, atttypmod) FROM pg_attribute "
        "WHERE attrelid = CAST(:t AS regclass) AND attnum > 0 AND NOT attisdropped"), {"t": staging}).all())
    for c in new:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {c} {types[c]}"))
        conn.execute(text(f"ALTER TABLE {history} ADD COLUMN {c} {types[c]}"))
    old = [c for c in cols if c in have and c != "row_hash"]
    same = " AND ".join(f"c.{c} IS NOT DISTINCT FROM s.{c}" for c in old)
    conn.execute(text(
        f"UPDATE {table} c SET {', '.join(f'{x} = s.{x}' for x in new)}, row_hash = s.row_hash "
        f"FROM {staging} s WHERE {match} AND {same}"))
    return new
