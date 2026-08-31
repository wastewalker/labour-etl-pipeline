"""The pipeline against a real database.

The tests that matter most are the failure ones. Anyone can load three sources
on a good day; what this pipeline claims is that a bad day leaves the database
consistent and the ledger honest, and that claim is only worth as much as the
tests that deliberately break it.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from labour_etl.config import Config
from labour_etl.db.connection import to_float
from labour_etl.domain.errors import RecordRejected, SourceUnavailable
from labour_etl.domain.records import ExtractionResult, Observation
from labour_etl.pipeline.loader import count_observations
from labour_etl.pipeline.runner import run_pipeline
from labour_etl.sources.base import Source, SourceKind

Connection = psycopg.Connection[dict[str, object]]


class FakeSource(Source):
    """A source with a scripted outcome.

    The real extractors are covered by the unit suite against saved fixtures.
    What these tests need is precise control over *what a source returns*, so
    that the loading and failure-handling behaviour can be pinned down without
    depending on what a public website happens to publish today.
    """

    kind: SourceKind = "rest_api"

    def __init__(
        self,
        key: str,
        result: ExtractionResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.key = key
        self.name = f"Fake source {key}"
        self.url = f"https://example.test/{key}"
        self._result = result or ExtractionResult(observations=(), rejections=())
        self._error = error
        self.extract_calls = 0

    def extract(self, config: Config) -> ExtractionResult:
        self.extract_calls += 1
        if self._error is not None:
            raise self._error
        return self._result


def observation(
    source_key: str, iso3: str = "BOL", year: int = 2024, value: float = 7.4
) -> Observation:
    return Observation.create(source_key=source_key, country_iso3=iso3, year=year, value=value)


def poisoned(source_key: str) -> Observation:
    """An observation the database will refuse.

    Built by calling the dataclass directly instead of ``Observation.create``,
    which is the only way to get one: the domain validation would reject 999
    long before it reached SQL. That is the point - this simulates a value that
    got past the application, and proves the CHECK constraint is a real backstop
    rather than decoration.
    """
    return Observation(source_key=source_key, country_iso3="PER", year=2024, value=999.0)


def source_runs(conn: Connection, run_id: int) -> dict[str, dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT source_key, status, rows_loaded, rows_rejected, rows_skipped, error_message
              FROM source_runs WHERE run_id = %s
            """,
            (run_id,),
        )
        return {str(row["source_key"]): dict(row) for row in cur.fetchall()}


class TestFailureIsolation:
    def test_a_failure_mid_load_rolls_that_source_back_completely(
        self, conn: Connection, config: Config
    ) -> None:
        # Two good rows and one the database refuses, in that order. Without a
        # transaction the first two would already be committed by the time the
        # third one blew up, leaving a half-loaded source that looks fine.
        broken = FakeSource(
            "broken",
            ExtractionResult(
                observations=(
                    observation("broken", "BOL"),
                    observation("broken", "CHL"),
                    poisoned("broken"),
                ),
                rejections=(),
            ),
        )

        summary = run_pipeline(conn, config, (broken,))

        assert count_observations(conn, "broken") == 0
        assert summary.status == "failed"

        entry = source_runs(conn, summary.run_id)["broken"]
        assert entry["status"] == "failed"
        assert "rolled back" in str(entry["error_message"])

    def test_one_broken_source_does_not_stop_the_others(
        self, conn: Connection, config: Config
    ) -> None:
        # Good, broken, good - so the test proves both that a later source is
        # still attempted and that an earlier one's data survives.
        first = FakeSource("first", ExtractionResult((observation("first"),), ()))
        broken = FakeSource(
            "broken", ExtractionResult((observation("broken"), poisoned("broken")), ())
        )
        last = FakeSource("last", ExtractionResult((observation("last"),), ()))

        summary = run_pipeline(conn, config, (first, broken, last))

        assert summary.status == "partial"
        assert summary.failed_sources == ("broken",)
        assert count_observations(conn, "first") == 1
        assert count_observations(conn, "broken") == 0
        assert count_observations(conn, "last") == 1
        # The source after the failure was still asked for its data.
        assert last.extract_calls == 1

    def test_an_unreachable_source_is_recorded_and_contained(
        self, conn: Connection, config: Config
    ) -> None:
        good = FakeSource("good", ExtractionResult((observation("good"),), ()))
        down = FakeSource("down", error=SourceUnavailable("down", "HTTP 503"))

        summary = run_pipeline(conn, config, (good, down))

        assert summary.status == "partial"
        assert count_observations(conn) == 1
        assert source_runs(conn, summary.run_id)["down"]["error_message"] == "HTTP 503"

    def test_an_unexpected_exception_in_a_source_is_contained(
        self, conn: Connection, config: Config
    ) -> None:
        # A source is third-party-shaped code over third-party-shaped data. A
        # bug in one must not become an outage for the other two.
        good = FakeSource("good", ExtractionResult((observation("good"),), ()))
        buggy = FakeSource("buggy", error=ZeroDivisionError("division by zero"))

        summary = run_pipeline(conn, config, (good, buggy))

        assert summary.status == "partial"
        assert count_observations(conn, "good") == 1
        entry = source_runs(conn, summary.run_id)["buggy"]
        assert "ZeroDivisionError" in str(entry["error_message"])

    def test_every_source_failing_makes_the_run_fail(
        self, conn: Connection, config: Config
    ) -> None:
        summary = run_pipeline(
            conn,
            config,
            (
                FakeSource("a", error=SourceUnavailable("a", "down")),
                FakeSource("b", error=SourceUnavailable("b", "down")),
            ),
        )

        assert summary.status == "failed"
        # Only a total failure is worth waking someone for.
        assert summary.exit_code == 1

    def test_a_partial_run_still_exits_zero(self, conn: Connection, config: Config) -> None:
        summary = run_pipeline(
            conn,
            config,
            (
                FakeSource("ok", ExtractionResult((observation("ok"),), ())),
                FakeSource("down", error=SourceUnavailable("down", "down")),
            ),
        )

        assert summary.status == "partial"
        # One flaky public website out of three is the condition this pipeline
        # absorbs. Alerting on it every time trains everyone to ignore alerts.
        assert summary.exit_code == 0


