"""Run KoreAgent against SWE-bench instances and emit a predictions file.

The official SWE-bench harness never launches KoreAgent. This module is the
missing inference-side driver: it prepares each instance container, gives the
issue statement to KoreAgent, collects the repository diff, and writes the
``predictions.json`` file consumed by ``swebench eval``.

The host process talks to the task container through ``docker exec``. The
container is disposable and is the isolation boundary; file tools operate on a
host directory bind-mounted at ``/testbed`` so both views stay in sync.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from ..application import Application, RpcFault
from ..config import Settings
from ..secrets import SecretStore

DATASET_SERVER = "https://datasets-server.huggingface.co/rows"
DEFAULT_DATASET = "SWE-bench/SWE-bench_Verified"
CONTAINER_REPO = "/testbed"

PATCH_EXCLUDES = """__pycache__/
*.py[cod]
.pytest_cache/
.mypy_cache/
.ruff_cache/
*.egg-info/
.coverage
htmlcov/
.tox/
build/
dist/
"""

SYSTEM_HINT = """You are resolving a real GitHub issue in the repository at /testbed.

Work autonomously:
1. Read the issue and locate the relevant code.
2. Reproduce the problem with existing tests or a small scratch script.
3. Implement a minimal, correct fix inside the repository.
4. Run the most relevant tests to verify the fix.
5. Stop when the issue is addressed. Do not ask the user questions.

Constraints:
- The shell tool runs inside the task container at /testbed with the task's Python environment.
- Do not modify existing test files; add new scratch tests outside the repository if needed.
- Do not install new packages or change dependency manifests.
- Keep the change focused and do not reformat unrelated code.
- The evaluator applies the git diff from /testbed; your final message should briefly summarize the change.

Repository: {repo}
Base commit: {base_commit}

