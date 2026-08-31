"""Normalisation: the layer that decides what the three sources actually said."""

from __future__ import annotations

import pytest

from labour_etl.domain.errors import RecordRejected, ValueMissing
from labour_etl.domain.normalize import (
    clean_country_name,
    iso3_from_country_name,
    normalize_iso3,
    normalize_rate,
    normalize_year,
)


class TestNormalizeIso3:
    def test_accepts_a_well_formed_code(self) -> None:
        assert normalize_iso3("bol") == "BOL"
        assert normalize_iso3("  PER  ") == "PER"

    @pytest.mark.parametrize("raw", ["BO", "BOLI", "B0L", "", "  "])
    def test_rejects_anything_that_is_not_three_letters(self, raw: str) -> None:
        with pytest.raises(RecordRejected):
            normalize_iso3(raw)

    @pytest.mark.parametrize("code", ["WLD", "LCN", "HIC", "EUU"])
    def test_rejects_regional_and_income_aggregates(self, code: str) -> None:
        # These look exactly like country codes. Loading 'Latin America &
        # Caribbean' as a country double counts every country inside it.
        with pytest.raises(RecordRejected, match="aggregate"):
            normalize_iso3(code)


class TestCountryNames:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Bolivia", "Bolivia"),
            ("Bolivia\u202f*", "Bolivia"),  # narrow no-break space + link marker
            ("Brazil[3]", "Brazil"),  # footnote marker
            ("Peru\u00a0*", "Peru"),  # no-break space
            ("  Chile  ", "Chile"),
        ],
    )
    def test_strips_the_artefacts_a_rendered_table_carries(self, raw: str, expected: str) -> None:
        assert clean_country_name(raw) == expected

    def test_maps_a_tracked_country_to_its_code(self) -> None:
        assert iso3_from_country_name("Bolivia\u202f*") == "BOL"
        assert iso3_from_country_name("BRAZIL") == "BRA"

    def test_returns_none_for_a_country_outside_the_remit(self) -> None:
        # Not an error. The HTML source lists every country on earth and this
        # pipeline tracks ten of them; the other 183 are simply not our business.
        assert iso3_from_country_name("Afghanistan\u202f*") is None

    def test_rejects_an_empty_name(self) -> None:
        with pytest.raises(RecordRejected, match="empty"):
            iso3_from_country_name("  [1] ")

    def test_does_not_guess_at_near_misses(self) -> None:
        # 'Bolivarian Republic of Venezuela' must not resolve to Bolivia.
        assert iso3_from_country_name("Bolivarian Republic of Venezuela") is None


class TestNormalizeYear:
    def test_accepts_a_year_as_text_or_number(self) -> None:
        assert normalize_year("2024") == 2024
        assert normalize_year(2024) == 2024
        assert normalize_year(" 2018 ") == 2018

    def test_rejects_a_boolean(self) -> None:
        # bool is a subclass of int, so True would otherwise parse as year 1.
        with pytest.raises(RecordRejected):
            normalize_year(True)

    @pytest.mark.parametrize("raw", ["", "twenty", None, "2024.0", "20 24"])
    def test_rejects_anything_that_is_not_a_year(self, raw: object) -> None:
        with pytest.raises(RecordRejected):
            normalize_year(raw)

    @pytest.mark.parametrize("year", [1959, 1800, 2200])
    def test_rejects_years_outside_the_supported_range(self, year: int) -> None:
        with pytest.raises(RecordRejected, match="outside the supported range"):
            normalize_year(year)


class TestNormalizeRate:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("7.4", 7.4),
            (7.4, 7.4),
            ("7,4", 7.4),  # comma decimal separator
            ("7.4%", 7.4),
            ("7.4 %", 7.4),
            ("13.3[5]", 13.3),  # footnote glued to the value
            ("0", 0.0),
            ("100", 100.0),
        ],
    )
    def test_parses_the_spellings_the_sources_use(self, raw: object, expected: float) -> None:
        assert normalize_rate(raw) == pytest.approx(expected)

    @pytest.mark.parametrize(
        "raw",
        [None, "", "-", "..", "N/A", "\u2013", "\u2014", "\u2212"],
    )
    def test_treats_every_placeholder_as_missing_rather_than_broken(self, raw: object) -> None:
        # The en dash is the one that matters: it is what the HTML table renders
        # for a country with no published figure, and it is not the ASCII hyphen
        # anyone would guess at.
        with pytest.raises(ValueMissing):
            normalize_rate(raw)

    def test_missing_is_a_subclass_of_rejected(self) -> None:
        # Callers that only care "this row produced nothing" still catch it.
        with pytest.raises(RecordRejected):
            normalize_rate("\u2013")

    def test_distinguishes_missing_from_unparseable(self) -> None:
        # A blank cell is the source working; 'twelve' is a parser that needs
        # attention. Collapsing the two means nobody ever looks at either.
        with pytest.raises(RecordRejected) as caught:
            normalize_rate("twelve")
        assert not isinstance(caught.value, ValueMissing)

    @pytest.mark.parametrize("raw", [740, "740", -0.5, 100.1])
    def test_rejects_rates_outside_the_plausible_range(self, raw: object) -> None:
        # 740 is a rate multiplied by 100 one time too many. Storing it poisons
        # every average computed downstream.
        with pytest.raises(RecordRejected, match="plausible range"):
            normalize_rate(raw)

    @pytest.mark.parametrize("raw", [float("nan"), float("inf"), float("-inf")])
    def test_rejects_non_finite_values(self, raw: float) -> None:
        with pytest.raises(RecordRejected):
            normalize_rate(raw)

    def test_rounds_away_float_noise(self) -> None:
        assert normalize_rate(7.4000000001) == 7.4
