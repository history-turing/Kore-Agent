from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass

from ..config import Settings

CONTAINER_BOOTSTRAP = (
    "if [ -f /opt/miniconda3/bin/activate ]; then "
    "source /opt/miniconda3/bin/activate; "
    "conda activate testbed 2>/dev/null || true; fi"
)

POWERSHELL_UTF8_PREFIX = (
    "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); $OutputEncoding = [Console]::OutputEncoding; "
)
CMD_UTF8_PREFIX = "chcp 65001>nul & "


@dataclass(frozen=True)
class ShellInvocation:
    argv: list[str]
    backend: str
    interpreter: str


@dataclass(frozen=True)
class ShellRuntime:
    backend: str
    interpreter: str | None
    host_available: bool
    host_enabled: bool
    workspace_sandbox: str
    reason: str | None = None


def _resolve_windows_interpreter(
    requested: str,
    *,
    which: Callable[[str], str | None],
) -> tuple[str, str]:
    """Resolve a Windows shell without importing platform code into the caller."""
    name = requested.strip().lower() or "auto"
    if name not in {"auto", "pwsh", "powershell", "cmd"}:
        raise ValueError(f"Unsupported Windows shell interpreter: {requested}")
    if name == "auto":
        for candidate in ("pwsh", "powershell"):
            path = which(candidate)
            if path:
                return candidate, path
        return "cmd", os.environ.get("COMSPEC", "cmd.exe")
    if name == "cmd":
        return "cmd", os.environ.get("COMSPEC", "cmd.exe")
    path = which(name)
    if not path:
        raise ValueError(f"Windows shell interpreter is not available: {name}")
    return name, path


def describe_shell(
    settings: Settings,
    *,
    platform: str | None = None,
    which: Callable[[str], str | None] | None = None,
) -> ShellRuntime:
    """Describe the configured shell without starting a process."""
    platform = sys.platform if platform is None else platform
    which = shutil.which if which is None else which
    if settings.shell_backend == "docker":
        return ShellRuntime(
            backend="docker",
            interpreter="bash",
            host_available=True,
            host_enabled=True,
            workspace_sandbox="docker",
        )
    if settings.shell_backend != "local":
        return ShellRuntime(
            backend=settings.shell_backend,
            interpreter=None,
            host_available=False,
            host_enabled=False,
            workspace_sandbox="unavailable",
            reason=f"Unsupported shell backend: {settings.shell_backend}",
        )
    if platform == "win32":
        try:
            interpreter, _ = _resolve_windows_interpreter(settings.shell_interpreter, which=which)
        except ValueError as exc:
            return ShellRuntime(
                backend="windows-host",
                interpreter=None,
                host_available=False,
                host_enabled=False,
                workspace_sandbox="unavailable",
                reason=str(exc),
            )
        return ShellRuntime(
            backend="windows-host",
            interpreter=interpreter,
            host_available=True,
            host_enabled=settings.shell_filesystem == "host" and settings.shell_network == "allowed",
            workspace_sandbox="unavailable",
        )
    return ShellRuntime(
        backend="posix-host",
        interpreter="direct",
        host_available=True,
        host_enabled=settings.shell_filesystem == "host" and settings.shell_network == "allowed",
        workspace_sandbox="macos-seatbelt" if platform == "darwin" else "unavailable",
    )


def _windows_argv(interpreter: str, executable: str, command: str) -> list[str]:
    if interpreter == "cmd":
        return [executable, "/d", "/s", "/c", CMD_UTF8_PREFIX + command]
    return [
        executable,
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        POWERSHELL_UTF8_PREFIX + command,
    ]


def build_shell_invocation(
    settings: Settings,
    command: str,
    *,
    platform: str | None = None,
    os_name: str | None = None,
    which: Callable[[str], str | None] | None = None,
) -> ShellInvocation:
    """Build the interpreter and argv for one shell call."""
    platform = sys.platform if platform is None else platform
    os_name = os.name if os_name is None else os_name
    which = shutil.which if which is None else which
    if settings.shell_backend == "docker":
        container = settings.shell_container.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", container):
            raise ValueError("A valid container name is required for the docker shell backend")
        bootstrap = settings.shell_bootstrap.strip() or CONTAINER_BOOTSTRAP
        script = f"{bootstrap}\n{command}" if bootstrap else command
        docker = shlex.split(settings.docker_command, posix=os_name != "nt") or ["docker"]
        return ShellInvocation(
            [*docker, "exec", "-w", "/testbed", container, "bash", "-lc", script],
            "docker",
            "bash",
        )
    if settings.shell_backend != "local":
        raise ValueError(f"Unsupported shell backend: {settings.shell_backend}")
    if platform == "win32":
        interpreter, executable = _resolve_windows_interpreter(settings.shell_interpreter, which=which)
        return ShellInvocation(_windows_argv(interpreter, executable, command), "windows-host", interpreter)
    return ShellInvocation(shlex.split(command, posix=os_name != "nt"), "posix-host", "direct")


def shell_command_argv(
    settings: Settings,
    command: str,
    *,
    platform: str | None = None,
    os_name: str | None = None,
    which: Callable[[str], str | None] | None = None,
) -> list[str]:
    """Compatibility wrapper for callers that only need argv."""
    return build_shell_invocation(
        settings,
        command,
        platform=platform,
        os_name=os_name,
        which=which,
    ).argv