<issue>
{problem_statement}
</issue>
"""


class DriverError(RuntimeError):
    pass


def log(message: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def _run(
    command: list[str],
    *,
    timeout: float | None = None,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            capture_output=capture,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise DriverError(f"Executable not found: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise DriverError(f"Command timed out after {timeout:g}s: {' '.join(command[:4])}") from exc
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-2000:]
        raise DriverError(f"{' '.join(command[:4])} failed ({result.returncode}): {detail}")
    return result


class DockerCli:
    """Docker CLI plus host-path translation.

    On Windows the Docker Desktop CLI may point at a stopped engine while a
    WSL distro exposes a working daemon. The driver can use either entrypoint;
    paths must be translated when commands run through ``wsl.exe``.
    """

    def __init__(self, prefix: list[str], *, wsl: bool = False):
        self.prefix = prefix
        self.wsl = wsl

    @classmethod
    def detect(cls) -> DockerCli:
        override = os.getenv("KORENEXT_DOCKER_COMMAND", "").strip()
        candidates: list[tuple[list[str], bool]] = []
        if override:
            candidates.append((shlex.split(override), False))
        if os.name == "nt":
            candidates.append((["docker"], False))
            candidates.append((["wsl.exe", "-e", "docker"], True))
        else:
            candidates.append((["docker"], False))
        errors = []
        for prefix, wsl in candidates:
            probe = _run([*prefix, "version", "--format", "{{.Server.Version}}"], check=False)
            if probe.returncode == 0:
                return cls(prefix, wsl=wsl)
            errors.append((probe.stderr or probe.stdout or "").strip())
        raise DriverError(f"Docker is not available: {'; '.join(errors)}")

    def run(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return _run([*self.prefix, *args], **kwargs)

    def local_path(self, path: Path) -> str:
        resolved = path.resolve()
        if not self.wsl:
            return str(resolved)
        drive = resolved.drive
        if drive:
            return f"/mnt/{drive[0].lower()}{resolved.as_posix()[2:]}"
        return resolved.as_posix()

    def server_version(self) -> str:
        result = self.run(["version", "--format", "{{.Server.Version}}"], check=False)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise DriverError(f"Docker is not available: {detail}")
        return result.stdout.strip()


def _load_local_rows(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("rows") or data.get("data") or []
    if isinstance(data, list) and data and isinstance(data[0], dict) and "row" in data[0]:
        data = [item["row"] for item in data]
    if not isinstance(data, list):
        raise DriverError(f"Unsupported dataset file layout: {path}")
    return [item for item in data if isinstance(item, dict)]


def _fetch_rows(dataset: str, config: str, split: str, max_rows: int | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    page = 100
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        while True:
            response = client.get(
                DATASET_SERVER,
                params={
                    "dataset": dataset,
                    "config": config,
                    "split": split,
                    "offset": offset,
                    "length": page,
                },
            )
            response.raise_for_status()
            payload = response.json()
            batch = [item.get("row") or {} for item in payload.get("rows", [])]
            rows.extend(batch)
            total = int(payload.get("num_rows_total") or 0)
            offset += len(batch)
            if not batch or (max_rows is not None and len(rows) >= max_rows):
                break
            if total and offset >= total:
                break
    return rows[:max_rows] if max_rows is not None else rows


def load_instances(args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.dataset_file:
        rows = _load_local_rows(Path(args.dataset_file).expanduser())
    else:
        wanted = set(args.instance_ids or [])
        rows = _fetch_rows(args.dataset, args.config, args.split, None if wanted else args.offset + args.limit)
    if args.instance_ids:
        wanted = set(args.instance_ids)
        rows = [row for row in rows if str(row.get("instance_id")) in wanted]
        order = {name: index for index, name in enumerate(args.instance_ids)}
        rows.sort(key=lambda row: order.get(str(row.get("instance_id")), 10**9))
    if not args.instance_ids:
        rows = rows[args.offset : args.offset + args.limit]
    for row in rows:
        missing = [key for key in ("instance_id", "repo", "base_commit", "problem_statement") if not row.get(key)]
        if missing:
            raise DriverError(f"Instance {row.get('instance_id')} is missing fields: {', '.join(missing)}")
    return rows


def image_for(row: dict[str, Any], template: str | None) -> str:
    image = str(row.get("image") or "").strip()
    if template:
        return template.format(instance_id=row["instance_id"], repo=row["repo"], image=image)
    if image:
        return image
    # Older dataset exports may omit the image column.
    return f"swebench/sweb.eval.x86_64.{row['instance_id']}:latest"


def build_prompt(row: dict[str, Any]) -> str:
    return SYSTEM_HINT.format(
        repo=row["repo"],
        base_commit=row["base_commit"],
        problem_statement=str(row["problem_statement"]).strip(),
    )


def model_label(settings: Settings) -> str:
    raw = f"koreagent-{settings.provider}-{settings.model}"
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip("-") or "koreagent"


def select_pending(rows: list[dict[str, Any]], predictions: list[dict[str, Any]], resume: bool) -> list[dict[str, Any]]:
    if not resume:
        return rows
    saved = {
        str(item.get("instance_id"))
        for item in predictions
        if isinstance(item, dict) and str(item.get("model_patch") or "").strip()
    }
    return [row for row in rows if str(row["instance_id"]) not in saved]


def saved_model_settings(settings: Settings) -> dict[str, Any]:
    """Read the desktop's saved model selection without modifying the database."""
    if not settings.database.is_file():
        return {}
    try:
        with sqlite3.connect(f"{settings.database.as_uri()}?mode=ro", uri=True, timeout=5) as connection:
            row = connection.execute("SELECT value_json FROM kv WHERE key='model_settings'").fetchone()
    except sqlite3.Error:
        return {}
    if not row:
        return {}
    try:
        value = json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def resolve_settings(settings: Settings, args: argparse.Namespace) -> Settings:
    """Prefer explicit CLI overrides, then the desktop's saved provider and key."""
    saved = saved_model_settings(settings)
    providers = {
        str(item.get("provider")): item
        for item in saved.get("providers", [])
        if isinstance(item, dict) and item.get("provider")
    }
    provider = args.provider or str(saved.get("default_provider") or settings.provider)
    item = providers.get(provider, {})
    model = args.model or str(item.get("model") or settings.model)
    base_url = args.base_url or str(item.get("base_url") or settings.base_url)
    api_key = settings.api_key or SecretStore(settings.data_dir / "settings").get(provider)
    return replace(settings, provider=provider, model=model, base_url=base_url, api_key=api_key)


