"""Shared HTTP fetching with a retry budget.

Every network source goes through here so the retry policy is defined once.
Anything that fails after the budget is exhausted becomes ``SourceUnavailable``,
which is what tells the runner to abandon and roll back that source rather than
load a partial set.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import httpx

from ..domain.errors import SourceUnavailable

LOGGER = logging.getLogger(__name__)

# Public statistical services rate-limit rather than block, and a plain
# 'python-httpx' agent is what gets throttled first. Identifying the client and
# where it comes from is both politer and more reliable.
USER_AGENT = (
    "labour-etl-pipeline/1.0 "
    "(+https://github.com/royer-angulo/labour-etl-pipeline; portfolio project)"
)

# Retrying a 404 or a 400 just wastes the budget: the answer will not change.
# These are the statuses where waiting genuinely helps.
RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

BACKOFF_BASE_SECONDS = 1.0


def fetch_text(
    *,
    source_key: str,
    url: str,
    timeout: float,
    max_retries: int,
    params: dict[str, str] | None = None,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """GET a URL and return its body, retrying transient failures.

    ``client`` and ``sleep`` are injectable so tests can drive the retry logic
    against a stub transport without waiting real seconds for the backoff.
    """
    attempts = max_retries + 1
    last_reason = "no attempt was made"

    owns_client = client is None
    active = client or httpx.Client(
        timeout=timeout,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    )

    try:
        for attempt in range(1, attempts + 1):
            try:
                response = active.get(url, params=params)
            except httpx.HTTPError as exc:
                # Connection refused, DNS failure, read timeout: all worth
                # another attempt, none worth a stack trace in the ledger.
                last_reason = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code < 400:
                    return response.text

                last_reason = f"HTTP {response.status_code}"
                if response.status_code not in RETRYABLE_STATUSES:
                    raise SourceUnavailable(source_key, last_reason)

            if attempt < attempts:
                delay = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                LOGGER.warning(
                    "Source %s attempt %d/%d failed (%s); retrying in %.1fs",
                    source_key,
                    attempt,
                    attempts,
                    last_reason,
                    delay,
                )
                sleep(delay)

        raise SourceUnavailable(
            source_key, f"{attempts} attempt(s) failed, last was {last_reason}"
        )
    finally:
        if owns_client:
            active.close()
