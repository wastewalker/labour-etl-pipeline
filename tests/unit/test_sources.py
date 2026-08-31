"""The three extractors, driven against saved fixtures.

The fixtures under ``tests/fixtures`` are real responses, trimmed. That matters:
a hand-written fixture only ever contains the shapes its author remembered, and
the whole difficulty of this pipeline is the shapes nobody remembers - the en
dash standing in for a missing figure, the narrow no-break space before a
footnote asterisk, the two-level table header.

Malformed inputs are written inline instead, because deliberately corrupting a
fixture would make it lie about what the source actually publishes.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from labour_etl.config import Config
from labour_etl.domain.errors import SourceUnavailable
from labour_etl.sources.owid_csv import OwidCsvSource
from labour_etl.sources.wikipedia_html import WikipediaHtmlSource
from labour_etl.sources.world_bank import WorldBankSource

FIXTURES = Path(__file__).parent.parent / "fixtures"

TRACKED = frozenset({"BOL", "PER", "CHL", "ARG", "BRA"})


@pytest.fixture
def config() -> Config:
    return Config(
        database_url="postgres://unused",
        country_filter=TRACKED,
        min_year=2018,
        http_max_retries=0,
    )


def client_returning(handler: object) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


class TestWorldBankSource:
    def test_reads_every_page_of_a_paginated_response(self, config: Config) -> None:
        page_one = json.loads((FIXTURES / "world_bank_page.json").read_text(encoding="utf-8"))
        # The fixture is page 1 of 2. Page 2 is synthesised so pagination is
        # genuinely exercised rather than assumed.
        page_two = [
            {"page": 2, "pages": 2, "per_page": 12, "total": 16},
            [
                {"countryiso3code": "BOL", "date": "2017", "value": 4.0},
                {"countryiso3code": "PER", "date": "2017", "value": 4.1},
            ],
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            page = request.url.params.get("page")
            return httpx.Response(200, json=page_one if page == "1" else page_two)

        with client_returning(handler) as client:
            result = WorldBankSource(client).extract(config)

        # Page 1 holds 12 in-scope rows; page 2's two rows are below min_year.
        assert len(result.observations) == 12
        assert result.skipped == 2
        assert {o.source_key for o in result.observations} == {"world_bank_api"}

    def test_counts_a_null_figure_as_out_of_scope_not_as_a_rejection(self, config: Config) -> None:
        # A null is the API saying "no figure for this year", which is normal.
        payload = [
            {"page": 1, "pages": 1},
            [
                {"countryiso3code": "BOL", "date": "2020", "value": None},
                {"countryiso3code": "BOL", "date": "2021", "value": 5.2},
            ],
        ]

        with client_returning(lambda r: httpx.Response(200, json=payload)) as client:
            result = WorldBankSource(client).extract(config)

        assert len(result.observations) == 1
        assert result.rejections == ()
        assert result.skipped == 1

    def test_rejects_a_row_whose_value_is_not_a_number(self, config: Config) -> None:
        payload = [
            {"page": 1, "pages": 1},
            [{"countryiso3code": "BOL", "date": "2020", "value": "twelve"}],
        ]

        with client_returning(lambda r: httpx.Response(200, json=payload)) as client:
            result = WorldBankSource(client).extract(config)

        assert result.observations == ()
        assert len(result.rejections) == 1
        assert result.skipped == 0

    def test_treats_an_error_object_as_the_source_being_unavailable(self, config: Config) -> None:
        # The API answers 200 with this shape when it dislikes the request. A
        # parser that trusted the envelope would read it as zero rows and record
        # a successful load of nothing.
        payload = [{"message": [{"id": "120", "value": "The provided parameter is invalid"}]}]

        with (
            client_returning(lambda r: httpx.Response(200, json=payload)) as client,
            pytest.raises(SourceUnavailable, match="unexpected response envelope"),
        ):
            WorldBankSource(client).extract(config)

    def test_treats_a_non_json_body_as_the_source_being_unavailable(self, config: Config) -> None:
        with (
            client_returning(lambda r: httpx.Response(200, text="<html>oops</html>")) as client,
            pytest.raises(SourceUnavailable, match="not JSON"),
        ):
            WorldBankSource(client).extract(config)

    def test_asks_only_for_the_countries_it_wants(self, config: Config) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url.path))
            return httpx.Response(200, json=[{"page": 1, "pages": 1}, []])

        with client_returning(handler) as client:
            WorldBankSource(client).extract(config)

        assert "ARG;BOL;BRA;CHL;PER" in seen[0]


class TestOwidCsvSource:
    def test_parses_the_fixture_and_filters_to_the_configured_scope(self, config: Config) -> None:
        text = (FIXTURES / "owid_unemployment.csv").read_text(encoding="utf-8")
        result = OwidCsvSource().parse(text, config)

        assert result.observations
        assert result.rejections == ()
        assert {o.country_iso3 for o in result.observations} <= TRACKED
        assert all(o.year >= 2018 for o in result.observations)
        # Everything filtered out - other countries, earlier years, aggregates -
        # is out of scope, not rejected.
        assert result.skipped > 0

    def test_ignores_aggregate_entities(self, config: Config) -> None:
        csv = "entity,code,year,sl_uem_totl_zs\nWorld,WLD,2020,6.5\nBolivia,BOL,2020,7.9\n"
        result = OwidCsvSource().parse(csv, config)

        assert [o.country_iso3 for o in result.observations] == ["BOL"]
        assert result.rejections == ()

    def test_ignores_rows_with_no_country_code(self, config: Config) -> None:
        csv = "entity,code,year,sl_uem_totl_zs\nEuropean Union (27),,2020,6.5\n"
        result = OwidCsvSource().parse(csv, config)

        assert result.observations == ()
        assert result.rejections == ()

    def test_rejects_an_in_scope_row_whose_value_is_unparseable(self, config: Config) -> None:
        csv = "entity,code,year,sl_uem_totl_zs\nBolivia,BOL,2020,not-a-number\n"
        result = OwidCsvSource().parse(csv, config)

        assert result.observations == ()
        assert len(result.rejections) == 1

    def test_treats_a_renamed_column_as_the_source_being_unavailable(self, config: Config) -> None:
        # Loading whatever columns survived a rename would produce a
        # plausible-looking partial result, which is worse than failing.
        csv = "entity,code,year,unemployment\nBolivia,BOL,2020,7.9\n"

        with pytest.raises(SourceUnavailable, match="schema changed"):
            OwidCsvSource().parse(csv, config)

    def test_keeps_the_year_as_an_integer(self, config: Config) -> None:
        # Type inference would make this column a float and stringify 2020 as
        # '2020.0', which the year parser then rejects.
        csv = "entity,code,year,sl_uem_totl_zs\nBolivia,BOL,2020,7.9\n"
        result = OwidCsvSource().parse(csv, config)

        assert result.observations[0].year == 2020


class TestWikipediaHtmlSource:
    def test_parses_the_fixture_table(self, config: Config) -> None:
        html = (FIXTURES / "wikipedia_unemployment.html").read_text(encoding="utf-8")
        result = WikipediaHtmlSource().parse(html, config)

        assert {o.country_iso3 for o in result.observations} <= TRACKED
        assert result.observations

    def test_reads_the_year_out_of_the_column_header(self, config: Config) -> None:
        # The year is metadata in a sub-header cell, not a constant. When the
        # article is refreshed the pipeline follows without a code change.
        html = (FIXTURES / "wikipedia_unemployment.html").read_text(encoding="utf-8")
        result = WikipediaHtmlSource().parse(html, config)

        years = {o.year for o in result.observations}
        assert len(years) == 1
        assert 2020 <= years.pop() <= 2030

    def test_ignores_countries_outside_the_remit(self, config: Config) -> None:
        html = (FIXTURES / "wikipedia_unemployment.html").read_text(encoding="utf-8")
        result = WikipediaHtmlSource().parse(html, config)

        assert result.skipped > 0
        assert result.rejections == ()

    def test_fails_when_the_target_column_is_gone(self, config: Config) -> None:
        # Silently falling back to the neighbouring column would attribute a
        # different publisher's methodology to this source's data.
        html = """
        <table class="wikitable">
          <tr><th rowspan="2">Country</th><th colspan="2">Unemployment rate (%)</th></tr>
          <tr><th>WB [6] (2024)</th><th>IMF [7] (2026)</th></tr>
          <tr><td>Bolivia</td><td>3.1</td><td>3.4</td></tr>
        </table>
        """

        with pytest.raises(SourceUnavailable, match="no longer in the table header"):
            WikipediaHtmlSource().parse(html, config)

    def test_fails_when_there_is_no_table_at_all(self, config: Config) -> None:
        with pytest.raises(SourceUnavailable, match="no wikitable"):
            WikipediaHtmlSource().parse("<html><body><p>Moved.</p></body></html>", config)

    def test_fails_when_the_table_holds_no_readable_rows(self, config: Config) -> None:
        # The header still parses but every row is unreadable - a layout change
        # that a row-level skip would hide indefinitely.
        html = """
        <table class="wikitable">
          <tr><th rowspan="2">Country</th><th colspan="1">Unemployment rate (%)</th></tr>
          <tr><th>CIA [5] (2024)</th></tr>
          <tr><td colspan="2">Section: Americas</td></tr>
        </table>
        """

        with pytest.raises(SourceUnavailable, match="no observations"):
            WikipediaHtmlSource().parse(html, config)

    def test_counts_a_dashed_cell_as_missing_rather_than_broken(self, config: Config) -> None:
        html = """
        <table class="wikitable">
          <tr><th rowspan="2">Country</th><th colspan="1">Unemployment rate (%)</th></tr>
          <tr><th>CIA [5] (2024)</th></tr>
          <tr><td>Bolivia</td><td>&ndash;</td></tr>
          <tr><td>Peru</td><td>4.9</td></tr>
        </table>
        """
        result = WikipediaHtmlSource().parse(html, config)

        assert [o.country_iso3 for o in result.observations] == ["PER"]
        assert result.rejections == ()
        assert result.skipped == 1


class TestExtractorEdgeCases:
    """Failure paths that only a malformed response reaches."""

    def test_world_bank_rejects_a_row_that_is_not_an_object(self, config: Config) -> None:
        payload = [
            {"page": 1, "pages": 1},
            ["not-a-record", {"countryiso3code": "BOL", "date": "2020", "value": 5.0}],
        ]

        with client_returning(lambda r: httpx.Response(200, json=payload)) as client:
            result = WorldBankSource(client).extract(config)

        assert len(result.observations) == 1
        assert len(result.rejections) == 1

    def test_world_bank_fails_when_the_envelope_holds_the_wrong_shapes(
        self, config: Config
    ) -> None:
        # Two elements, so the length check passes, but neither is what the
        # contract promises.
        payload = ["metadata", "records"]

        with (
            client_returning(lambda r: httpx.Response(200, json=payload)) as client,
            pytest.raises(SourceUnavailable, match="metadata and rows"),
        ):
            WorldBankSource(client).extract(config)

    def test_world_bank_skips_an_aggregate_that_slips_into_the_response(
        self, config: Config
    ) -> None:
        payload = [
            {"page": 1, "pages": 1},
            [
                {"countryiso3code": "WLD", "date": "2020", "value": 6.5},
                {"countryiso3code": "BOL", "date": "2020", "value": 7.9},
            ],
        ]

        with client_returning(lambda r: httpx.Response(200, json=payload)) as client:
            result = WorldBankSource(client).extract(config)

        assert [o.country_iso3 for o in result.observations] == ["BOL"]
        assert result.skipped == 1
        assert result.rejections == ()

    def test_wikipedia_rejects_a_row_with_an_empty_country_cell(self, config: Config) -> None:
        html = """
        <table class="wikitable">
          <tr><th rowspan="2">Country</th><th colspan="1">Unemployment rate (%)</th></tr>
          <tr><th>CIA [5] (2024)</th></tr>
          <tr><td>[1]</td><td>4.9</td></tr>
          <tr><td>Peru</td><td>4.9</td></tr>
        </table>
        """
        result = WikipediaHtmlSource().parse(html, config)

        assert [o.country_iso3 for o in result.observations] == ["PER"]
        assert len(result.rejections) == 1

    def test_wikipedia_rejects_an_unparseable_value_for_a_tracked_country(
        self, config: Config
    ) -> None:
        html = """
        <table class="wikitable">
          <tr><th rowspan="2">Country</th><th colspan="1">Unemployment rate (%)</th></tr>
          <tr><th>CIA [5] (2024)</th></tr>
          <tr><td>Bolivia</td><td>about four</td></tr>
        </table>
        """
        result = WikipediaHtmlSource().parse(html, config)

        assert result.observations == ()
        assert len(result.rejections) == 1

    def test_wikipedia_skips_a_row_with_too_few_cells(self, config: Config) -> None:
        html = """
        <table class="wikitable">
          <tr><th rowspan="2">Country</th><th colspan="1">Unemployment rate (%)</th></tr>
          <tr><th>CIA [5] (2024)</th></tr>
          <tr><td colspan="2">Americas</td></tr>
          <tr><td>Bolivia</td><td>3.1</td></tr>
        </table>
        """
        result = WikipediaHtmlSource().parse(html, config)

        assert [o.country_iso3 for o in result.observations] == ["BOL"]
        assert result.skipped == 1

    def test_owid_reports_an_empty_body_as_unavailable(self, config: Config) -> None:
        with pytest.raises(SourceUnavailable):
            OwidCsvSource().parse("", config)