def _container_name(instance_id: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", instance_id).strip("-")[:40]
    return f"kore-{slug}-{uuid.uuid4().hex[:8]}"


def _remove_container(docker: DockerCli, name: str) -> None:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
        docker.run(["rm", "-f", name], check=False)


def ensure_image(docker: DockerCli, image: str, pull: bool) -> None:
    probe = docker.run(["image", "inspect", image], check=False)
    if probe.returncode == 0:
        return
    if not pull:
        raise DriverError(f"Docker image is missing: {image}")
    log(f"pulling {image} (this can take several minutes)")
    docker.run(["pull", image], timeout=3600)


def prepare_instance(
    docker: DockerCli,
    image: str,
    workspace: Path,
    container: str,
    network: str,
    *,
    reuse: bool = False,
) -> None:
    can_reuse = (
        reuse and (workspace / ".git" / "HEAD").is_file() and any(item.name != ".git" for item in workspace.iterdir())
    )
    if reuse and workspace.exists() and not can_reuse:
        raise DriverError(f"Cannot reuse an incomplete workspace: {workspace}")
    if not can_reuse:
        if workspace.exists() and any(workspace.iterdir()):
            raise DriverError(f"Workspace is not empty: {workspace}")
        workspace.mkdir(parents=True, exist_ok=True)
        seed = f"{container}-seed"
        _remove_container(docker, seed)
        docker.run(["create", "--name", seed, image])
        try:
            docker.run(["cp", f"{seed}:{CONTAINER_REPO}/.", docker.local_path(workspace)], timeout=1800)
        finally:
            _remove_container(docker, seed)
    _remove_container(docker, container)
    mount = f"type=bind,source={docker.local_path(workspace)},target={CONTAINER_REPO}"
    docker.run(
        [
            "run",
            "-d",
            "--name",
            container,
            "--entrypoint",
            "sleep",
            "--mount",
            mount,
            "--network",
            network,
            image,
            "infinity",
        ],
        timeout=300,
    )
    check = docker.run(
        ["exec", container, "bash", "-lc", f"test -d {CONTAINER_REPO}/.git && echo ok"],
        check=False,
    )
    if check.returncode != 0:
        raise DriverError(f"{image} does not contain a git checkout at {CONTAINER_REPO}")
    docker.run(
        [
            "exec",
            container,
            "bash",
            "-lc",
            f"cd {CONTAINER_REPO} && git config --global --add safe.directory {CONTAINER_REPO} && "
            "git config core.fileMode false",
        ]
    )


def checkout_head(docker: DockerCli, container: str) -> str:
    return docker.run(["exec", container, "git", "-C", CONTAINER_REPO, "rev-parse", "HEAD"]).stdout.strip()


def checkout_base(docker: DockerCli, container: str, base_commit: str, *, clean: bool = False) -> None:
    probe = docker.run(
        ["exec", container, "git", "-C", CONTAINER_REPO, "cat-file", "-t", base_commit],
        check=False,
    )
    if probe.returncode != 0:
        raise DriverError(f"Image does not contain base commit {base_commit}")
    if clean:
        docker.run(["exec", container, "git", "-C", CONTAINER_REPO, "clean", "-fd"])
    docker.run(["exec", container, "git", "-C", CONTAINER_REPO, "checkout", "--force", base_commit])
    docker.run(["exec", container, "git", "-C", CONTAINER_REPO, "reset", "--hard"])


def collect_patch(docker: DockerCli, container: str, base_commit: str) -> str:
    script = (
        f"cd {CONTAINER_REPO}\n"
        "mkdir -p .git/info\n"
        "cat >> .git/info/exclude <<'KORE_EXCLUDE'\n"
        f"{PATCH_EXCLUDES}"
        "KORE_EXCLUDE\n"
        "git add -A\n"
        f"git diff --cached --binary {base_commit}\n"
    )
    return docker.run(["exec", container, "bash", "-lc", script], timeout=300).stdout


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    temporary.replace(path)


def _event_summary(path: Path) -> dict[str, Any]:
    """Extract benchmark diagnostics from the recorded run events."""
    model_calls = 0
    tool_calls = 0
    blocked_tool_calls = 0
    tool_seconds = 0.0
    tokens = 0
    last_step = 0
    first_edit_step: int | None = None
    final_message = ""
    if not path.is_file():
        return {
            "model_calls": 0,
            "tool_calls": 0,
            "blocked_tool_calls": 0,
            "tool_seconds": 0.0,
            "tokens": 0,
            "steps": 0,
            "first_edit_step": None,
            "final_message": "",
        }
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line).get("params") or {}
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        step = event.get("step")
        if isinstance(step, int):
            last_step = max(last_step, step)
        if kind == "model_request":
            model_calls += 1
        elif kind == "tool_call":
            tool_calls += 1
        elif kind == "tool_result":
            result = event.get("tool_result") or {}
            tool_seconds += float(result.get("duration_ms") or 0) / 1000
            error = str(result.get("error") or "")
            if "benchmark mode" in error.lower() or "benchmark_policy" in error.lower():
                blocked_tool_calls += 1
        elif kind == "first_edit" and first_edit_step is None:
            first_edit_step = event.get("step")
        elif kind == "agent_completed":
            final_message = str((event.get("message") or {}).get("content") or "")
        elif kind in {"run_budget_warning", "run_budget_finalizing", "run_budget_exceeded"}:
            chargeable = event.get("run_budget_chargeable_tokens")
            if chargeable is not None:
                tokens = max(tokens, int(chargeable))
        usage = event.get("usage")
        if isinstance(usage, dict) and usage.get("total_tokens"):
            tokens = max(tokens, int(usage["total_tokens"]))
    return {
        "model_calls": model_calls,
        "tool_calls": tool_calls,
        "blocked_tool_calls": blocked_tool_calls,
        "tool_seconds": round(tool_seconds, 1),
        "tokens": tokens,
        "steps": last_step,
        "first_edit_step": first_edit_step,
        "final_message": final_message[:2000],
    }


