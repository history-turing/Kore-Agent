from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from .errors import ToolError
from .policies import PROGRAM_READ_TOOLS

MAX_CALLS = 32
MAX_SOURCE_BYTES = 20_000
MAX_OUTPUT_BYTES = 200_000


def _json_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


async def _call_tool(
    executor: Any,
    prepared: Any,
    name: str,
    arguments: Any,
    index: int,
) -> Any:
    if name not in PROGRAM_READ_TOOLS:
        raise ToolError(
            f"Tool {name} is not allowed inside a program; only read-only tools are available",
            code="not_allowed",
        )
    if name == "http_request":
        method = str(arguments.get("method") or "GET").upper() if isinstance(arguments, dict) else "GET"
        if method not in {"GET", "HEAD"}:
            raise ToolError(
                "program may only issue GET or HEAD HTTP requests",
                code="not_allowed",
            )
    if not isinstance(arguments, dict):
        raise ToolError("tool arguments must be a dict", code="invalid_arguments")
    if not prepared.projection.contains(name):
        raise ToolError(f"Tool {name} is not active for this run", code="not_active")
    nested = await executor.preflight(
        prepared.run_id,
        name,
        arguments,
        f"{prepared.call_id}:{index}",
        mode=prepared.mode,
        projection=prepared.projection,
    )
    return _json_value(await nested.dispatch())


async def run_program(
    executor: Any,
    prepared: Any,
    source: str,
    timeout: float = 10,
) -> str:
    if not source.strip():
        raise ToolError("program source is required", code="invalid_arguments")
    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise ToolError("program source is too large", code="invalid_arguments")
    worker = Path(__file__).with_name("program_worker.py")
    if executor.workspace.root is None:
        raise ToolError("Program execution requires a workspace", code="workspace_required")
    workspace = Path(executor.workspace.root)
    workspace.mkdir(parents=True, exist_ok=True)
    env = executor.sandbox.clean_environment({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-u",
            str(worker),
            cwd=workspace,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise ToolError(f"program worker could not start: {exc}", code="unavailable") from exc
    assert process.stdin and process.stdout and process.stderr
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(1.0, float(timeout))
    calls = 0
    call_log: list[dict[str, Any]] = []
    final: dict[str, Any] | None = None
    failure: str | None = None

    async def read_message() -> dict[str, Any]:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError
        line = await asyncio.wait_for(process.stdout.readline(), remaining)
        if not line:
            raise RuntimeError("program worker exited before returning a result")
        return json.loads(line)

    async def send(payload: dict[str, Any]) -> None:
        process.stdin.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
        await process.stdin.drain()

    try:
        await send({"source": source})
        while True:
            message = await read_message()
            kind = message.get("type")
            if kind == "call":
                calls += 1
                request_id = message.get("id")
                if calls > MAX_CALLS:
                    call_log.append(
                        {
                            "name": str(message.get("name") or ""),
                            "ok": False,
                            "error": f"program exceeded {MAX_CALLS} tool calls",
                        }
                    )
                    await send(
                        {
                            "id": request_id,
                            "ok": False,
                            "error": f"program exceeded {MAX_CALLS} tool calls",
                        }
                    )
                    continue
                try:
                    value = await _call_tool(
                        executor,
                        prepared,
                        str(message.get("name") or ""),
                        message.get("arguments"),
                        calls,
                    )
                    call_log.append(
                        {
                            "name": str(message.get("name") or ""),
                            "ok": True,
                        }
                    )
                    await send({"id": request_id, "ok": True, "value": value})
                except Exception as exc:
                    call_log.append(
                        {
                            "name": str(message.get("name") or ""),
                            "ok": False,
                            "error": str(exc)[:500],
                        }
                    )
                    await send({"id": request_id, "ok": False, "error": str(exc)})
            elif kind == "final":
                final = message
                break
            elif kind == "error":
                failure = str(message.get("message") or "program failed")
                break
            else:
                failure = "program worker returned an invalid message"
                break
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise ToolError(f"program timed out after {timeout:g}s", code="timeout") from exc
    except (RuntimeError, json.JSONDecodeError, BrokenPipeError) as exc:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise ToolError(f"program failed: {exc}", code="program_error") from exc
    finally:
        if process.returncode is None and final is None and failure is None:
            process.kill()
            await process.wait()

    if failure is not None:
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), 2)
            except TimeoutError:
                process.kill()
                await process.wait()
        stderr = (await process.stderr.read()).decode("utf-8", errors="replace").strip()
        detail = f"{failure}\n{stderr}" if stderr else failure
        raise ToolError(detail, code="program_error")
    try:
        await asyncio.wait_for(process.wait(), 2)
    except TimeoutError:
        process.kill()
        await process.wait()
    if final is None:
        stderr = (await process.stderr.read()).decode("utf-8", errors="replace").strip()
        raise ToolError(stderr or "program worker returned no result", code="program_error")
    payload = json.dumps(
        {
            "result": final.get("value"),
            "logs": final.get("logs") or [],
            "calls": final.get("calls", calls),
            "call_log": call_log[-MAX_CALLS:],
        },
        ensure_ascii=False,
    )
    if len(payload.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise ToolError("program output exceeded the 200000 byte limit", code="output_too_large")
    return payload
