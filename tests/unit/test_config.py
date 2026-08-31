"""Configuration parsing: fail at startup, never mid-load."""

from __future__ import annotations

import pytest

from labour_etl.config import Config, load_config
from labour_etl.domain.errors import ConfigurationError

MINIMAL = {"DATABASE_URL": "postgres://user:pass@localhost:5432/db"}


def test_applies_defaults_when_only_the_database_url_is_given() -> None:
    config = load_config(MINIMAL)

    assert config.log_level == "INFO"
    assert config.http_timeout_seconds == 30.0
    assert config.http_max_retries == 3
    assert config.min_year == 2010
    assert config.country_filter == frozenset()


def test_fails_fast_without_a_database_url() -> None:
    # The alternative is a process that looks healthy and dies twenty seconds
    # into the first extraction.
    with pytest.raises(ConfigurationError, match="DATABASE_URL"):
        load_config({})


def test_parses_the_country_filter_into_upper_case_codes() -> None:
    config = load_config({**MINIMAL, "COUNTRY_FILTER": " bol , per,chl "})

    assert config.country_filter == frozenset({"BOL", "PER", "CHL"})


def test_an_empty_country_filter_means_every_country() -> None:
    config = load_config({**MINIMAL, "COUNTRY_FILTER": "  ,  "})

    assert config.country_filter == frozenset()
    assert config.wants("XYZ") is True


def test_a_populated_filter_excludes_everything_else() -> None:
    config = load_config({**MINIMAL, "COUNTRY_FILTER": "BOL"})

    assert config.wants("BOL") is True
    assert config.wants("PER") is False


@pytest.mark.parametrize("level", ["debug", "Warning", "ERROR"])
def test_accepts_a_log_level_in_any_case(level: str) -> None:
    assert load_config({**MINIMAL, "LOG_LEVEL": level}).log_level == level.upper()


def test_rejects_an_unknown_log_level_rather_than_guessing() -> None:
    with pytest.raises(ConfigurationError, match="LOG_LEVEL"):
        load_config({**MINIMAL, "LOG_LEVEL": "verbose"})


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("HTTP_MAX_RETRIES", "-1"),
        ("HTTP_MAX_RETRIES", "three"),
        ("HTTP_TIMEOUT_SECONDS", "0"),
        ("HTTP_TIMEOUT_SECONDS", "soon"),
        ("MIN_YEAR", "1800"),
    ],
)
def test_rejects_out_of_range_and_unparseable_numbers(key: str, value: str) -> None:
    with pytest.raises(ConfigurationError, match=key):
        load_config({**MINIMAL, key: value})


def test_zero_retries_is_allowed() -> None:
    # A single attempt with no retry is a legitimate choice, unlike a negative
    # budget which is a typo.
    assert load_config({**MINIMAL, "HTTP_MAX_RETRIES": "0"}).http_max_retries == 0


def test_blank_values_fall_back_to_the_default() -> None:
    # Unset variables arrive as empty strings from a .env file or a workflow
    # that declares them without a value.
    config = load_config({**MINIMAL, "HTTP_MAX_RETRIES": "", "MIN_YEAR": "  "})

    assert config.http_max_retries == 3
    assert config.min_year == 2010


def test_config_is_immutable() -> None:
    config = Config(database_url="postgres://x")

    with pytest.raises(AttributeError):
        config.min_year = 1990  # type: ignore[misc]
