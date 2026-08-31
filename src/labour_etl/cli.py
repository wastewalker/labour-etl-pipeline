"""Command line entry point.

Four verbs, because those are the four things anyone actually does with this:
migrate the schema, run the pipeline, look at what the last runs did, and look
at where the sources disagree.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence

from dotenv import load_dotenv

from .config import load_config
from .db.connection import connect
from .db.migrate import run_migrations
from .domain.errors import EtlError
from .pipeline.runner import run_pipeline
from .sources import default_sources

LOGGER = logging.getLogger("labour_etl")


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        # UTC everywhere. A run ledger whose timestamps are in the operator's
        # local time is a ledger nobody can correlate with anything else.
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )


def _cmd_migrate(args: argparse.Namespace) -> int:
    config = load_config()
    _configure_logging(config.log_level)

    with connect(config.database_url) as conn:
        result = run_migrations(conn)

    if result.applied:
        print(f"Applied {len(result.applied)} migration(s): {', '.join(result.applied)}")
    else:
        print(f"Schema is up to date ({len(result.skipped)} migration(s) already applied)")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    config = load_config()
    _configure_logging(config.log_level)

    with connect(config.database_url) as conn:
        run_migrations(conn)
        summary = run_pipeline(conn, config, default_sources(), triggered_by=args.triggered_by)

    print(f"\nRun {summary.run_id}: {summary.status.upper()}")
    for outcome in summary.outcomes:
        if outcome.succeeded:
            print(
                f"  OK    {outcome.source_key:18s} "
                f"loaded={outcome.counts.loaded:<5d} changed={outcome.changed:<5d} "
                f"rejected={outcome.counts.rejected:<4d} out-of-scope={outcome.counts.skipped}"
            )
        else:
            print(f"  FAIL  {outcome.source_key:18s} {outcome.error}")

    print(f"\n{summary.succeeded_count}/{len(summary.outcomes)} sources succeeded.")
    return summary.exit_code


def _cmd_status(args: argparse.Namespace) -> int:
    config = load_config()
    _configure_logging(config.log_level)

    with connect(config.database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.id, r.started_at, r.finished_at, r.status, r.triggered_by,
                   count(sr.id) FILTER (WHERE sr.status = 'succeeded') AS ok,
                   count(sr.id)                                        AS total
              FROM pipeline_runs r
              LEFT JOIN source_runs sr ON sr.run_id = r.id
             GROUP BY r.id
             ORDER BY r.started_at DESC
             LIMIT %s
            """,
            (args.limit,),
        )
        runs = cur.fetchall()

        if not runs:
            print("No runs recorded yet.")
            return 0

        print(f"{'ID':>5}  {'STARTED (UTC)':20}  {'STATUS':10}  {'SOURCES':8}  TRIGGER")
        for run in runs:
            started = str(run["started_at"])[:19]
            print(
                f"{run['id']:>5}  {started:20}  {run['status']!s:10}  "
                f"{run['ok']}/{run['total']:<6}  {run['triggered_by']}"
            )

        cur.execute(
            """
            SELECT source_key, status, rows_loaded, rows_rejected, rows_skipped, error_message
              FROM source_runs
             WHERE run_id = %s
             ORDER BY source_key
            """,
            (runs[0]["id"],),
        )
        print(f"\nSources in run {runs[0]['id']}:")
        for source_run in cur.fetchall():
            detail = (
                source_run["error_message"]
                if source_run["status"] == "failed"
                else f"loaded={source_run['rows_loaded']} "
                f"rejected={source_run['rows_rejected']} "
                f"out-of-scope={source_run['rows_skipped']}"
            )
            print(f"  {source_run['source_key']!s:18s} {source_run['status']!s:10s} {detail}")

    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    config = load_config()
    _configure_logging(config.log_level)

    with connect(config.database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT country_iso3, year, source_count, values_by_source, spread
              FROM observation_reconciliation
             WHERE source_count > 1
             ORDER BY spread DESC, country_iso3, year
             LIMIT %s
            """,
            (args.limit,),
        )
        rows = cur.fetchall()

    if not rows:
        print("No country-year has more than one source yet. Run the pipeline first.")
        return 0

    print("Where the sources disagree most (spread in percentage points):\n")
    print(f"{'COUNTRY':8} {'YEAR':6} {'SPREAD':8} VALUES BY SOURCE")
    for row in rows:
        values = json.dumps(row["values_by_source"], sort_keys=True)
        print(f"{row['country_iso3']!s:8} {row['year']:<6} {row['spread']!s:8} {values}")

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="labour-etl",
        description="Load one labour indicator from three public sources.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    migrate = subparsers.add_parser("migrate", help="apply pending schema migrations")
    migrate.set_defaults(handler=_cmd_migrate)

    run = subparsers.add_parser("run", help="migrate, then extract and load every source")
    run.add_argument(
        "--triggered-by",
        default="manual",
        help="recorded in the ledger; the scheduled workflow passes 'schedule'",
    )
    run.set_defaults(handler=_cmd_run)

    status = subparsers.add_parser("status", help="show recent runs from the ledger")
    status.add_argument("--limit", type=int, default=10)
    status.set_defaults(handler=_cmd_status)

    report = subparsers.add_parser("report", help="show where the sources disagree")
    report.add_argument("--limit", type=int, default=20)
    report.set_defaults(handler=_cmd_report)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)

    try:
        exit_code: int = args.handler(args)
        return exit_code
    except EtlError as exc:
        # Expected failures get one readable line. A stack trace here would bury
        # "DATABASE_URL is required" under twenty frames of argparse.
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
