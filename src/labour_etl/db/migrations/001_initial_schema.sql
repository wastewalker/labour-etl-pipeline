-- Core schema: the source registry, the observations themselves, and the
-- two-level run ledger.

CREATE TABLE sources (
    key         TEXT PRIMARY KEY,
    name        TEXT        NOT NULL,
    url         TEXT        NOT NULL,
    kind        TEXT        NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT sources_kind_valid CHECK (kind IN ('rest_api', 'csv', 'html'))
);

COMMENT ON TABLE sources IS
    'Registry of upstream publishers. Rows are upserted by the pipeline at the '
    'start of each run, so the code stays the single source of truth for what '
    'a source is and where it lives.';

CREATE TABLE observations (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_key      TEXT           NOT NULL REFERENCES sources (key) ON DELETE CASCADE,
    country_iso3    CHAR(3)        NOT NULL,
    year            SMALLINT       NOT NULL,
    indicator_code  TEXT           NOT NULL,
    value           NUMERIC(9, 6)  NOT NULL,
    first_seen_at   TIMESTAMPTZ    NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ    NOT NULL DEFAULT now(),

    -- The natural key. Sources revise their figures, so re-reading one must
    -- update the existing row; without this constraint a weekly schedule would
    -- silently accumulate a duplicate set of every observation per run.
    CONSTRAINT observations_natural_key
        UNIQUE (source_key, country_iso3, year, indicator_code),

    -- The application validates these too. The database is the backstop: a bad
    -- migration or a manual UPDATE must not be able to store a rate of 740.
    CONSTRAINT observations_value_plausible CHECK (value >= 0 AND value <= 100),
    CONSTRAINT observations_year_plausible  CHECK (year >= 1960 AND year <= 2100),
    CONSTRAINT observations_iso3_shape      CHECK (country_iso3 ~ '^[A-Z]{3}$')
);

CREATE INDEX observations_country_year_idx
    ON observations (country_iso3, year);

-- The ledger is two levels because the two questions are different: "did last
-- night's run work" is about the run, "why is the HTML source stale" is about
-- one source within it.
CREATE TABLE pipeline_runs (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    status        TEXT        NOT NULL,
    triggered_by  TEXT        NOT NULL,

    CONSTRAINT pipeline_runs_status_valid
        CHECK (status IN ('running', 'succeeded', 'partial', 'failed'))
);

CREATE TABLE source_runs (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id          BIGINT      NOT NULL REFERENCES pipeline_runs (id) ON DELETE CASCADE,
    source_key      TEXT        NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    status          TEXT        NOT NULL,
    rows_extracted  INTEGER     NOT NULL DEFAULT 0,
    rows_loaded     INTEGER     NOT NULL DEFAULT 0,
    rows_rejected   INTEGER     NOT NULL DEFAULT 0,
    -- Rows the source published that are outside this pipeline's remit. Kept
    -- apart from rows_rejected so a genuine parse failure is visible instead of
    -- being lost inside a number that is large on every healthy run.
    rows_skipped    INTEGER     NOT NULL DEFAULT 0,
    error_message   TEXT,

    CONSTRAINT source_runs_status_valid
        CHECK (status IN ('running', 'succeeded', 'failed')),

    -- A failed source run must say why, and a successful one must not pretend
    -- there was a problem. Enforcing it here keeps the ledger trustworthy even
    -- if a future code path forgets.
    CONSTRAINT source_runs_failure_has_reason
        CHECK ((status = 'failed') = (error_message IS NOT NULL)),

    CONSTRAINT source_runs_unique_per_run UNIQUE (run_id, source_key)
);

CREATE INDEX source_runs_run_idx ON source_runs (run_id);

-- The ledger's most common query by far is "show me the last few runs".
CREATE INDEX pipeline_runs_started_idx ON pipeline_runs (started_at DESC);
