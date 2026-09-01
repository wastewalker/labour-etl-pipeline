# Labour ETL Pipeline

[![CI](https://github.com/wastewalker/labour-etl-pipeline/actions/workflows/ci.yml/badge.svg)](https://github.com/wastewalker/labour-etl-pipeline/actions/workflows/ci.yml)
[![Scheduled run](https://github.com/wastewalker/labour-etl-pipeline/actions/workflows/scheduled-run.yml/badge.svg)](https://github.com/wastewalker/labour-etl-pipeline/actions/workflows/scheduled-run.yml)
[![Python](https://img.shields.io/badge/python-3.12-blue)](https://www.python.org)
[![Types](https://img.shields.io/badge/mypy-strict-blue)](./pyproject.toml)
[![Coverage](https://img.shields.io/badge/coverage-97%25-blue)](./pyproject.toml)

An ETL pipeline that loads the same indicator — the unemployment rate — from
three public sources that publish it in three different formats, and keeps every
figure attributed to the source it came from so you can see where they disagree.

The engineering claim is not "it loads data". It is that **a bad night leaves the
database consistent and the ledger honest**: one broken source does not stop the
others, a load that fails halfway leaves nothing behind, and re-running changes
nothing that has not actually changed.

---

## Table of contents

- [The three sources](#the-three-sources)
- [What it actually produces](#what-it-actually-produces)
- [Quick start](#quick-start)
- [How failure is handled](#how-failure-is-handled)
- [The run ledger](#the-run-ledger)
- [Design decisions](#design-decisions)
- [Schema](#schema)
- [Testing strategy](#testing-strategy)
- [CI and scheduling](#ci-and-scheduling)
- [Limitations](#limitations)
- [How AI was used](#how-ai-was-used)

---

## The three sources

| Source | Format | Why it is here |
| --- | --- | --- |
| [World Bank Indicators API](https://datahelpdesk.worldbank.org/knowledgebase/articles/889392) | REST / JSON, paginated | The well-behaved one. Stable contract, explicit envelope, real pagination. |
| [Our World in Data](https://ourworldindata.org/grapher/unemployment-rate) | CSV over HTTP | Bulk download of every country and year. Filtering happens in pandas before any object is built. |
| [Wikipedia](https://en.wikipedia.org/wiki/List_of_countries_by_unemployment_rate) | Rendered HTML table | The fragile one, on purpose. Two-level header, four publishers side by side, footnote markers, and an en dash where a figure is missing. |

Three formats, three sets of problems. The HTML source is included precisely
because it is the one most likely to break on any given night — a pipeline whose
resilience is never exercised is a pipeline whose resilience is untested.

### An honest note about independence

Two of these three are not independent. **Our World in Data republishes the World
Bank series**, so they agree to the decimal, and the reconciliation view reports a
spread of exactly zero between them. That is visible in the output below and it
is asserted by a test.

This was discovered by running the pipeline, not assumed while designing it. The
sources stay as they are because the point of the exercise is three *formats*,
not three *methodologies* — but describing them as three independent measurements
would be a claim the data does not support.

## What it actually produces

Real output from a run against the live sources:

```
Run 1: SUCCEEDED
  OK    world_bank_api     loaded=110   changed=110   rejected=0    out-of-scope=0
  OK    owid_csv           loaded=110   changed=110   rejected=0    out-of-scope=6876
  OK    wikipedia_cia      loaded=10    changed=10    rejected=0    out-of-scope=183
```

Running it again immediately afterwards:

```
Run 2: SUCCEEDED
  OK    world_bank_api     loaded=110   changed=0     rejected=0    out-of-scope=0
  OK    owid_csv           loaded=110   changed=0     rejected=0    out-of-scope=6876
  OK    wikipedia_cia      loaded=10    changed=0     rejected=0    out-of-scope=183
```

`changed=0` is what idempotence looks like from the outside: the rows were
presented to the database and none of them moved.

And the payoff — `labour-etl report`:

```
COUNTRY  YEAR   SPREAD   VALUES BY SOURCE
ECU      2024   1.347    {"owid_csv": 3.453, "wikipedia_cia": 4.8, "world_bank_api": 3.453}
BRA      2024   0.899    {"owid_csv": 6.801, "wikipedia_cia": 7.7, "world_bank_api": 6.801}
ARG      2024   0.750    {"owid_csv": 7.15,  "wikipedia_cia": 7.9, "world_bank_api": 7.15}
BOL      2024   0.174    {"owid_csv": 3.274, "wikipedia_cia": 3.1, "world_bank_api": 3.274}
```

Ecuador's unemployment rate is 3.45% or 4.8% depending on who you ask. Neither is
wrong; they measure different things. Storing one number and calling it "the"
unemployment rate is how a dashboard ends up lying quietly, so this pipeline
keeps all of them and shows the gap.

## Quick start

Everything below assumes Docker is running. Nothing else needs installing.

```bash
docker compose up -d db
docker compose run --rm etl migrate
docker compose run --rm etl run
docker compose run --rm etl report
```

### Without Docker

```bash
python -m venv .venv && . .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env                            # then point DATABASE_URL at a database
labour-etl run
```

### The four commands

| Command | What it does |
| --- | --- |
| `labour-etl migrate` | Apply pending schema migrations. Idempotent. |
| `labour-etl run` | Migrate, then extract and load every source. |
| `labour-etl status` | Recent runs, and the per-source detail of the latest. |
| `labour-etl report` | Where the sources disagree, worst first. |

### Configuration

All optional except `DATABASE_URL` — see [`.env.example`](./.env.example).
`COUNTRY_FILTER` and `MIN_YEAR` bound the load; leaving them unset loads every
country and year the sources publish, which is around 6,000 rows.

## How failure is handled

This is the part worth reading. Every source runs through the same three steps:

1. **Extract, outside any transaction.** Network calls are slow, and holding a
   transaction open across one pins a connection and a snapshot for no reason.
2. **Load inside one transaction.** Commit on success, roll back on any error.
3. **Write the outcome to the ledger, committed separately.**

Step 3 is separate from step 2 deliberately. If the ledger entry shared a
transaction with the load it describes, a failed load would roll back the record
of its own failure, and the table would only ever contain successes.

### Three kinds of bad, treated three different ways

The distinction the whole design rests on:

| What happened | What the pipeline does | Why |
| --- | --- | --- |
| **One row is unusable** (`RecordRejected`) | Count it, log the reason, keep loading | One malformed row out of 900 is not a failed source. Aborting would throw away the 899 good ones. |
| **A row is out of scope** (`skipped`) | Count it separately, say nothing | The HTML source publishes 193 countries and this pipeline tracks 10. The other 183 are not 183 problems. |
| **The source itself is unreadable** (`SourceUnavailable`) | Abandon it, roll back, record why | The host is down, or the schema changed. Nothing from it can be trusted, and a partial load would silently replace good data with less of it. |

Getting these the wrong way round is the classic ETL failure: either one bad row
aborts a nightly load, or a source that started returning an HTML error page
quietly overwrites good data with nothing.

**Out-of-scope gets its own column, and that matters.** Folding those 183 rows
into the rejection count would make the number large on every healthy run — and a
number that is always large is a number nobody reads. The one row that genuinely
failed to parse would be invisible inside it.

This is not theoretical. A live run reported **80 rejections** from the World Bank
source before a fix: the API leaves `countryiso3code` blank for some aggregate
entities, and those were being counted as malformed. Nothing was actually wrong.
That is exactly how a rejection count stops being worth reading, and it is why
the two counters are separate.

### Exit codes

A **partial** run — one source down, two loaded — exits **0**. One flaky public
website out of three is the condition this pipeline is built to absorb, and a
scheduler that alerts every time a website is briefly slow is a scheduler whose
alerts get ignored. Only a run where *every* source failed exits 1.

## The run ledger

Two tables, because the two questions are different. `pipeline_runs` answers "did
last night's run work". `source_runs` answers "why is the HTML source stale".

```
   ID  STARTED (UTC)         STATUS      SOURCES   TRIGGER
    2  2026-08-31 22:12:28   succeeded   3/3       manual
    1  2026-08-31 22:12:24   succeeded   3/3       manual

Sources in run 2:
  owid_csv           succeeded  loaded=110 rejected=0 out-of-scope=6876
  wikipedia_cia      succeeded  loaded=10  rejected=0 out-of-scope=183
  world_bank_api     succeeded  loaded=110 rejected=0 out-of-scope=0
```

A failed source must carry a reason and a successful one must not. That is a
`CHECK` constraint, not a convention, so an unexplained failure can never reach
the ledger even if a future code path forgets.

## Design decisions

### Re-running is safe, and `updated_at` means something

The load is an upsert on the natural key `(source, country, year, indicator)`, so
a source revising a figure updates the existing row rather than adding a second
one. Without that constraint a daily schedule would accumulate a duplicate set of
every observation per run.

The `ON CONFLICT DO UPDATE` carries a `WHERE observations.value IS DISTINCT FROM
EXCLUDED.value`. Without it, every run rewrites every row and stamps `updated_at`
with the run time — which makes the column mean "when we last looked" instead of
"when this figure last changed". Only the second is worth storing.

### Aggregates are the trap in this dataset

The World Bank returns regional and income aggregates alongside countries, using
codes that look exactly like ISO-3166 alpha-3: `WLD`, `LCN`, `HIC`. Loading them
as countries is the easiest way to double count — "Latin America & Caribbean"
contains Bolivia, so a naive sum over the table counts Bolivia twice. They are
filtered by an explicit list, and rejected by the validator if one gets through.

### Country names are a lookup table, never a fuzzy match

The HTML source identifies countries by name. A fuzzy match that resolves
`Guinea-Bissau` to `Guinea` produces data that is *wrong* rather than *missing*,
and wrong data does not announce itself. A name not in the table is out of scope,
full stop.

### The HTML source reads its year out of the table header

The sub-header cells read `CIA [5] (2024)`. The year is metadata to be parsed,
not a constant to hardcode — so when Wikipedia refreshes the table to 2025 the
pipeline follows without a code change. If the target column disappears entirely,
the source fails loudly rather than silently reading the column next to it, which
would attribute a different publisher's methodology to this source's data.

### One column out of four

That same table carries four publishers side by side. The **CIA World Factbook**
column is read, and the neighbouring **World Bank** column deliberately is not —
taking it would give the same publisher two votes and make the reconciliation
view show agreement where there is only duplication.

### Values are `NUMERIC`, not `float`

`NUMERIC(9,6)` in the database, `float` in the domain, and one explicit
conversion at the boundary rather than a global type adapter. psycopg returning
`Decimal` is correct behaviour for an arbitrary-precision column, and silently
changing that for every `NUMERIC` in the schema to avoid one conversion is a bad
trade.

### Migrations run before every load

For a pipeline this size it removes a whole class of "deployed, but the schema is
one release behind" incidents, and it means the tests run the real migrator
rather than a schema fixture that can drift. The advisory lock in
`src/labour_etl/db/migrate.py` handles two processes starting together. On a
larger system with migrations that take minutes, this belongs in a release step
instead.

## Schema

```
sources ─────< observations
   key           UNIQUE (source_key, country_iso3, year, indicator_code)

pipeline_runs ─────< source_runs
   running |            running | succeeded | failed
   succeeded |          rows_extracted / loaded / rejected / skipped
   partial |            error_message (required iff failed)
   failed

observation_reconciliation  (view)
   country_iso3, year, source_count, values_by_source, min, max, spread, mean
```

Two migrations, applied in filename order: `001` creates the tables, `002` adds
the reconciliation view.

## Testing strategy

151 tests, 97% coverage, thresholds enforced in `pyproject.toml`.

**Unit** (`tests/unit`, 126 tests, no I/O) covers normalisation, configuration,
the HTTP retry budget driven through `httpx.MockTransport` with an injected
`sleep`, and all three extractors against **saved fixtures of real responses**.

The fixtures matter. A hand-written fixture only contains the shapes its author
remembered, and the whole difficulty here is the shapes nobody remembers: the en
dash (U+2013) standing in for a missing figure, the narrow no-break space
(U+202F) before a footnote asterisk, the two-level table header. Malformed inputs
are written inline instead — corrupting a fixture would make it lie about what
the source actually publishes.

**Integration** (`tests/integration`, 25 tests) runs against a real PostgreSQL 16
started by Testcontainers on a pinned tag. It covers what a mock cannot:

- **Rollback mid-load.** A source yields two good rows and one the database
  refuses; the test asserts *zero* rows survive for that source. Without the
  transaction the first two would already be committed.
- **Failure isolation.** Good source, broken source, good source — the test
  asserts the third was still asked for its data and the first's rows survived.
- **The ledger outlives the rollback**, which it would not if it shared the
  transaction.
- **Idempotence**: a second run loads the same rows, reports `changed=0`, and
  leaves `updated_at` untouched.
- **Migration atomicity**: a migration that fails halfway leaves neither the
  table it created nor a ledger row claiming success.
- Database-level `CHECK`, foreign key and `ON DELETE CASCADE` behaviour.

The "poisoned" observation used by the rollback tests is built by calling the
dataclass directly rather than through `Observation.create`, which is the only
way to get one — the domain validation would reject it long before SQL. That is
the point: it simulates a value that got past the application, and proves the
`CHECK` constraint is a real backstop rather than decoration.

If Docker is unavailable, point the suite at any PostgreSQL instead. It is
truncated between tests, so give it a scratch database:

```bash
TEST_DATABASE_URL=postgres://user:pass@localhost:5432/scratch pytest tests/integration
```

## CI and scheduling

Two workflows, with a deliberate split.

**[`ci.yml`](.github/workflows/ci.yml)** runs on every push and pull request and
**never touches the network** beyond the package index: Ruff, `ruff format
--check`, mypy in strict mode, the full test suite with coverage thresholds, and
a Docker job that builds the image and drives the CLI against a real database.
The extractors are tested against fixtures, so a public website being slow can
never turn this build red.

**[`scheduled-run.yml`](.github/workflows/scheduled-run.yml)** runs daily at
06:15 UTC against the **live sources**. Its database is a service container, so
nothing persists between runs — this repository has no hosted database, and
pretending otherwise would be worse than saying so.

What that job proves every morning is that the three extractors still understand
what the three publishers are currently serving. That is the thing most likely to
break in a pipeline like this, and exactly what a green unit suite against saved
fixtures *cannot* tell you. It goes red only if every source fails; one publisher
being down produces a `partial` run and a note in the job summary, which keeps
the badge meaningful.

## Limitations

Stated plainly, because a reviewer will find them anyway:

- **The scheduled run does not persist.** No hosted database. Each run starts
  from an empty schema, so there is no accumulating history to query.
- **Two of the three sources share a publisher.** Documented above; the
  reconciliation view shows a spread of zero between them.
- **One indicator only.** Widening to many would change the schema and the
  reconciliation logic. An unused `indicator_code` column that always holds the
  same value would be scaffolding for a feature that does not exist.
- **The HTML source will break.** Wikipedia's layout is nobody's contract. It
  will fail one day, the other two will keep loading, and the ledger will say so.
- **No backfill and no point-in-time history.** An observation carries its current
  value and when it last changed, not every value it has ever had.
- **The country name mapping covers ten countries.** Widening the scope means
  extending that table by hand — deliberately, rather than guessing.

## How AI was used

This repository was built with Claude Code as an active participant, and
[`AI-WORKFLOW.md`](./AI-WORKFLOW.md) documents that honestly: what was delegated,
what was decided by hand, and the bugs the model shipped and how each was caught
— including the two that only surfaced by running the thing against the real
internet.

## License

MIT — see [LICENSE](./LICENSE).
