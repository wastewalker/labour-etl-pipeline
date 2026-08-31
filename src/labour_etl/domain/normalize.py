"""Turning three sources' spellings of the same fact into one shape.

Every function here raises ``RecordRejected`` rather than returning ``None`` on
bad input. That is deliberate: a rejection carries a reason, and the reason is
what makes the run ledger worth reading six months later.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from .errors import RecordRejected

# The earliest year any of the three sources publishes for this indicator.
MIN_SUPPORTED_YEAR = 1960

ISO3_PATTERN = re.compile(r"^[A-Z]{3}$")

# The World Bank's country endpoint returns regional and income aggregates
# alongside actual countries, using codes that look exactly like ISO-3166
# alpha-3. Loading them as if they were countries is the single easiest way to
# end up double counting: "Latin America & Caribbean" contains Bolivia, so a
# naive sum over the table counts Bolivia twice.
KNOWN_AGGREGATE_CODES = frozenset(
    {
        "AFE", "AFW", "ARB", "CAF", "CEB", "CHI", "CSS", "EAP", "EAR", "EAS",
        "ECA", "ECS", "EMU", "EUU", "FCS", "HIC", "HPC", "IBD", "IBT", "IDA",
        "IDB", "IDX", "INX", "LAC", "LCN", "LDC", "LIC", "LMC", "LMY", "LTE",
        "MEA", "MIC", "MNA", "MMR", "NAC", "OED", "OSS", "PRE", "PSS", "PST",
        "SAS", "SSA", "SSF", "SST", "TEA", "TEC", "TLA", "TMN", "TSA", "TSS",
        "UMC", "WLD",
    }
)

# Country names as the HTML source spells them, mapped to ISO-3166 alpha-3.
#
# This table doubles as the scope of the HTML source: it lists every country the
# pipeline tracks, and a name absent from it is treated as out of scope rather
# than guessed at. A wrong guess here would silently attribute one country's
# unemployment rate to another, which is worse than not loading the row.
COUNTRY_NAME_TO_ISO3 = {
    "argentina": "ARG",
    "bolivia": "BOL",
    "brazil": "BRA",
    "chile": "CHL",
    "colombia": "COL",
    "ecuador": "ECU",
    "mexico": "MEX",
    "paraguay": "PRY",
    "peru": "PER",
    "uruguay": "URY",
}


def normalize_iso3(raw: str) -> str:
    """Validate an ISO-3166 alpha-3 code and reject aggregates."""
    code = raw.strip().upper()

    if not ISO3_PATTERN.match(code):
        raise RecordRejected(f"'{raw}' is not an ISO-3166 alpha-3 country code", raw)

    if code in KNOWN_AGGREGATE_CODES:
        raise RecordRejected(
            f"'{code}' is a regional or income aggregate, not a country", raw
        )

    return code


# Rendered HTML brings along typography that is invisible to a reader and fatal
# to a dictionary lookup: narrow and non-breaking spaces, footnote markers, and
# a trailing asterisk marking a linked article ("Bolivia *").
_NAME_NOISE = re.compile(r"\[[^\]]*\]|[*   ​]")


def clean_country_name(raw: str) -> str:
    """Strip rendering artefacts from a country name read out of an HTML table."""
    return re.sub(r"\s+", " ", _NAME_NOISE.sub(" ", raw)).strip()


def iso3_from_country_name(raw: str) -> str | None:
    """Map a country name to its alpha-3 code.

    Returns ``None`` when the name is not one this pipeline tracks. That is a
    scope decision, not a data error: the HTML source publishes every country in
    the world, and quietly ignoring the ones outside our remit is a different
    thing from rejecting a row we ought to have been able to read. Only the
    second kind belongs in the rejection count.

    Deliberately a lookup table and not a fuzzy match. A near-miss that resolves
    'Guinea-Bissau' to 'Guinea' produces data that is wrong rather than missing,
    and wrong data does not announce itself.
    """
    cleaned = clean_country_name(raw).lower()
    if not cleaned:
        raise RecordRejected("Country name is empty", raw)

    return COUNTRY_NAME_TO_ISO3.get(cleaned)


def normalize_year(raw: object) -> int:
    """Parse a year from whatever the source used to express it."""
    if isinstance(raw, bool):
        # bool is an int subclass, and True would otherwise parse as year 1.
        raise RecordRejected(f"'{raw}' is not a year", raw)

    try:
        year = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise RecordRejected(f"'{raw}' is not a year", raw) from exc

    current_year = datetime.now(UTC).year
    # One year ahead is allowed: sources publish a projection for the current
    # year, and around New Year their clock and ours disagree.
    if not MIN_SUPPORTED_YEAR <= year <= current_year + 1:
        raise RecordRejected(
            f"Year {year} is outside the supported range "
            f"{MIN_SUPPORTED_YEAR}-{current_year + 1}",
            raw,
        )

    return year


# Values arrive as '7.4', '7,4', '7.4%', '7.4 %' or with a footnote attached.
_RATE_CLEANUP = re.compile("[%\\s\u00a0\u202f]|\\[[^\\]]*\\]")

# Placeholders the three sources use for "no figure". The dashes are the
# ones that matter: the HTML table renders a missing value as an EN DASH
# (U+2013), not the ASCII hyphen anyone writes when guessing at this list.
_MISSING_MARKERS = frozenset(
    {
        "",
        "-",
        "--",
        "..",
        "n/a",
        "na",
        "nan",
        "none",
        "null",
        "\u2013",  # en dash, what the HTML source uses
        "\u2014",  # em dash
        "\u2212",  # minus sign
    }
)


def normalize_rate(raw: object) -> float:
    """Parse an unemployment rate expressed as a percentage.

    Rejects anything outside 0-100. A rate of 740 is a value that was
    multiplied by 100 one time too many, and storing it would poison every
    average computed downstream.
    """
    if raw is None:
        raise RecordRejected("Rate is missing", raw)

    text = _RATE_CLEANUP.sub("", str(raw))

    if text.strip().lower() in _MISSING_MARKERS:
        raise RecordRejected("Rate is missing", raw)

    # Some sources use a comma as the decimal separator. This is only safe
    # because the value is a percentage: there is no thousands separator to
    # confuse it with, since no unemployment rate reaches 1,000.
    text = text.replace(",", ".")

    try:
        value = float(text)
    except ValueError as exc:
        raise RecordRejected(f"'{raw}' is not a number", raw) from exc

    if value != value or value in (float("inf"), float("-inf")):  # NaN or infinity
        raise RecordRejected(f"'{raw}' is not a finite number", raw)

    if not 0.0 <= value <= 100.0:
        raise RecordRejected(
            f"Unemployment rate {value} is outside the plausible range 0-100", raw
        )

    # Six decimals is far more precision than any of these sources publishes,
    # and it stops float noise from making two identical figures compare unequal.
    return round(value, 6)
