"""Configuration, parsed and validated once at startup.

A missing ``DATABASE_URL`` should stop the process with a readable message, not
surface twenty seconds later as a connection error in the middle of a load.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .domain.errors import ConfigurationError

VALID_LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})


@dataclass(frozen=True, slots=True)
class Config:
    database_url: str
    log_level: str = "INFO"
    http_timeout_seconds: float = 30.0
    http_max_retries: int = 3
    #: Empty means "every country the source publishes".
    country_filter: frozenset[str] = field(default_factory=frozenset)
    min_year: int = 2010

    def wants(self, iso3: str) -> bool:
        """Whether an observation for this country should be loaded."""
        return not self.country_filter or iso3 in self.country_filter


def _read_int(env: dict[str, str], key: str, default: int, *, minimum: int) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{key} must be a whole number, got '{raw}'") from exc
    if value < minimum:
        raise ConfigurationError(f"{key} must be at least {minimum}, got {value}")
    return value


def _read_float(env: dict[str, str], key: str, default: float, *, minimum: float) -> float:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{key} must be a number, got '{raw}'") from exc
    if value < minimum:
        raise ConfigurationError(f"{key} must be at least {minimum}, got {value}")
    return value


def load_config(env: dict[str, str] | None = None) -> Config:
    """Build a validated ``Config`` from the environment.

    Takes the mapping as an argument so tests configure it directly instead of
    mutating ``os.environ`` and leaking state between them.
    """
    source = dict(os.environ) if env is None else env

    database_url = source.get("DATABASE_URL", "").strip()
    if not database_url:
        raise ConfigurationError("DATABASE_URL is required")

    log_level = source.get("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    if log_level not in VALID_LOG_LEVELS:
        raise ConfigurationError(
            f"LOG_LEVEL must be one of {', '.join(sorted(VALID_LOG_LEVELS))}, got '{log_level}'"
        )

    raw_filter = source.get("COUNTRY_FILTER", "").strip()
    country_filter = frozenset(
        code.strip().upper() for code in raw_filter.split(",") if code.strip()
    )

    return Config(
        database_url=database_url,
        log_level=log_level,
        http_timeout_seconds=_read_float(source, "HTTP_TIMEOUT_SECONDS", 30.0, minimum=1.0),
        http_max_retries=_read_int(source, "HTTP_MAX_RETRIES", 3, minimum=0),
        country_filter=country_filter,
        min_year=_read_int(source, "MIN_YEAR", 2010, minimum=1960),
    )