class TestLedger:
    def test_the_record_of_a_failure_survives_the_rollback(
        self, conn: Connection, config: Config
    ) -> None:
        # If the ledger shared a transaction with the load it describes, this
        # row would have been rolled back along with the data, and the table
        # would only ever contain successes.
        broken = FakeSource("broken", ExtractionResult((poisoned("broken"),), ()))

        summary = run_pipeline(conn, config, (broken,))

        assert source_runs(conn, summary.run_id)["broken"]["status"] == "failed"

    def test_records_the_counts_that_distinguish_a_healthy_run(
        self, conn: Connection, config: Config
    ) -> None:
        source = FakeSource(
            "counted",
            ExtractionResult(
                observations=(observation("counted"), observation("counted", "CHL")),
                rejections=(RecordRejected("value is not a number"),),
                skipped=183,
            ),
        )

        summary = run_pipeline(conn, config, (source,))
        entry = source_runs(conn, summary.run_id)["counted"]

        assert entry["rows_loaded"] == 2
        assert entry["rows_rejected"] == 1
        # 183 untracked countries are not 183 problems, and keeping them in a
        # separate column is what stops the one real rejection being invisible.
        assert entry["rows_skipped"] == 183

    def test_rejections_do_not_stop_the_load(self, conn: Connection, config: Config) -> None:
        source = FakeSource(
            "mixed",
            ExtractionResult(
                observations=(observation("mixed"),),
                rejections=(RecordRejected("bad row"), RecordRejected("another")),
            ),
        )

        summary = run_pipeline(conn, config, (source,))

        assert summary.status == "succeeded"
        assert count_observations(conn, "mixed") == 1

    def test_records_what_triggered_the_run(self, conn: Connection, config: Config) -> None:
        summary = run_pipeline(conn, config, (FakeSource("s"),), triggered_by="schedule")

        with conn.cursor() as cur:
            cur.execute(
                "SELECT triggered_by, status, finished_at FROM pipeline_runs WHERE id = %s",
                (summary.run_id,),
            )
            row = cur.fetchone()

        assert row is not None
        assert row["triggered_by"] == "schedule"
        assert row["finished_at"] is not None

    def test_registers_each_source_before_referencing_it(
        self, conn: Connection, config: Config
    ) -> None:
        run_pipeline(conn, config, (FakeSource("registered"),))

        with conn.cursor() as cur:
            cur.execute("SELECT key, name, url, kind FROM sources")
            rows = cur.fetchall()

        assert [row["key"] for row in rows] == ["registered"]
        assert rows[0]["kind"] == "rest_api"


class TestIdempotence:
    def test_running_twice_loads_nothing_new(self, conn: Connection, config: Config) -> None:
        # A weekly schedule must not accumulate a duplicate set of every
        # observation per run. The natural key and the upsert are what prevent it.
        source = FakeSource(
            "repeat",
            ExtractionResult((observation("repeat"), observation("repeat", "CHL")), ()),
        )

        run_pipeline(conn, config, (source,))
        assert count_observations(conn, "repeat") == 2

        second = run_pipeline(conn, config, (source,))
        assert count_observations(conn, "repeat") == 2
        # Nothing moved, so nothing was written.
        assert second.outcomes[0].changed == 0

    def test_an_unchanged_value_leaves_updated_at_alone(
        self, conn: Connection, config: Config
    ) -> None:
        # updated_at must mean "when this figure last changed", not "when we
        # last looked". The conditional DO UPDATE is what makes the difference.
        source = FakeSource("stable", ExtractionResult((observation("stable"),), ()))

        run_pipeline(conn, config, (source,))
        with conn.cursor() as cur:
            cur.execute("SELECT updated_at FROM observations WHERE source_key = 'stable'")
            row = cur.fetchone()
            assert row is not None
            first_seen = row["updated_at"]

        run_pipeline(conn, config, (source,))
        with conn.cursor() as cur:
            cur.execute("SELECT updated_at FROM observations WHERE source_key = 'stable'")
            row = cur.fetchone()
            assert row is not None

        assert row["updated_at"] == first_seen

    def test_a_revised_figure_updates_in_place(self, conn: Connection, config: Config) -> None:
        original = FakeSource("revised", ExtractionResult((observation("revised", value=7.4),), ()))
        run_pipeline(conn, config, (original,))

        corrected = FakeSource(
            "revised", ExtractionResult((observation("revised", value=7.9),), ())
        )
        summary = run_pipeline(conn, config, (corrected,))

        with conn.cursor() as cur:
            cur.execute("SELECT value FROM observations WHERE source_key = 'revised'")
            rows = cur.fetchall()

        assert len(rows) == 1
        assert to_float(rows[0]["value"]) == pytest.approx(7.9)
        assert summary.outcomes[0].changed == 1


