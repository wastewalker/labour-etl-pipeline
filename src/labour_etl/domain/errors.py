"""Error taxonomy.

The whole design of this pipeline rests on one distinction, so it is made
explicit in the type system rather than left to a comment:

``RecordRejected``
    One row is unusable. The other rows are fine. Count it, record why, and
    keep going. A source that publishes one malformed row out of nine hundred
    has not failed, and treating it as a failure would mean losing the eight
    hundred and ninety-nine good ones.

``SourceUnavailable``
    The source itself could not be read or made sense of - the host is down,
    the response is not the document we were promised, the schema changed
    underneath us. Nothing from this source can be trusted, so its load is
    abandoned and rolled back. Other sources are unaffected.

Getting these the wrong way round is the classic ETL failure: either one bad
row aborts a nightly load, or a source that started returning an HTML error
page quietly overwrites good data with nothing.
"""

from __future__ import annotations


class EtlError(Exception):
    """Base class for everything this package raises deliberately."""


class ConfigurationError(EtlError):
    """The pipeline cannot start: bad or missing configuration."""


class SourceUnavailable(EtlError):
    """A source could not be read. Its load is abandoned and rolled back.

    Carries the source key so the run ledger can attribute the failure without
    the caller having to remember which source it was extracting.
    """

    def __init__(self, source_key: str, reason: str) -> None:
        super().__init__(f"Source '{source_key}' is unavailable: {reason}")
        self.source_key = source_key
        self.reason = reason


class RecordRejected(EtlError):
    """One record is unusable. Counted and logged; the load continues.

    ``raw`` keeps the offending input so a rejection can be diagnosed from the
    ledger without re-running the extraction against a source that may have
    changed in the meantime.
    """

    def __init__(self, reason: str, raw: object = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.raw = raw
