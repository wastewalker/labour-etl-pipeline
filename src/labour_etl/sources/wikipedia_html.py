"""Wikipedia's unemployment table - the HTML source.

This is the fragile one, and that is the point of including it. Scraping a
rendered table means depending on a layout nobody promised to keep, so it is the
source most likely to fail on any given night. The pipeline is built so that
when it does, the other two still load.

Two things about this table make it more than a `<td>` loop:

1. It has a two-level header. The first row spans "Unemployment rate (%)" across
   four columns; the second row names the four publishers and the year each one
   reported - "CIA [5] (2024)". The year is therefore metadata that has to be
   parsed out of a header cell, not a constant to hardcode. When Wikipedia
   refreshes the table to 2025, this picks it up on its own.

2. Those four columns are four different publishers with four different
   methodologies. Loading them as one series would average incompatible figures
   into a number that means nothing, so exactly one column is read - see
   ``PROVIDER`` below.
"""

from __future__ import annotations

import logging
import re

import httpx
from bs4 import BeautifulSoup, Tag

from ..config import Config
from ..domain.errors import RecordRejected, SourceUnavailable, ValueMissing
from ..domain.normalize import iso3_from_country_name
from ..domain.records import ExtractionResult, Observation
from .base import Source, SourceKind
from .http import fetch_text

LOGGER = logging.getLogger(__name__)

ARTICLE_URL = "https://en.wikipedia.org/wiki/List_of_countries_by_unemployment_rate"

# The column to read. The CIA World Factbook is chosen over the neighbouring
# World Bank column deliberately: the World Bank figures are already loaded
# straight from their own API, so taking them again here would give the same
# publisher two votes and make the reconciliation view look like agreement where
# there is only duplication.
PROVIDER = "CIA"

# Sub-header cells look like 'CIA [ 5 ] (2024)' once BeautifulSoup has flattened
# the footnote link.
_HEADER_PATTERN = re.compile(r"^([A-Za-z]{2,6})\s*\((\d{4})\)$")
_FOOTNOTE = re.compile(r"\[[^\]]*\]")


class WikipediaHtmlSource(Source):
    key = "wikipedia_cia"
    name = "Wikipedia list of countries by unemployment rate (CIA column)"
    url = ARTICLE_URL
    kind: SourceKind = "html"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client

    def _download(self, config: Config) -> str:
        return fetch_text(
            source_key=self.key,
            url=self.url,
            timeout=config.http_timeout_seconds,
            max_retries=config.http_max_retries,
            client=self._client,
        )

    def _locate_column(self, header_row: Tag) -> tuple[int, int]:
        """Find the target provider's column and the year it reported.

        Returns ``(offset_within_value_columns, year)``. Raises
        ``SourceUnavailable`` if the provider is gone, because a scraper that
        silently falls back to a different column would attribute the wrong
        publisher's methodology to this source's data.
        """
        cells = header_row.find_all(["th", "td"])

        for offset, cell in enumerate(cells):
            text = _FOOTNOTE.sub("", cell.get_text(" ", strip=True))
            text = re.sub(r"\s+", " ", text).strip()

            match = _HEADER_PATTERN.match(text)
            if match and match.group(1).upper() == PROVIDER:
                return offset, int(match.group(2))

        raise SourceUnavailable(
            self.key,
            f"the '{PROVIDER}' column is no longer in the table header; the article layout changed",
        )

    def parse(self, html: str, config: Config) -> ExtractionResult:
        """Parse an article body into observations.

        Separate from the download so the tests can run it against a saved
        fixture without touching the network.
        """
        soup = BeautifulSoup(html, "lxml")

        table = soup.select_one("table.wikitable")
        if table is None:
            raise SourceUnavailable(self.key, "no wikitable found in the article")

        rows = table.find_all("tr")
        if len(rows) < 3:
            raise SourceUnavailable(
                self.key, f"table has {len(rows)} rows; expected a two-row header plus data"
            )

        offset, year = self._locate_column(rows[1])
        LOGGER.info("Reading the %s column, which reports %d", PROVIDER, year)

        observations: list[Observation] = []
        rejections: list[RecordRejected] = []
        skipped = 0

        for row in rows[2:]:
            cells = row.find_all(["th", "td"])
            # The country cell sits in front of the value columns, so the target
            # value is one past the offset found in the header.
            value_index = offset + 1

            if len(cells) <= value_index:
                # A spanning row, a section divider, or a genuinely truncated
                # row. Either way there is no country-value pair to read.
                skipped += 1
                continue

            country_name = cells[0].get_text(" ", strip=True)

            try:
                iso3 = iso3_from_country_name(country_name)
            except RecordRejected as rejection:
                rejections.append(rejection)
                continue

            if iso3 is None or not config.wants(iso3):
                skipped += 1
                continue

            try:
                observations.append(
                    Observation.create(
                        source_key=self.key,
                        country_iso3=iso3,
                        year=year,
                        value=cells[value_index].get_text(" ", strip=True),
                    )
                )
            except ValueMissing:
                # The table renders "no figure" as an en dash. Normal, not news.
                skipped += 1
            except RecordRejected as rejection:
                rejections.append(rejection)

        if not observations and not rejections:
            # Every row skipped means the table was found but nothing in it was
            # readable - a layout change that did not break the header check.
            # Reporting success here would mask it indefinitely.
            raise SourceUnavailable(
                self.key,
                f"parsed {len(rows) - 2} rows but produced no observations; "
                "the table layout has probably changed",
            )

        return ExtractionResult(
            observations=tuple(observations),
            rejections=tuple(rejections),
            skipped=skipped,
        )

    def extract(self, config: Config) -> ExtractionResult:
        return self.parse(self._download(config), config)
