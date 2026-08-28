"""Small runtime compatibility shims for the supported Python 3.10+ range."""

from __future__ import annotations

try:  # Python 3.11+
    from enum import StrEnum as StrEnum
except ImportError:  # pragma: no cover - covered through an isolated import test
    from enum import Enum

    class StrEnum(str, Enum):
        """Python 3.10-compatible subset of :class:`enum.StrEnum`."""

        def __str__(self) -> str:
            return str(self.value)
