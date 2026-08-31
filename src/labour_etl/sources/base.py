"""What every source has to be.

Sources differ in transport (HTTP JSON, HTTP CSV, HTML) and in how badly they
are shaped, but the runner should not know any of that. It sees three objects
with the same two questions: who are you, and what did you find?
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal

from ..config import Config
from ..domain.records import ExtractionResult

SourceKind = Literal["rest_api", "csv", "html"]


class Source(ABC):
    """One upstream publisher of the indicator.

    ``key`` is the stable identifier stored on every observation, so it must
    never change once data has been loaded under it - it is half of the natural
    key, and renaming it would orphan every row.
    """

    key: str
    name: str
    url: str
    kind: SourceKind

    @abstractmethod
    def extract(self, config: Config) -> ExtractionResult:
        """Read the source and return observations plus per-record rejections.

        Implementations raise ``SourceUnavailable`` when the source as a whole
        could not be read, and collect ``RecordRejected`` per bad row instead of
        letting one row abort the rest.
        """

    def __repr__(self) -> str:
        return f"<{type(self).__name__} key={self.key!r}>"
