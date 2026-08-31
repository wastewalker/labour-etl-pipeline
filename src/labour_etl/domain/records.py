"""The canonical record every source is normalised into."""

from __future__ import annotations

from dataclasses import dataclass

from .errors import RecordRejected
from .normalize import normalize_iso3, normalize_rate, normalize_year

# This pipeline tracks exactly one indicator across three sources. The code is
# a constant rather than a parameter because widening it to many indicators
# would change the schema and the reconciliation logic, and pretending
# otherwise with an unused column would be scaffolding for a feature that does
# not exist.
INDICATOR_CODE = "UNEMPLOYMENT_RATE"


@dataclass(frozen=True, slots=True)
class Observation:
    """One country-year unemployment rate, attributed to the source it came from.

    Frozen because an observation is a fact that was read, not a mutable row.
    Anything that transforms it produces a new one, which keeps the
    already-loaded set and the incoming set impossible to mix up.
    """

    source_key: str
    country_iso3: str
    year: int
    value: float
    indicator_code: str = INDICATOR_CODE

    @classmethod
    def create(
        cls,
        *,
        source_key: str,
        country_iso3: str,
        year: object,
        value: object,
    ) -> Observation:
        """Normalise raw source fields into an observation.

        Raises ``RecordRejected`` if any field is unusable, so a caller can
        count the rejection and move to the next row.
        """
        if not source_key:
            raise RecordRejected("Observation is missing its source attribution")

        return cls(
            source_key=source_key,
            country_iso3=normalize_iso3(country_iso3),
            year=normalize_year(year),
            value=normalize_rate(value),
        )

    @property
    def natural_key(self) -> tuple[str, str, int, str]:
        """What makes two observations the same fact.

        A source is allowed to revise a figure, so re-reading a source must
        update the existing row rather than insert a second one. This tuple is
        what the unique constraint and the upsert both key on.
        """
        return (self.source_key, self.country_iso3, self.year, self.indicator_code)


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """What one source produced in one run.

    Rejections are carried alongside the good records rather than raised,
    because "nine hundred rows loaded, three rejected" is a successful run whose
    shape an operator should still be able to see.

    ``skipped`` is counted separately from ``rejections`` and the distinction is
    the point. The HTML source publishes 193 countries and this pipeline tracks
    ten; the other 183 are out of scope, not broken. Folding them into the
    rejection count would bury the three rows that genuinely failed to parse
    under a number that is large every single run and means nothing.
    """

    observations: tuple[Observation, ...]
    rejections: tuple[RecordRejected, ...]
    skipped: int = 0

    @property
    def extracted_count(self) -> int:
        """In-scope rows the source offered, good and bad together."""
        return len(self.observations) + len(self.rejections)
