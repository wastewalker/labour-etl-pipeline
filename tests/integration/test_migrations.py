"""Migrations against a real database."""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from labour_etl.db.migrate import run_migrations

Connection = psycopg.Connection[dict[str, object]]


def test_applies_every_migration_in_order(conn: Connection) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM schema_migrations ORDER BY name")
        applied = [str(row["name"]) for row in cur.fetchall()]

    # Filenames are NNN_description.sql precisely so that sorting by name is
    # sorting by intended order.
    assert applied == ["001_initial_schema.sql", "002_reconciliation_view.sql"]


def test_a_second_run_applies_nothing(conn: Connection) -> None:
    # Migrations run at the start of every pipeline run. If they were not
    # idempotent, the second run of the day would crash on an existing table.
    result = run_migrations(conn)

    assert result.applied == ()
    assert "001_initial_schema.sql" in result.skipped


def test_a_failing_migration_leaves_no_trace(conn: Connection, tmp_path: Path) -> None:
    # One transaction per file: the table created by the first statement must
    # not survive the second statement failing, and the ledger must not claim
    # the migration succeeded.
    (tmp_path / "900_broken.sql").write_text(
        "CREATE TABLE half_built (id INT);\nSELECT * FROM table_that_does_not_exist;\n",
        encoding="utf-8",
    )

    with pytest.raises(psycopg.errors.UndefinedTable):
        run_migrations(conn, directory=tmp_path)

    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('half_built') AS present")
        row = cur.fetchone()
        assert row is not None
        assert row["present"] is None

        cur.execute("SELECT count(*) AS total FROM schema_migrations WHERE name = '900_broken.sql'")
        row = cur.fetchone()
        assert row is not None
        assert row["total"] == 0


def test_the_reconciliation_view_exists(conn: Connection) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('observation_reconciliation') AS present")
        row = cur.fetchone()

    assert row is not None
    assert row["present"] is not None
