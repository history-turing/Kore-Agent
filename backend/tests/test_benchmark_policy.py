from __future__ import annotations

import pytest

from kore_next.tools.policies import (
    is_mutating_shell_command,
    is_mutating_tool,
    shell_policy_violation,
)


@pytest.mark.parametrize(
    "command",
    [
        "cd /testbed && python -m pytest testing -q",
        "pytest -q",
        "python -m pytest tests",
        "git cat-file --batch-all-objects --batch-check",
        "find / -name '*.py'",
    ],
)
def test_benchmark_policy_blocks_broad_commands(command):
    assert shell_policy_violation(command)


@pytest.mark.parametrize(
    "command",
    [
        "cd /testbed && python -m pytest astropy/modeling/tests/test_separable.py -q",
        "pytest src/_pytest/logging.py::test_clear",
        "python -m pytest astropy/modeling/tests -q",
        "rg -n 'separability' astropy/modeling",
        "git diff --stat",
    ],
)
def test_benchmark_policy_allows_targeted_commands(command):
    assert shell_policy_violation(command) is None


def test_mutation_detection_covers_common_edit_paths():
    assert is_mutating_shell_command("sed -i 's/a/b/' src/file.py")
    assert is_mutating_shell_command("python - <<'EOF'\nopen('src/file.py', 'w').write('x')\nEOF")
    assert is_mutating_shell_command("git apply fix.patch")
    assert not is_mutating_shell_command("python -m pytest src/test_file.py -q")
    assert not is_mutating_shell_command("command 2>&1")


def test_mutating_tool_detection():
    assert is_mutating_tool("write_file", {"path": "x"})
    assert is_mutating_tool("shell", {"command": "sed -i 's/a/b/' x"})
    assert not is_mutating_tool("read_file", {"path": "x"})

