"""The canonical record."""

from __future__ import annotations

import pytest

from labour_etl.domain.errors import RecordRejected
from labour_etl.domain.records import INDICATOR_CODE, ExtractionResult, Observation


def make(**overrides: object) -> Observation:
    fields: dict[str, object] = {
        "source_key": "test_source",
        "country_iso3": "BOL",
        "year": "2024",
        "value": "7.4",
    }
    fields.update(overrides)
    return Observation.create(**fields)  # type: ignore[arg-type]


def test_normalises_every_field_on_the_way_in() -> None:
    observation = make(country_iso3="bol", year=" 2024 ", value="7,4 %")

    assert observation.country_iso3 == "BOL"
    assert observation.year == 2024
    assert observation.value == pytest.approx(7.4)
    assert observation.indicator_code == INDICATOR_CODE


def test_requires_a_source_attribution() -> None:
    # An observation with no source cannot be reconciled against anything, and
    # its natural key would collide with every other source's.
    with pytest.raises(RecordRejected, match="source attribution"):
        make(source_key="")


def test_is_immutable() -> None:
    with pytest.raises(AttributeError):
        make().value = 9.9  # type: ignore[misc]


def test_the_natural_key_is_what_makes_two_records_the_same_fact() -> None:
    first = make(value="7.4")
    revised = make(value="7.9")

    # Same country, year and source: the second is a revision of the first, not
    # a new observation. This is exactly the tuple the upsert conflicts on.
    assert first.natural_key == revised.natural_key


def test_different_sources_do_not_collide() -> None:
    assert make(source_key="a").natural_key != make(source_key="b").natural_key


class TestExtractionResult:
    def test_counts_only_in_scope_rows_as_extracted(self) -> None:
        result = ExtractionResult(
            observations=(make(),),
            rejections=(RecordRejected("bad"),),
            skipped=183,
        )

        # 183 countries this pipeline does not track are not 183 problems.
        assert result.extracted_count == 2

    def test_defaults_to_nothing_skipped(self) -> None:
        assert ExtractionResult(observations=(), rejections=()).skipped == 0
