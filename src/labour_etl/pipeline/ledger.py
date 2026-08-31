"""The run ledger.

Every write here is committed immediately and on its own. That is not an
oversight - it is the whole reason the ledger is trustworthy. If a ledger entry
shared a transaction with the load it describes, then a failed load would roll
back the record of its own failure, and the table would only ever contain
successes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

import psycopg

from ..db.connection import as_int
from ..sources.base import Source

LOGGER = logging.getLogger(__name__)

RunStatus = Literal["running", "succeeded", "partial", "failed"]
SourceRunStatus = Literal["running", "succeeded", "failed"]

Connection = psycopg.Connection[dict[str, object]]


@dataclass(frozen=True, slots=True)
class SourceCounts:
    extracted: int = 0
    loaded: int = 0
    rejected: int = 0
    skipped: int = 0


def upsert_sources(conn: Connection, sources: tuple[Source, ...]) -> None:
    """Register each source's metadata before any observation references it.

    Code is the single source of truth for what a source is; this reflects that
    into the database so ``observations.source_key`` has something to point at.
    """
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO sources (key, name, url, kind)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (key) DO UPDATE
               SET name = EXCLUDED.name,
                   url = EXCLUDED.url,
                   kind = EXCLUDED.kind,
                   updated_at = now()
            """,
            [(s.key, s.name, s.url, s.kind) for s in sources],
        )
    conn.commit()


def start_run(conn: Connection, triggered_by: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pipeline_runs (status, triggered_by)
            VALUES ('running', %s)
            RETURNING id
            """,
            (triggered_by,),
        )
        row = cur.fetchone()
    conn.commit()

    assert row is not None  # INSERT ... RETURNING yields a row or raises
    return as_int(row["id"])


def start_source_run(conn: Connection, run_id: int, source_key: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO source_runs (run_id, source_key, status)
            VALUES (%s, %s, 'running')
            RETURNING id
            """,
            (run_id, source_key),
        )
        row = cur.fetchone()
    conn.commit()

    assert row is not None
    return as_int(row["id"])


def finish_source_run(
    conn: Connection,
    source_run_id: int,
    status: SourceRunStatus,
    counts: SourceCounts,
    error_message: str | None = None,
) -> None:
    """Close out one source's entry.

    A failed source must carry a reason and a successful one must not; the
    schema enforces that with a CHECK, so a mistake here fails loudly at the
    moment it happens rather than leaving an unexplained failure in the ledger.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE source_runs
               SET status = %s,
                   finished_at = now(),
                   rows_extracted = %s,
                   rows_loaded = %s,
                   rows_rejected = %s,
                   rows_skipped = %s,
                   error_message = %s
             WHERE id = %s
            """,
            (
                status,
                counts.extracted,
                counts.loaded,
                counts.rejected,
                counts.skipped,
                error_message,
                source_run_id,
            ),
        )
    conn.commit()


def finish_run(conn: Connection, run_id: int, status: RunStatus) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE pipeline_runs SET status = %s, finished_at = now() WHERE id = %s",
            (status, run_id),
        )
    conn.commit()
