"""Orchestration: run every source, isolate every failure.

The shape of this module is the answer to one question - what should happen when
one of three sources is broken at three in the morning? The answer taken here is
that the other two should still load, the broken one should leave the previous
night's data untouched rather than half-replaced, and the ledger should say
plainly which was which.

Concretely, per source:

  1. Extract, outside any transaction. Network calls are slow and a transaction
     held open across one pins a connection and a snapshot for no reason.
  2. Load inside one transaction. Commit on success, roll back on any error.
  3. Write the outcome to the ledger, committed separately, so the record of a
     failure cannot be rolled back along with the failure itself.

A source raising anything at all is contained. The pipeline only fails as a
whole when every source failed, or when the database is unreachable - in which
case there is nowhere to record anything anyway.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import psycopg

from ..config import Config
from ..domain.errors import SourceUnavailable
from ..sources.base import Source
from . import ledger
from .ledger import SourceCounts
from .loader import load_observations

LOGGER = logging.getLogger(__name__)

Connection = psycopg.Connection[dict[str, object]]


@dataclass(frozen=True, slots=True)
class SourceOutcome:
    source_key: str
    succeeded: bool
    counts: SourceCounts
    changed: int = 0
    error: str | None = None


@dataclass(frozen=True, slots=True)
class RunSummary:
    run_id: int
    status: ledger.RunStatus
    outcomes: tuple[SourceOutcome, ...] = field(default=())

    @property
    def succeeded_count(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.succeeded)

    @property
    def failed_sources(self) -> tuple[str, ...]:
        return tuple(o.source_key for o in self.outcomes if not o.succeeded)

    @property
    def rows_loaded(self) -> int:
        return sum(o.counts.loaded for o in self.outcomes)

    @property
    def exit_code(self) -> int:
        """What the process should return.

        A partial run exits 0 on purpose. One flaky source out of three is the
        condition this pipeline is designed to absorb, and a scheduler that
        alerts on it every time a public website is briefly slow is a scheduler
        whose alerts get ignored. The ledger still records the failure, and the
        summary still names it.
        """
        return 0 if self.status in ("succeeded", "partial") else 1


def _run_one_source(
    conn: Connection,
    config: Config,
    source: Source,
    run_id: int,
) -> SourceOutcome:
    source_run_id = ledger.start_source_run(conn, run_id, source.key)

    try:
        # Deliberately outside a transaction: this is the slow part.
        extraction = source.extract(config)
    except SourceUnavailable as exc:
        LOGGER.warning("Source %s unavailable: %s", source.key, exc.reason)
        ledger.finish_source_run(conn, source_run_id, "failed", SourceCounts(), exc.reason)
        return SourceOutcome(source.key, succeeded=False, counts=SourceCounts(), error=exc.reason)
    except Exception as exc:
        # A source is third-party-shaped code touching third-party-shaped data.
        # Anything it raises is contained here so the remaining sources still
        # get their turn; letting an unexpected exception escape would make one
        # source's bug an outage for all of them.
        reason = f"unexpected {type(exc).__name__}: {exc}"
        LOGGER.exception("Source %s raised an unexpected error", source.key)
        ledger.finish_source_run(conn, source_run_id, "failed", SourceCounts(), reason)
        return SourceOutcome(source.key, succeeded=False, counts=SourceCounts(), error=reason)

    counts = SourceCounts(
        extracted=extraction.extracted_count,
        loaded=0,
        rejected=len(extraction.rejections),
        skipped=extraction.skipped,
    )

    for rejection in extraction.rejections:
        LOGGER.warning("Source %s rejected a record: %s", source.key, rejection.reason)

    try:
        result = load_observations(conn, extraction.observations)
        conn.commit()
    except Exception as exc:
        conn.rollback()
        reason = f"load failed and was rolled back: {type(exc).__name__}: {exc}"
        LOGGER.exception("Source %s failed during load", source.key)
        ledger.finish_source_run(conn, source_run_id, "failed", counts, reason)
        return SourceOutcome(source.key, succeeded=False, counts=counts, error=reason)

    counts = SourceCounts(
        extracted=counts.extracted,
        loaded=result.written,
        rejected=counts.rejected,
        skipped=counts.skipped,
    )
    ledger.finish_source_run(conn, source_run_id, "succeeded", counts)

    LOGGER.info(
        "Source %s loaded %d rows (%d changed, %d rejected, %d out of scope)",
        source.key,
        result.written,
        result.changed,
        counts.rejected,
        counts.skipped,
    )
    return SourceOutcome(source.key, succeeded=True, counts=counts, changed=result.changed)


def run_pipeline(
    conn: Connection,
    config: Config,
    sources: tuple[Source, ...],
    triggered_by: str = "manual",
) -> RunSummary:
    """Run every source and return what happened."""
    if not sources:
        raise ValueError("run_pipeline needs at least one source")

    ledger.upsert_sources(conn, sources)
    run_id = ledger.start_run(conn, triggered_by)

    outcomes = tuple(_run_one_source(conn, config, source, run_id) for source in sources)

    succeeded = sum(1 for outcome in outcomes if outcome.succeeded)
    if succeeded == len(outcomes):
        status: ledger.RunStatus = "succeeded"
    elif succeeded == 0:
        status = "failed"
    else:
        status = "partial"

    ledger.finish_run(conn, run_id, status)

    LOGGER.info(
        "Run %d finished as '%s': %d/%d sources succeeded",
        run_id,
        status,
        succeeded,
        len(outcomes),
    )
    return RunSummary(run_id=run_id, status=status, outcomes=outcomes)