async def run_agent(
    row: dict[str, Any],
    *,
    base_settings: Settings,
    workspace: Path,
    state_dir: Path,
    container: str,
    events_path: Path,
    timeout: float,
    secrets_source: Path | None = None,
) -> dict[str, Any]:
    if secrets_source and secrets_source.is_file():
        target = state_dir / "settings" / "secrets.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copyfile(secrets_source, target)
    settings = replace(
        base_settings,
        workspace_dir=workspace,
        data_dir=state_dir,
        user_skills_dir=state_dir / "user-skills",
        auto_approve=True,
        shell_backend="docker",
        shell_container=container,
        shell_filesystem="host",
        shell_network="allowed",
        web_tools_enabled=False,
    )
    app = Application(settings)
    events_path.parent.mkdir(parents=True, exist_ok=True)
    stream = events_path.open("w", encoding="utf-8", newline="\n")

    async def record(method: str, params: dict[str, Any]) -> None:
        event = {"at": datetime.now(UTC).isoformat(), "method": method, "params": params}
        stream.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        stream.flush()

    app.broadcast = record
    run_id: str | None = None
    run_record: dict[str, Any] = {}
    status = "completed"
    error: str | None = None
    started = time.monotonic()
    try:
        await app.start()
        conversation = (await app.conversation_create({"title": f"SWE-bench {row['instance_id']}"}))["conversation"]
        result = await asyncio.wait_for(
            app.conversation_send({"conversation_id": conversation["id"], "content": build_prompt(row)}),
            timeout=timeout,
        )
        run_id = str(result["run"]["id"])
    except TimeoutError:
        status, error = "timeout", f"Agent exceeded {timeout:g}s"
    except RpcFault as exc:
        status, error = "failed", str(exc)
        if isinstance(exc.data, dict):
            run_id = exc.data.get("run_id")
    except Exception as exc:  # noqa: BLE001 - benchmark runs must record failures and continue
        status, error = "error", f"{type(exc).__name__}: {exc}"
    finally:
        usage: dict[str, Any] = {}
        if run_id:
            try:
                run_record = (await app.run_get({"run_id": run_id}))["run"]
                usage = dict(run_record.get("usage") or {})
            except Exception:  # noqa: BLE001
                usage = {}
        await app.close()
        # Let aiosqlite worker threads observe the close before the loop ends.
        await asyncio.sleep(0.2)
        stream.close()
    diagnostics = _event_summary(events_path)
    return {
        "status": status,
        "error": error,
        "run_id": run_id,
        "stop_reason": run_record.get("stop_reason"),
        "usage": usage,
        **diagnostics,
        "steps": run_record.get("steps") or diagnostics.get("steps"),
        "duration_seconds": round(time.monotonic() - started, 1),
    }


