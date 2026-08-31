"""A real PostgreSQL for the integration suite.

Testcontainers by default, which is what CI uses. ``TEST_DATABASE_URL`` points
the suite at an already-running instance instead - useful on a machine with no
working Docker daemon, and the reason these tests can be run rather than skipped
there. Everything is truncated between tests, so never aim it at a database you
care about.

A real database rather than a mock, because what is under test here is precisely
what a mock would have to fake: transactional rollback, constraint violations,
``ON CONFLICT`` upsert semantics and NUMERIC round-tripping. A fake that got all
of those right would be a database.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import psycopg
import pytest

from labour_etl.config import Config
from labour_etl.db.connection import connect
from labour_etl.db.migrate import run_migrations

# Pinned. A floating tag would make the suite's result depend on the day it ran.
POSTGRES_IMAGE = "postgres:16.4-alpine"

Connection = psycopg.Connection[dict[str, object]]


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    external = os.environ.get("TEST_DATABASE_URL")
    if external:
        yield external
        return

    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer(
        POSTGRES_IMAGE, username="labour", password="labour", dbname="labour_etl_test"
    )
    container.start()
    try:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(5432)
        yield f"postgres://labour:labour@{host}:{port}/labour_etl_test"
    finally:
        container.stop()


@pytest.fixture(scope="session")
def migrated_url(database_url: str) -> str:
    """Apply migrations once for the whole session.

    Running the real migrator rather than a hand-written schema fixture: this is
    the only place the migrations are executed before production does it, so a
    broken one has to fail here.
    """
    with connect(database_url) as conn:
        run_migrations(conn)
    return database_url


@pytest.fixture
def conn(migrated_url: str) -> Iterator[Connection]:
    """A clean connection with an empty database."""
    with connect(migrated_url) as connection:
        with connection.cursor() as cur:
            # CASCADE follows the foreign keys into observations and source_runs.
            cur.execute("TRUNCATE sources, pipeline_runs RESTART IDENTITY CASCADE")
        connection.commit()
        yield connection


@pytest.fixture
def config() -> Config:
    return Config(
        database_url="postgres://unused-by-these-tests",
        country_filter=frozenset({"BOL", "PER", "CHL"}),
        min_year=2018,
        http_max_retries=0,
    )