class TestReconciliation:
    def test_shows_the_spread_between_sources_for_one_country_year(
        self, conn: Connection, config: Config
    ) -> None:
        sources = (
            FakeSource("api", ExtractionResult((observation("api", value=7.4),), ())),
            FakeSource("csv", ExtractionResult((observation("csv", value=7.4),), ())),
            FakeSource("html", ExtractionResult((observation("html", value=3.1),), ())),
        )
        run_pipeline(conn, config, sources)

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT source_count, min_value, max_value, spread, values_by_source
                  FROM observation_reconciliation
                 WHERE country_iso3 = 'BOL' AND year = 2024
                """
            )
            row = cur.fetchone()

        assert row is not None
        assert row["source_count"] == 3
        assert to_float(row["spread"]) == pytest.approx(4.3)
        # jsonb_object_agg comes back as a dict; asserting the type is also
        # asserting the view returns the per-source breakdown, not a scalar.
        values_by_source = row["values_by_source"]
        assert isinstance(values_by_source, dict)
        assert set(values_by_source) == {"api", "csv", "html"}

    def test_two_sources_that_agree_show_no_spread(self, conn: Connection, config: Config) -> None:
        # This is not a hypothetical. Our World in Data republishes the World
        # Bank series, so those two sources agree to the decimal and the view
        # reports a spread of exactly zero. Documenting that is more useful than
        # implying three independent measurements.
        sources = (
            FakeSource("api", ExtractionResult((observation("api", value=7.4),), ())),
            FakeSource("mirror", ExtractionResult((observation("mirror", value=7.4),), ())),
        )
        run_pipeline(conn, config, sources)

        with conn.cursor() as cur:
            cur.execute("SELECT spread FROM observation_reconciliation WHERE country_iso3 = 'BOL'")
            row = cur.fetchone()

        assert row is not None
        assert to_float(row["spread"]) == 0.0


class TestDatabaseConstraints:
    def test_the_check_constraint_refuses_an_implausible_rate(
        self, conn: Connection, config: Config
    ) -> None:
        run_pipeline(conn, config, (FakeSource("s", ExtractionResult((observation("s"),), ())),))

        with pytest.raises(psycopg.errors.CheckViolation), conn.cursor() as cur:
            cur.execute("UPDATE observations SET value = 200 WHERE source_key = 's'")
        conn.rollback()

    def test_an_observation_cannot_reference_an_unregistered_source(self, conn: Connection) -> None:
        with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.cursor() as cur:
            cur.execute(
                """
                    INSERT INTO observations
                        (source_key, country_iso3, year, indicator_code, value)
                    VALUES ('never_registered', 'BOL', 2024, 'UNEMPLOYMENT_RATE', 7.4)
                    """
            )
        conn.rollback()

    def test_deleting_a_source_takes_its_observations_with_it(
        self, conn: Connection, config: Config
    ) -> None:
        run_pipeline(conn, config, (FakeSource("s", ExtractionResult((observation("s"),), ())),))

        with conn.cursor() as cur:
            cur.execute("DELETE FROM sources WHERE key = 's'")
        conn.commit()

        assert count_observations(conn, "s") == 0

    def test_a_failed_source_run_must_carry_a_reason(self, conn: Connection) -> None:
        # The schema refuses a failure with no explanation, so an unexplained
        # entry can never reach the ledger even if a future code path forgets.
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO pipeline_runs (status, triggered_by) VALUES ('running', 't') "
                "RETURNING id"
            )
            row = cur.fetchone()
            assert row is not None
            run_id = row["id"]

            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute(
                    "INSERT INTO source_runs (run_id, source_key, status) "
                    "VALUES (%s, 'x', 'failed')",
                    (run_id,),
                )
        conn.rollback()


class TestRunnerContract:
    def test_refuses_to_run_with_no_sources(self, conn: Connection, config: Config) -> None:
        with pytest.raises(ValueError, match="at least one source"):
            run_pipeline(conn, config, ())
