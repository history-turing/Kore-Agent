from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class SandboxError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class ProcessLaunch:
    command: list[str]
    cwd: Path
    env: dict[str, str]
    profile_path: Path | None
    backend: str


class SandboxSupervisor:
    SAFE_ENV = {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
        "TERM",
        "USERPROFILE",
        "HOME",
    }

    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()

    def resolve(self, raw: str, *, must_exist: bool = False) -> Path:
        candidate = (self.workspace / raw).resolve()
        try:
            candidate.relative_to(self.workspace)
        except ValueError as exc:
            raise SandboxError("Path escapes the workspace") from exc
        if must_exist and not candidate.exists():
            raise SandboxError(f"Path does not exist: {raw}")
        return candidate

    def clean_environment(self, additions: dict[str, Any] | None = None) -> dict[str, str]:
        # Child processes receive only launch essentials plus explicit config,
        # avoiding accidental inheritance of host credentials.
        env = {key: value for key, value in os.environ.items() if key.upper() in self.SAFE_ENV}
        for key, value in (additions or {}).items():
            name = str(key)
            if not name or "=" in name or "\x00" in name:
                raise SandboxError(f"Invalid environment variable name: {name!r}")
            env[name] = str(value)
        return env

    def prepare_launch(
        self,
        executable: str,
        args: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, Any] | None = None,
        filesystem: str = "workspace",
        network: str = "allowed",
        readable_roots: list[str] | None = None,
        writable_roots: list[str] | None = None,
        allowed_domains: list[str] | None = None,
    ) -> ProcessLaunch:
        """Validate policy before any child process is created, for Shell and MCP alike."""
        if filesystem not in {"host", "workspace", "readonly"} or network not in {"allowed", "denied"}:
            raise SandboxError("Unsupported sandbox policy; process was not started")
        if readable_roots or writable_roots or allowed_domains:
            raise SandboxError("Custom sandbox roots and domain allowlists are unavailable; process was not started")
        working = (cwd or self.workspace).resolve()
        try:
            working.relative_to(self.workspace)
        except ValueError as exc:
            raise SandboxError("Process cwd escapes the workspace") from exc
        command = [executable, *args]
        profile_path: Path | None = None
        backend = "host"
        if filesystem != "host" or network != "allowed":
            # Never silently run an isolation request on an unsupported host.
            if sys.platform != "darwin":
                raise SandboxError("Workspace sandbox is unavailable on this platform; process was not started")
            sandbox_exec = shutil.which("sandbox-exec") or "/usr/bin/sandbox-exec"
            if not Path(sandbox_exec).is_file():
                raise SandboxError("macOS sandbox-exec is unavailable; process was not started")
            profile_path = self._seatbelt_profile(filesystem=filesystem, network=network)
            command = [sandbox_exec, "-f", str(profile_path), *command]
            backend = "macos-seatbelt"
        return ProcessLaunch(command, working, self.clean_environment(env), profile_path, backend)

    async def run(
        self,
        executable: str,
        args: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, Any] | None = None,
        timeout: float = 120,
        filesystem: str = "workspace",
        network: str = "allowed",
    ) -> ProcessResult:
        launch = self.prepare_launch(
            executable, args, cwd=cwd, env=env, filesystem=filesystem, network=network,
        )

        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *launch.command,
                cwd=launch.cwd,
                env=launch.env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError as exc:
            if process and process.returncode is None:
                process.kill()
                await process.wait()
            raise SandboxError(f"Process timed out after {timeout:g}s") from exc
        finally:
            if launch.profile_path:
                launch.profile_path.unlink(missing_ok=True)
        return ProcessResult(
            process.returncode,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )

    def _seatbelt_profile(self, *, filesystem: str, network: str) -> Path:
        workspace = str(self.workspace).replace('"', '\\"')
        rules = ["(version 1)", "(deny default)", "(allow process*)", "(allow sysctl-read)", "(allow mach-lookup)"]
        rules.append('(allow file-read* (subpath "/System") (subpath "/usr") (subpath "/Library"))')
        rules.append(f'(allow file-read* (subpath "{workspace}"))')
        if filesystem == "workspace":
            rules.append(f'(allow file-write* (subpath "{workspace}"))')
        elif filesystem == "host":
            rules.append("(allow file-read* file-write*)")
        if network == "allowed":
            rules.append("(allow network*)")
        handle, raw = tempfile.mkstemp(prefix="koreagent-sandbox-", suffix=".sb")
        path = Path(raw)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write("\n".join(rules))
        return path
