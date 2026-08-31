"""Minimal forward-only migrator.

A migration framework is more machinery than this schema needs, and the
integration tests run migrations against a throwaway database on every run, so
the cost of the abstraction would be paid constantly. What is kept from the
real thing: an applied-migrations ledger, one transaction per file, and an
advisory lock so two processes starting at once cannot both apply the same file.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import psycopg

LOGGER = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# Any 64-bit constant works; it only has to be stable across processes.
ADVISORY_LOCK_KEY = 4_815_162_342


@dataclass(frozen=True, slots=True)
class MigrationResult:
    applied: tuple[str, ...]
    skipped: tuple[str, ...]


def _migration_files(directory: Path) -> list[Path]:
    # Files are named NNN_description.sql, so sorting by name is chronological.
    return sorted(directory.glob("*.sql"))


def run_migrations(
    conn: psycopg.Connection[dict[str, object]],
    directory: Path | None = None,
) -> MigrationResult:
    """Apply every migration not yet recorded in the ledger.

    Idempotent: running it against an up-to-date database applies nothing.
    """
    source_dir = MIGRATIONS_DIR if directory is None else directory

    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name        TEXT PRIMARY KEY,
                applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        conn.commit()

        cur.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
        conn.commit()

        try:
            cur.execute("SELECT name FROM schema_migrations")
            already_applied = {str(row["name"]) for row in cur.fetchall()}

            applied: list[str] = []
            skipped: list[str] = []

            for path in _migration_files(source_dir):
                if path.name in already_applied:
                    skipped.append(path.name)
                    continue

                sql = path.read_text(encoding="utf-8")

                # One transaction per file: a migration that fails halfway
                # leaves no partial schema behind, and no ledger row claiming
                # it succeeded.
                try:
                    cur.execute(sql)
                    cur.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
                    conn.commit()
                except Exception:
                    conn.rollback()
                    LOGGER.exception("Migration %s failed and was rolled back", path.name)
                    raise

                LOGGER.info("Applied migration %s", path.name)
                applied.append(path.name)

            return MigrationResult(applied=tuple(applied), skipped=tuple(skipped))
        finally:
            cur.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
            conn.commit()
