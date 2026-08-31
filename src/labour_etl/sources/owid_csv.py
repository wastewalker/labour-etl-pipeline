"""Our World in Data - the CSV source.

This is the one place pandas earns its keep: the download is every country for
every year since 1991, and column-wise filtering before we ever build a Python
object is both faster and clearer than a loop with three ``continue``
statements in it.

Everything after the filter goes through the same normalisation as the other
two sources. Pandas gets the data down to the rows we care about; it does not
get to decide what a valid observation is.
"""

from __future__ import annotations

import logging
from io import StringIO

import httpx
import pandas as pd

from ..config import Config
from ..domain.errors import RecordRejected, SourceUnavailable
from ..domain.normalize import ISO3_PATTERN, KNOWN_AGGREGATE_CODES
from ..domain.records import ExtractionResult, Observation
from .base import Source, SourceKind
from .http import fetch_text

LOGGER = logging.getLogger(__name__)

CSV_URL = "https://ourworldindata.org/grapher/unemployment-rate.csv"

ENTITY_COLUMN = "entity"
CODE_COLUMN = "code"
YEAR_COLUMN = "year"
VALUE_COLUMN = "sl_uem_totl_zs"

REQUIRED_COLUMNS = (ENTITY_COLUMN, CODE_COLUMN, YEAR_COLUMN, VALUE_COLUMN)


class OwidCsvSource(Source):
    key = "owid_csv"
    name = "Our World in Data (unemployment rate)"
    url = CSV_URL
    kind: SourceKind = "csv"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client

    def _download(self, config: Config) -> str:
        return fetch_text(
            source_key=self.key,
            url=self.url,
            timeout=config.http_timeout_seconds,
            max_retries=config.http_max_retries,
            params={"v": "1", "csvType": "full", "useColumnShortNames": "true"},
            client=self._client,
        )

    def parse(self, csv_text: str, config: Config) -> ExtractionResult:
        """Parse a CSV body into observations. Separate from the download so the
        tests can exercise the parsing against a fixture with no network."""
        try:
            # Everything is read as text on purpose. Letting pandas infer types
            # means a column of mostly-numbers with one stray value silently
            # becomes an object column, and the year becomes a float that
            # stringifies as '2024.0'.
            frame = pd.read_csv(
                StringIO(csv_text),
                dtype=str,
                keep_default_na=False,
            )
        except (pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
            raise SourceUnavailable(self.key, f"CSV could not be parsed: {exc}") from exc

        missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
        if missing:
            # The publisher renamed or dropped a column. Loading whatever
            # remains would produce a plausible-looking partial result, so this
            # is a source failure rather than a pile of rejections.
            raise SourceUnavailable(
                self.key,
                f"expected columns {missing} are absent; the CSV schema changed",
            )

        total_rows = len(frame)

        # Filter in pandas, before building any objects. `code` is empty for
        # aggregate entities like 'World' and 'European Union (27)'.
        in_scope = frame[
            frame[CODE_COLUMN].str.fullmatch(ISO3_PATTERN.pattern.strip("^$"), na=False)
            & ~frame[CODE_COLUMN].isin(KNOWN_AGGREGATE_CODES)
        ]
        if config.country_filter:
            in_scope = in_scope[in_scope[CODE_COLUMN].isin(config.country_filter)]

        # Year is text at this point, so compare numerically rather than
        # lexicographically - '999' would otherwise sort above '2010'.
        years = pd.to_numeric(in_scope[YEAR_COLUMN], errors="coerce")
        in_scope = in_scope[years >= config.min_year]

        observations: list[Observation] = []
        rejections: list[RecordRejected] = []

        for row in in_scope.itertuples(index=False):
            try:
                observations.append(
                    Observation.create(
                        source_key=self.key,
                        country_iso3=str(getattr(row, CODE_COLUMN)),
                        year=getattr(row, YEAR_COLUMN),
                        value=getattr(row, VALUE_COLUMN),
                    )
                )
            except RecordRejected as rejection:
                rejections.append(rejection)

        return ExtractionResult(
            observations=tuple(observations),
            rejections=tuple(rejections),
            skipped=total_rows - len(in_scope),
        )

    def extract(self, config: Config) -> ExtractionResult:
        return self.parse(self._download(config), config)