def run_instance(
    row: dict[str, Any],
    args: argparse.Namespace,
    base_settings: Settings,
    run_dir: Path,
    docker: DockerCli,
    secrets_source: Path | None,
) -> dict[str, Any]:
    instance_id = str(row["instance_id"])
    image = image_for(row, args.image_template)
    container = _container_name(instance_id)
    workspace = (
        Path(args.workspace_dir).expanduser().resolve() if args.workspace_dir else run_dir / "workspaces" / instance_id
    )
    state_dir = run_dir / "state" / instance_id
    events_path = run_dir / "logs" / f"{instance_id}.events.jsonl"
    patch_path = run_dir / "patches" / f"{instance_id}.patch"
    result: dict[str, Any] = {
        "instance_id": instance_id,
        "repo": row["repo"],
        "base_commit": row["base_commit"],
        "image": image,
        "container": container,
        "status": "error",
        "error": None,
        "duration_seconds": 0.0,
    }
    started = time.monotonic()
    try:
        ensure_image(docker, image, args.pull)
        reuse = bool(args.workspace_dir) or args.reuse_workspace
        prepare_instance(docker, image, workspace, container, args.container_network, reuse=reuse)
        # Official images are built at an environment-setup commit, not always
        # at the issue's base commit; the evaluator resets to base before
        # applying a patch, so the agent must see the same snapshot.
        checkout_base(docker, container, str(row["base_commit"]), clean=reuse)
        head = checkout_head(docker, container)
        if head != row["base_commit"]:
            log(f"warning: {instance_id} image HEAD {head[:12]} != base_commit {row['base_commit'][:12]}")
        log(f"agent started for {instance_id}")
        agent_result = asyncio.run(
            run_agent(
                row,
                base_settings=base_settings,
                workspace=workspace,
                state_dir=state_dir,
                container=container,
                events_path=events_path,
                timeout=args.timeout,
                secrets_source=secrets_source,
            )
        )
        result.update(agent_result)
    except Exception as exc:  # noqa: BLE001 - keep the batch alive
        result["status"] = "error"
        result["error"] = f"{type(exc).__name__}: {exc}"
    try:
        patch = collect_patch(docker, container, str(row["base_commit"]))
    except Exception as exc:  # noqa: BLE001
        patch = ""
        result["error"] = (result.get("error") or "") + f" | patch collection failed: {exc}"
    if not patch.strip():
        result["status"] = "empty_patch"
        result["error"] = (result.get("error") or "") + " | empty patch"
    stop_reason = str(result.get("stop_reason") or "")
    if not patch.strip():
        result["failure_class"] = "empty_patch"
    elif stop_reason in {"run_budget", "max_steps"}:
        result["failure_class"] = "budget_exhausted"
    elif result.get("status") == "timeout":
        result["failure_class"] = "timeout"
    elif result.get("status") != "completed":
        result["failure_class"] = "run_error"
    else:
        result["failure_class"] = "patch_generated"
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_text(patch, encoding="utf-8", newline="\n")
    result["patch_path"] = str(patch_path)
    result["patch_bytes"] = len(patch.encode("utf-8"))
    result["duration_seconds"] = round(time.monotonic() - started, 1)
    if not args.keep_containers:
        _remove_container(docker, container)
    if not args.keep_workspaces and not args.workspace_dir:
        shutil.rmtree(workspace, ignore_errors=True)
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run KoreAgent on SWE-bench instances and write predictions.json")
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--config", default="default")
    parser.add_argument("--split", default="test")
    parser.add_argument("--dataset-file", help="Local JSON/JSONL export; skips the HuggingFace API")
    parser.add_argument("--instance-ids", nargs="*", default=[], help="Only run these instance ids")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--offset", type=int, default=0, help="Skip the first N instances (useful for batches)")
    parser.add_argument("--image-template", help="Format string with {instance_id}, {repo}, {image}")
    parser.add_argument("--no-pull", dest="pull", action="store_false", help="Fail instead of pulling missing images")
    parser.add_argument("--run-dir", help="Directory for workspaces, logs and patches")
    parser.add_argument("--output", help="predictions.json path (defaults to <run-dir>/predictions.json)")
    parser.add_argument("--model")
    parser.add_argument("--provider")
    parser.add_argument("--base-url")
    parser.add_argument("--max-steps", type=int, default=40)
    parser.add_argument("--warning-tokens", type=int, default=180_000)
    parser.add_argument("--finalization-tokens", type=int, default=240_000)
    parser.add_argument("--hard-tokens", type=int, default=300_000)
    parser.add_argument("--shell-timeout-cap", type=float, default=120)
    parser.add_argument("--shell-output-limit", type=int, default=12000)
    parser.add_argument("--timeout", type=float, default=1800, help="Per-instance agent timeout in seconds")
    parser.add_argument(
        "--container-network",
        default="none",
        choices=["none", "bridge"],
        help="Task container network; none keeps benchmark inference offline",
    )
    parser.add_argument("--keep-containers", action="store_true")
    parser.add_argument("--keep-workspaces", action="store_true")
    parser.add_argument(
        "--workspace-dir",
        help="Use one existing checkout for every instance (fast image reuse; env must match)",
    )
    parser.add_argument(
        "--reuse-workspace",
        action="store_true",
        help="Reuse an existing checkout in the run directory instead of copying from the image",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip instances that already have a non-empty patch in --output",
    )
    parser.add_argument("--dry-run", action="store_true", help="Load and print instances without starting Docker")
    parser.add_argument("--check-images", action="store_true", help="With --dry-run, verify images exist locally")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    try:
        rows = load_instances(args)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"Failed to load instances: {exc}") from exc
    if args.dry_run:
        for row in rows:
            log(f"{row['instance_id']} image={image_for(row, args.image_template)} prompt={len(build_prompt(row))}B")
        if args.check_images:
            docker = DockerCli.detect()
            for row in rows:
                image = image_for(row, args.image_template)
                exists = docker.run(["image", "inspect", image], check=False).returncode == 0
                log(f"{'found' if exists else 'missing'} {image}")
        return

    docker = DockerCli.detect()
    version = docker.server_version()
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    settings = resolve_settings(Settings(), args)
    secrets_source = settings.data_dir / "settings" / "secrets.json"
    if not settings.api_key:
        raise SystemExit(
            f"No API key for provider {settings.provider}. "
            "Save one in KoreAgent settings or set KORENEXT_<PROVIDER>_API_KEY."
        )
    steps = max(args.max_steps, 4)
    if not (0 < args.warning_tokens < args.finalization_tokens < args.hard_tokens):
        raise SystemExit("Token budgets must satisfy warning < finalization < hard")
    warning_calls = max(2, int(steps * 0.5))
    finalization_calls = max(warning_calls + 1, int(steps * 0.9))
    hard_calls = max(finalization_calls + 1, steps)
    settings = replace(
        settings,
        docker_command=shlex.join(docker.prefix),
        benchmark_mode=True,
        benchmark_shell_timeout=args.shell_timeout_cap,
        benchmark_shell_output_limit=args.shell_output_limit,
        max_steps=steps,
        run_warning_model_calls=warning_calls,
        run_finalization_model_calls=finalization_calls,
        run_hard_model_calls=hard_calls,
        run_warning_tokens=args.warning_tokens,
        run_finalization_tokens=args.finalization_tokens,
        run_hard_tokens=args.hard_tokens,
    )
    run_dir = Path(args.run_dir).expanduser().resolve() if args.run_dir else settings.data_dir / "bench" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    output = Path(args.output).expanduser().resolve() if args.output else run_dir / "predictions.json"
    label = model_label(settings)
    existing_predictions: list[dict[str, Any]] = []
    existing_results: list[dict[str, Any]] = []
    if args.resume:
        if not output.is_file():
            raise SystemExit(f"--resume requires an existing predictions file: {output}")
        existing_predictions = json.loads(output.read_text(encoding="utf-8"))
        if not isinstance(existing_predictions, list):
            raise SystemExit(f"Predictions must be a JSON list: {output}")
        summary_path = run_dir / "summary.json"
        if summary_path.is_file():
            existing_results = list(json.loads(summary_path.read_text(encoding="utf-8")).get("results") or [])
    pending = select_pending(rows, existing_predictions, args.resume)
    log(f"run_id={run_id} docker={version} model={settings.provider}/{settings.model}")
    log(f"instances={len(rows)} pending={len(pending)} run_dir={run_dir}")
    if args.resume and len(pending) < len(rows):
        log(f"skipping {len(rows) - len(pending)} instances with saved patches")
    _write_json(
        run_dir / "run.json",
        {
            "run_id": run_id,
            "dataset": args.dataset if not args.dataset_file else args.dataset_file,
            "split": args.split,
            "provider": settings.provider,
            "model": settings.model,
            "model_label": label,
            "max_steps": settings.max_steps,
            "timeout_seconds": args.timeout,
            "instances": [row["instance_id"] for row in rows],
        },
    )

    predictions_by_id = {str(item.get("instance_id")): item for item in existing_predictions if isinstance(item, dict)}
    summaries_by_id = {str(item.get("instance_id")): item for item in existing_results if isinstance(item, dict)}
    for index, row in enumerate(pending, start=1):
        log(f"[{index}/{len(pending)}] {row['instance_id']}")
        result = run_instance(row, args, settings, run_dir, docker, secrets_source)
        summaries_by_id[row["instance_id"]] = result
        predictions_by_id[row["instance_id"]] = {
            "instance_id": row["instance_id"],
            "model_name_or_path": label,
            "model_patch": Path(result["patch_path"]).read_text(encoding="utf-8"),
        }
        _write_json(output, list(predictions_by_id.values()))
        _write_json(run_dir / "summary.json", {"run_id": run_id, "results": list(summaries_by_id.values())})
        log(
            f"  status={result['status']} patch={result['patch_bytes']}B "
            f"duration={result['duration_seconds']}s error={result.get('error') or '-'}"
        )
    summaries = list(summaries_by_id.values())
    failed = [item for item in summaries if item["status"] != "completed"]
    log(f"done: {len(summaries) - len(failed)}/{len(summaries)} completed; predictions={output}")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main(sys.argv[1:])
