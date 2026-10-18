from __future__ import annotations

import json
from dataclasses import replace

import pytest

from kore_next.bench.swebench_driver import build_prompt, image_for, load_instances, parse_args, select_pending
from kore_next.config import Settings
from kore_next.tools import ToolExecutor, builtin_tool_definitions, shell_command_argv


def test_build_prompt_contains_issue_and_guardrails():
    row = {
        "instance_id": "demo__demo-1",
        "repo": "demo/demo",
        "base_commit": "abc123",
        "problem_statement": "value is None and crashes",
    }
    prompt = build_prompt(row)
    assert "demo/demo" in prompt
    assert "abc123" in prompt
    assert "value is None and crashes" in prompt
    assert "Do not install new packages" in prompt


def test_image_for_prefers_dataset_image_and_supports_template():
    row = {"instance_id": "demo__demo-1", "repo": "demo/demo", "image": "official/image:latest"}
    assert image_for(row, None) == "official/image:latest"
    assert image_for(row, "mirror/{instance_id}:latest") == "mirror/demo__demo-1:latest"


def test_load_instances_from_jsonl(tmp_path):
    path = tmp_path / "instances.jsonl"
    rows = [
        {"instance_id": "a__a-1", "repo": "a/a", "base_commit": "1", "problem_statement": "one"},
        {"instance_id": "b__b-2", "repo": "b/b", "base_commit": "2", "problem_statement": "two"},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    args = parse_args(["--dataset-file", str(path), "--limit", "1"])
    loaded = load_instances(args)
    assert [row["instance_id"] for row in loaded] == ["a__a-1"]
    offset = parse_args(["--dataset-file", str(path), "--offset", "1", "--limit", "1"])
    assert [row["instance_id"] for row in load_instances(offset)] == ["b__b-2"]


def test_shell_command_argv_wraps_docker_exec():
    settings = replace(
        Settings(),
        shell_backend="docker",
        shell_container="kore-task-1",
        shell_bootstrap="source /opt/miniconda3/bin/activate",
    )
    argv = shell_command_argv(settings, "pytest -q tests/test_demo.py")
    assert argv[:6] == ["docker", "exec", "-w", "/testbed", "kore-task-1", "bash"]
    assert argv[-2] == "-lc"
    assert "source /opt/miniconda3/bin/activate" in argv[-1]
    assert "pytest -q tests/test_demo.py" in argv[-1]


def test_shell_command_argv_uses_configured_docker_prefix():
    settings = replace(
        Settings(),
        shell_backend="docker",
        shell_container="kore-task-1",
        docker_command="wsl.exe -e docker",
    )
    argv = shell_command_argv(settings, "true")
    assert argv[:3] == ["wsl.exe", "-e", "docker"]


async def test_file_tools_accept_container_repo_paths(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("value = 1\n", encoding="utf-8")
    settings = Settings(workspace_dir=tmp_path, data_dir=tmp_path / "data", user_skills_dir=tmp_path / "skills")
    executor = ToolExecutor(settings, None, None, None, None, None)
    content = await executor.execute("run", "read_file", {"path": "/testbed/pkg/mod.py"}, "call", mode="plan")
    assert content == "value = 1\n"
    listing = await executor.execute("run", "list_files", {"path": "/testbed/pkg"}, "call", mode="plan")
    assert "mod.py" in listing


def test_web_tools_can_be_disabled_for_offline_benchmarks():
    names = {item["function"]["name"] for item in builtin_tool_definitions("normal", web_tools=False)}
    assert "web_search" not in names
    assert "http_request" not in names
    assert "shell" in names


def test_resume_skips_saved_non_empty_patches_only():
    rows = [{"instance_id": "a"}, {"instance_id": "b"}, {"instance_id": "c"}]
    predictions = [
        {"instance_id": "a", "model_patch": "diff --git a/x b/x"},
        {"instance_id": "b", "model_patch": "  "},
        {"instance_id": "c"},
    ]
    assert [row["instance_id"] for row in select_pending(rows, predictions, True)] == ["b", "c"]
    assert len(select_pending(rows, predictions, False)) == 3


@pytest.mark.parametrize("container", ["", "-bad", "../bad", "bad name"])
def test_shell_command_argv_rejects_invalid_container(container):
    settings = replace(Settings(), shell_backend="docker", shell_container=container)
    with pytest.raises(ValueError, match="container name"):
        shell_command_argv(settings, "echo hi")
