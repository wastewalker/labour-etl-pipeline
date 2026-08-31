"""Database connections.

A batch job that runs to completion needs one connection, not a pool. The
pipeline opens it, does its work, and closes it, so the connection lifetime is
the process lifetime and there is nothing to leak between runs.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal

import psycopg
from psycopg.rows import dict_row

# psycopg returns NUMERIC as Decimal, which is the correct representation and
# the reason not to change it globally: Decimal is exactly what a database
# column of arbitrary precision should hand back. The domain works in float
# because that is what the three sources publish, so the conversion happens
# explicitly at this boundary rather than through a global type adapter that
# would silently affect every NUMERIC in the schema.
def to_float(value: Decimal | float | int) -> float:
    """Convert a value read from a NUMERIC column into the domain's float."""
    return float(value)


@contextmanager
def connect(database_url: str) -> Iterator[psycopg.Connection[dict[str, object]]]:
    """Open a connection with dict rows and autocommit off.

    Autocommit stays off because every write in this pipeline belongs to an
    explicit transaction. Leaving it on would make the per-source rollback -
    the whole point of the design - impossible to express.
    """
    conn = psycopg.connect(database_url, row_factory=dict_row, autocommit=False)
    try:
        yield conn
    finally:
        conn.close()
