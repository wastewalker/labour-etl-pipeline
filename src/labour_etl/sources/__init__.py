"""The source registry.

Adding a source means writing a class and adding it to this tuple. The runner
takes its sources as an argument rather than importing this directly, so tests
can hand it fakes without patching module state.
"""

from __future__ import annotations

from .base import Source, SourceKind
from .owid_csv import OwidCsvSource
from .wikipedia_html import WikipediaHtmlSource
from .world_bank import WorldBankSource

__all__ = [
    "OwidCsvSource",
    "Source",
    "SourceKind",
    "WikipediaHtmlSource",
    "WorldBankSource",
    "default_sources",
]


def default_sources() -> tuple[Source, ...]:
    """The three sources the scheduled run reads.

    Ordered most-reliable-first, so a run that is going to fail on the fragile
    HTML scrape has already banked the other two by the time it gets there.
    """
    return (WorldBankSource(), OwidCsvSource(), WikipediaHtmlSource())
