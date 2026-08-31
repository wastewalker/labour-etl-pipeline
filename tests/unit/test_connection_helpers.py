"""The narrowing helpers at the database boundary.

``dict_row`` types every column as ``object``, which is honest - the driver
genuinely does not know. These two functions are how the few places that read a
number narrow it without widening the connection type to ``Any`` and losing that
honesty everywhere.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from labour_etl.db.connection import as_int, to_float


class TestToFloat:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [(Decimal("7.4"), 7.4), (7.4, 7.4), (7, 7.0), (Decimal("0"), 0.0)],
    )
    def test_converts_what_a_numeric_column_can_return(
        self, value: object, expected: float
    ) -> None:
        assert to_float(value) == pytest.approx(expected)

    @pytest.mark.parametrize("value", ["7.4", None, [7.4]])
    def test_refuses_anything_else(self, value: object) -> None:
        # Better a loud failure here than a string silently concatenating its
        # way through arithmetic downstream.
        with pytest.raises(TypeError, match="expected a number"):
            to_float(value)


class TestAsInt:
    def test_narrows_an_integer(self) -> None:
        assert as_int(42) == 42

    def test_refuses_a_boolean(self) -> None:
        # bool is a subclass of int, and a primary key is never True.
        with pytest.raises(TypeError):
            as_int(True)

    @pytest.mark.parametrize("value", ["42", 4.2, None])
    def test_refuses_anything_that_is_not_an_integer(self, value: object) -> None:
        with pytest.raises(TypeError, match="expected an integer"):
            as_int(value)
