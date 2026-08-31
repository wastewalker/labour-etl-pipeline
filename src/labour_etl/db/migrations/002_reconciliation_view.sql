-- The payoff of loading the same indicator from three publishers: seeing where
-- they disagree.
--
-- A view rather than a table. The underlying data is small, the arithmetic is
-- trivial, and a materialised copy would be a second source of truth that the
-- loader has to remember to refresh.

CREATE VIEW observation_reconciliation AS
SELECT
    country_iso3,
    year,
    indicator_code,
    count(*)                                   AS source_count,
    jsonb_object_agg(source_key, value)        AS values_by_source,
    round(min(value), 3)                       AS min_value,
    round(max(value), 3)                       AS max_value,
    round(max(value) - min(value), 3)          AS spread,
    round(avg(value), 3)                       AS mean_value
FROM observations
GROUP BY country_iso3, year, indicator_code;

COMMENT ON VIEW observation_reconciliation IS
    'One row per country-year, with each source''s figure and the spread '
    'between them. A large spread is not an error: the sources measure '
    'unemployment with different methodologies, and showing the disagreement '
    'is more honest than picking one and calling it the truth.';
