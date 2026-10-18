from __future__ import annotations

from pathlib import Path

import pytest

from kore_next.workspace import WorkspaceContext, WorkspaceError, resolve_workspace


def test_empty_workspace_remains_empty():
    workspace = resolve_workspace(None)
    assert workspace == WorkspaceContext.none()
    assert workspace.path is None
    assert workspace.name is None
    assert workspace.available is False


def test_workspace_path_is_canonicalized(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    workspace = resolve_workspace(root, source="conversation")
    assert workspace.root == root.resolve()
    assert workspace.path == str(root.resolve())
    assert workspace.name == "project"
    assert workspace.source == "conversation"


def test_workspace_requires_absolute_existing_directory(tmp_path: Path):
    with pytest.raises(WorkspaceError, match="absolute"):
        resolve_workspace("relative")
    with pytest.raises(WorkspaceError, match="does not exist"):
        resolve_workspace(tmp_path / "missing")
    file_path = tmp_path / "file.txt"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="not a directory"):
        resolve_workspace(file_path)
