from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolProjection:
    """The immutable set of tools visible to one model step."""

    step: int
    visible: frozenset[str]
    base: tuple[dict[str, Any], ...]
    extra: tuple[dict[str, Any], ...]

    def contains(self, name: str) -> bool:
        return name in self.visible
