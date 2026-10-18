from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

WorkspaceSource = Literal["automation", "conversation", "run_override", "legacy_default", "none"]


class WorkspaceError(ValueError):
    """Raised when a workspace path cannot be used safely."""


@dataclass(frozen=True)
class WorkspaceContext:
    root: Path | None = None
    source: WorkspaceSource = "none"

    @property
    def path(self) -> str | None:
        return str(self.root) if self.root is not None else None

    @property
    def name(self) -> str | None:
        return self.root.name if self.root is not None else None

    @property
    def available(self) -> bool:
        return self.root is not None

    @classmethod
    def none(cls) -> WorkspaceContext:
        return cls()


def resolve_workspace(
    raw: str | Path | None,
    *,
    source: WorkspaceSource = "conversation",
) -> WorkspaceContext:
    if raw is None or not str(raw).strip():
        return WorkspaceContext.none()

    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise WorkspaceError("Workspace path must be absolute")
    try:
        root = candidate.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceError(f"Workspace does not exist: {candidate}") from exc
    if not root.is_dir():
        raise WorkspaceError(f"Workspace is not a directory: {root}")
    return WorkspaceContext(root=root, source=source)
