from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Settings
from .util import now, uid

ObservationSink = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class ComputerSession:
    session_id: str
    run_id: str
    created_at: str
    observation_id: str | None = None


class ComputerBusyError(RuntimeError):
    pass


class ComputerClient:
    def __init__(self, settings: Settings, sink: ObservationSink):
        self.settings = settings
        self.sink = sink
        self.process: asyncio.subprocess.Process | None = None
        self.sequence = 0
        self.lock = asyncio.Lock()
        configured = os.getenv("KORENEXT_COMPUTER_HELPER", "").strip()
        default = Path(__file__).resolve().parents[2] / "native" / "macos-computer-helper" / ".build" / "release" / "koreagent-next-computer-helper"
        self.helper_path = Path(configured).expanduser().resolve() if configured else default
        self.error: str | None = None
        self.session: ComputerSession | None = None
        self.lease_file = None
        self.lease_path = self.settings.data_dir / "computer.lock"

    async def start(self) -> None:
        if not self.settings.computer_enabled or sys.platform != "darwin":
            return
        if not self.helper_path.is_file():
            self.error = f"Helper not found: {self.helper_path}"
            return
        try:
            self.process = await asyncio.create_subprocess_exec(
                str(self.helper_path),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await self.request("status", {})
        except Exception as exc:
            self.error = str(exc)

    async def close(self) -> None:
        if self.session:
            await self.end_run(self.session.run_id)
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 3)
            except TimeoutError:
                self.process.kill()

    def _acquire_lease(self, run_id: str) -> ComputerSession:
        if self.session:
            if self.session.run_id != run_id:
                raise ComputerBusyError("computer is controlled by another run")
            return self.session
        self.lease_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lease_path.open("a+b")
        try:
            if sys.platform == "win32":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            handle.close()
            raise ComputerBusyError("computer is controlled by another host") from exc
        session = ComputerSession(uid(), run_id, datetime.now(UTC).isoformat())
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\nrun_id={run_id}\nsession_id={session.session_id}\n".encode())
        handle.flush()
        self.lease_file = handle
        self.session = session
        return session

    async def end_run(self, run_id: str) -> None:
        session = self.session
        if not session or session.run_id != run_id:
            return
        if self.process and self.process.returncode is None:
            try:
                await self.request("end_session", {"session_id": session.session_id})
            except Exception:
                pass
        handle, self.lease_file = self.lease_file, None
        self.session = None
        if handle:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.process or not self.process.stdin or not self.process.stdout:
            raise RuntimeError(self.error or "Computer helper is unavailable")
        async with self.lock:
            self.sequence += 1
            request_id = self.sequence
            message = {"id": request_id, "method": method, "params": params}
            self.process.stdin.write((json.dumps(message) + "\n").encode())
            await self.process.stdin.drain()
            while True:
                line = await asyncio.wait_for(self.process.stdout.readline(), 30)
                if not line:
                    raise RuntimeError("Computer helper exited")
                response = json.loads(line)
                if response.get("id") != request_id:
                    continue
                if response.get("error"):
                    raise RuntimeError(response["error"].get("message", "Computer helper error"))
                return response.get("result", {})

    async def status(self) -> dict[str, Any]:
        base = {
            "enabled": self.settings.computer_enabled,
            "available": False,
            "platform": sys.platform,
            "runtime": None,
            "reason": None,
            "helper_path": str(self.helper_path),
            "permissions": {"accessibility": "unknown", "screen_recording": "unknown"},
            "lease": None,
        }
        if sys.platform != "darwin":
            base["reason"] = "Computer Runtime requires macOS"
            return base
        if not self.settings.computer_enabled:
            base["reason"] = "KORENEXT_COMPUTER_ENABLED is false"
            return base
        if self.error:
            base["reason"] = self.error
            return base
        try:
            helper = await self.request("status", {})
            base.update(helper)
            base.update(available=True, runtime="macos-helper", reason=None)
        except RuntimeError as exc:
            base["reason"] = str(exc)
        if self.session:
            base["lease"] = {
                "busy": True,
                "owner_run_id": self.session.run_id,
                "session_id": self.session.session_id,
                "acquired_at": self.session.created_at,
            }
        else:
            base["lease"] = {"busy": False, "owner_run_id": None, "session_id": None}
        return base

    async def permission(self, name: str) -> dict[str, Any]:
        await self.request("request_permission", {"permission": name})
        return await self.status()

    async def tool(self, run_id: str, name: str, arguments: dict[str, Any]) -> str:
        method = name.removeprefix("computer_")
        new_session = self.session is None
        session = self._acquire_lease(run_id)
        if self.process and new_session:
            try:
                await self.request("begin_session", {"session_id": session.session_id})
            except Exception:
                await self.end_run(run_id)
                raise
        if self.process and method != "observe" and arguments.get("session_id") not in {None, session.session_id}:
            raise RuntimeError("session_mismatch: stale computer session")
        if method not in {"observe", "open_app", "focus_app"}:
            supplied = arguments.get("observation_id")
            if not supplied or supplied != session.observation_id:
                raise RuntimeError("stale_observation: observe again before acting")
        payload = {**arguments, "session_id": session.session_id}
        if method == "observe":
            observation_id = uid()
            screenshot_path = self.settings.data_dir / "screenshots" / f"{observation_id}.png"
            result = await self.request("observe", {**payload, "screenshot_path": str(screenshot_path)})
            observation = {
                "id": observation_id,
                "session_id": session.session_id,
                "created_at": now(),
                **result,
                "screenshot_ref": observation_id if screenshot_path.is_file() else None,
            }
            session.observation_id = observation_id
            await self.sink({"run_id": run_id, "event_time": now(), "observation": observation})
            return json.dumps(observation, ensure_ascii=False)
        result = await self.request(method, payload)
        session.observation_id = None
        result.setdefault("delivery_status", "delivered")
        result.setdefault("verification_status", "pending")
        result.setdefault("requires_fresh_observation", True)
        return json.dumps(result, ensure_ascii=False)

    def tool_definitions(self) -> list[dict[str, Any]]:
        if not self.settings.computer_enabled or sys.platform != "darwin" or self.error:
            return []
        specs = {
            "computer_observe": ("Observe the active macOS app, windows, elements and screenshot.", {}),
            "computer_click": (
                "Click an element reference from the latest observation, or a desktop point.",
                {
                    "element_ref": {"type": "string"},
                    "x": {"type": "number"},
                    "y": {"type": "number"},
                    "observation_id": {"type": "string"},
                },
            ),
            "computer_type": (
                "Type text into the focused element.",
                {"text": {"type": "string"}, "observation_id": {"type": "string"}},
            ),
            "computer_key": (
                "Press a named macOS key.",
                {"key": {"type": "string"}, "observation_id": {"type": "string"}},
            ),
            "computer_scroll": (
                "Scroll the macOS desktop.",
                {
                    "delta_x": {"type": "number"},
                    "delta_y": {"type": "number"},
                    "observation_id": {"type": "string"},
                },
            ),
            "computer_open_app": (
                "Launch or activate a macOS application.",
                {"app": {"type": "string"}},
            ),
            "computer_focus_app": (
                "Bring a macOS application to the front.",
                {"app": {"type": "string"}},
            ),
            "computer_focus_window": (
                "Focus a window reference from the latest observation.",
                {"window_ref": {"type": "string"}, "observation_id": {"type": "string"}},
            ),
        }
        result = []
        for name, (description, properties) in specs.items():
            if name == "computer_observe":
                required = []
            elif name == "computer_click":
                required = ["observation_id"]
            elif name == "computer_scroll":
                required = ["delta_y", "observation_id"]
            elif name == "computer_focus_window":
                required = ["window_ref", "observation_id"]
            elif name in {"computer_type", "computer_key"}:
                required = [next(iter(properties)), "observation_id"]
            else:
                required = [next(iter(properties))]
            result.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": description,
                        "parameters": {"type": "object", "properties": properties, "required": required},
                    },
                }
            )
        return result
