"""Writing observations into the database.

The load for one source is a single transaction, and the caller is responsible
for committing or rolling it back. That is what makes "a source either lands
completely or not at all" true rather than aspirational: there is no point at
which half of a source's rows are visible to a reader.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import psycopg

from ..db.connection import as_int
from ..domain.records import Observation

LOGGER = logging.getLogger(__name__)

Connection = psycopg.Connection[dict[str, object]]

# The upsert is what makes re-running safe. The natural key is the conflict
# target, so a source revising a figure updates the existing row instead of
# adding a second one.
#
# The WHERE clause on DO UPDATE is the part worth reading twice: without it,
# every run would rewrite every row and stamp updated_at with the run time,
# which would make the column mean "when we last looked" instead of "when this
# figure last changed". The second is the only one worth storing.
_UPSERT = """
INSERT INTO observations (source_key, country_iso3, year, indicator_code, value)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (source_key, country_iso3, year, indicator_code) DO UPDATE
   SET value = EXCLUDED.value,
       updated_at = now()
 WHERE observations.value IS DISTINCT FROM EXCLUDED.value
"""


@dataclass(frozen=True, slots=True)
class LoadResult:
    #: Rows presented to the database and accepted.
    written: int
    #: Rows whose value actually differed from what was already stored.
    changed: int


def load_observations(conn: Connection, observations: tuple[Observation, ...]) -> LoadResult:
    """Upsert a source's observations. Does not commit.

    Raises whatever the database raises: a constraint violation here means the
    caller must roll the whole source back, and swallowing it would defeat that.
    """
    if not observations:
        return LoadResult(written=0, changed=0)

    rows = [
        (
            observation.source_key,
            observation.country_iso3,
            observation.year,
            observation.indicator_code,
            observation.value,
        )
        for observation in observations
    ]

    with conn.cursor() as cur:
        cur.executemany(_UPSERT, rows)
        # With the conditional DO UPDATE, an unchanged row affects nothing, so
        # rowcount is the count of rows that genuinely moved. A re-run over
        # unchanged data reports zero, which is exactly what idempotence looks
        # like from the outside.
        changed = max(cur.rowcount, 0)

    return LoadResult(written=len(rows), changed=changed)


def count_observations(conn: Connection, source_key: str | None = None) -> int:
    """Rows currently stored, optionally for one source. Used by the tests."""
    with conn.cursor() as cur:
        if source_key is None:
            cur.execute("SELECT count(*) AS total FROM observations")
        else:
            cur.execute(
                "SELECT count(*) AS total FROM observations WHERE source_key = %s",
                (source_key,),
            )
        row = cur.fetchone()

    assert row is not None
    return as_int(row["total"])
