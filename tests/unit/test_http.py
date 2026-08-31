"""The retry budget.

Driven through ``httpx.MockTransport`` with an injected ``sleep``, so the
backoff is asserted rather than waited for. A test suite that actually slept
seven seconds to prove exponential backoff would be a test suite people skip.
"""

from __future__ import annotations

import httpx
import pytest

from labour_etl.domain.errors import SourceUnavailable
from labour_etl.sources.http import USER_AGENT, fetch_text


def fetch(
    handler: object,
    *,
    max_retries: int = 2,
    delays: list[float] | None = None,
) -> str:
    client = httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    try:
        return fetch_text(
            source_key="test_source",
            url="https://example.test/data",
            timeout=5.0,
            max_retries=max_retries,
            client=client,
            sleep=(delays.append if delays is not None else lambda _: None),
        )
    finally:
        client.close()


def test_returns_the_body_on_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="hello")

    assert fetch(handler) == "hello"


def test_sends_a_user_agent_that_identifies_the_client() -> None:
    # Wikimedia returns 403 to clients that do not say who they are, and the
    # default 'python-httpx' is exactly the kind it throttles first.
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("user-agent", ""))
        return httpx.Response(200, text="ok")

    client = httpx.Client(
        transport=httpx.MockTransport(handler), headers={"User-Agent": USER_AGENT}
    )
    try:
        fetch_text(
            source_key="test_source",
            url="https://example.test/",
            timeout=5.0,
            max_retries=0,
            client=client,
        )
    finally:
        client.close()

    assert "labour-etl-pipeline" in seen[0]
    assert "github.com" in seen[0]


def test_retries_a_transient_server_error_and_then_succeeds() -> None:
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, text="recovered")

    assert fetch(handler, max_retries=3) == "recovered"
    assert attempts["count"] == 3


def test_backs_off_exponentially_between_attempts() -> None:
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    with pytest.raises(SourceUnavailable):
        fetch(handler, max_retries=3, delays=delays)

    assert delays == [1.0, 2.0, 4.0]


def test_does_not_retry_a_status_that_will_not_change() -> None:
    # Retrying a 404 three times just spends the budget and delays the report.
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(404)

    with pytest.raises(SourceUnavailable, match="HTTP 404"):
        fetch(handler, max_retries=3)

    assert attempts["count"] == 1


def test_retries_a_connection_failure() -> None:
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, text="second time lucky")

    assert fetch(handler, max_retries=2) == "second time lucky"


def test_gives_up_after_the_budget_and_reports_the_last_reason() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow")

    with pytest.raises(SourceUnavailable) as caught:
        fetch(handler, max_retries=1)

    assert caught.value.source_key == "test_source"
    assert "2 attempt(s) failed" in caught.value.reason
    assert "ReadTimeout" in caught.value.reason


def test_zero_retries_means_one_attempt() -> None:
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(500)

    with pytest.raises(SourceUnavailable):
        fetch(handler, max_retries=0)

    assert attempts["count"] == 1
