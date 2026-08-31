"""World Bank Indicators API - the REST/JSON source.

The API wraps its payload in a two-element array: metadata first, then the
records. That envelope is validated before anything is read out of it, because
the failure mode this guards against is not a network error - it is the API
answering 200 with an error object, which a trusting parser reads as "zero rows"
and loads as a successful, empty run.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from ..config import Config
from ..domain.errors import RecordRejected, SourceUnavailable, ValueMissing
from ..domain.normalize import KNOWN_AGGREGATE_CODES
from ..domain.records import ExtractionResult, Observation
from .base import Source, SourceKind
from .http import fetch_text

LOGGER = logging.getLogger(__name__)

INDICATOR_ID = "SL.UEM.TOTL.ZS"
BASE_URL = "https://api.worldbank.org/v2"

# The API caps per_page at 32767. A thousand keeps each response small enough to
# read in a terminal when something goes wrong, and these queries return a few
# hundred rows in total.
PAGE_SIZE = 1000

# A runaway pagination loop against a live API is worse than an incomplete load.
MAX_PAGES = 50


class WorldBankSource(Source):
    key = "world_bank_api"
    name = "World Bank Indicators API"
    url = f"{BASE_URL}/country/{{countries}}/indicator/{INDICATOR_ID}"
    kind: SourceKind = "rest_api"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client

    def _country_segment(self, config: Config) -> str:
        # Asking for exactly the countries we want is both faster and safer than
        # fetching 'all' and discarding: the response cannot contain a country
        # we never asked about.
        if not config.country_filter:
            return "all"
        return ";".join(sorted(config.country_filter))

    def _fetch_page(self, config: Config, page: int) -> list[Any]:
        url = self.url.format(countries=self._country_segment(config))
        body = fetch_text(
            source_key=self.key,
            url=url,
            timeout=config.http_timeout_seconds,
            max_retries=config.http_max_retries,
            params={
                "format": "json",
                "per_page": str(PAGE_SIZE),
                "page": str(page),
                "date": f"{config.min_year}:2100",
            },
            client=self._client,
        )

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise SourceUnavailable(self.key, f"response was not JSON: {exc}") from exc

        if not isinstance(payload, list) or len(payload) < 2:
            # This is the shape the API uses to report its own errors, and the
            # shape it would take if the contract ever changed. Either way the
            # response cannot be read, and pretending it holds zero rows would
            # quietly report a successful load of nothing.
            raise SourceUnavailable(self.key, f"unexpected response envelope: {body[:200]}")

        return payload

    def extract(self, config: Config) -> ExtractionResult:
        observations: list[Observation] = []
        rejections: list[RecordRejected] = []
        skipped = 0

        page = 1
        while page <= MAX_PAGES:
            payload = self._fetch_page(config, page)
            meta, records = payload[0], payload[1]

            if not isinstance(meta, dict) or not isinstance(records, list):
                raise SourceUnavailable(self.key, "envelope did not hold metadata and rows")

            for record in records:
                if not isinstance(record, dict):
                    rejections.append(RecordRejected("Row is not an object", record))
                    continue

                code = str(record.get("countryiso3code", "")).strip().upper()

                # Aggregates and countries we do not track are out of scope, not
                # broken data. They must not inflate the rejection count.
                if code in KNOWN_AGGREGATE_CODES or not config.wants(code):
                    skipped += 1
                    continue

                try:
                    observations.append(
                        Observation.create(
                            source_key=self.key,
                            country_iso3=code,
                            year=record.get("date"),
                            value=record.get("value"),
                        )
                    )
                except ValueMissing:
                    # A null value is the API's normal way of saying "no figure
                    # for this year", not a defect worth an operator's attention.
                    skipped += 1
                except RecordRejected as rejection:
                    rejections.append(rejection)

            total_pages = meta.get("pages")
            if not isinstance(total_pages, int) or page >= total_pages:
                break
            page += 1
        else:
            LOGGER.warning(
                "Source %s stopped at the %d page limit; the load may be incomplete",
                self.key,
                MAX_PAGES,
            )

        return ExtractionResult(
            observations=tuple(observations),
            rejections=tuple(rejections),
            skipped=skipped,
        )
