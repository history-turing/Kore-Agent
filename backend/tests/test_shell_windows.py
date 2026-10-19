from __future__ import annotations

from dataclasses import replace

from kore_next.config import Settings
from kore_next.tools.shell import describe_shell


def settings(**overrides):
    return replace(Settings(), **overrides)


def test_windows_runtime_prefers_pwsh_without_changing_default_policy():
    runtime = describe_shell(
        settings(),
        platform="win32",
        which=lambda name: r"C:\Program Files\PowerShell\7\pwsh.exe" if name == "pwsh" else None,
    )

    assert runtime.backend == "windows-host"
    assert runtime.interpreter == "pwsh"
    assert runtime.host_available is True
    assert runtime.host_enabled is False
    assert runtime.workspace_sandbox == "unavailable"


def test_windows_runtime_reports_host_mode_when_explicitly_enabled():
    runtime = describe_shell(
        settings(shell_filesystem="host"),
        platform="win32",
        which=lambda name: (
            r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" if name == "powershell" else None
        ),
    )

    assert runtime.interpreter == "powershell"
    assert runtime.host_enabled is True


def test_windows_runtime_rejects_unknown_interpreter():
    runtime = describe_shell(settings(shell_interpreter="fish"), platform="win32", which=lambda _: None)

    assert runtime.host_available is False
    assert runtime.reason == "Unsupported Windows shell interpreter: fish"


def test_windows_runtime_reports_missing_interpreter():
    runtime = describe_shell(settings(shell_interpreter="pwsh"), platform="win32", which=lambda _: None)

    assert runtime.host_available is False
    assert runtime.reason == "Windows shell interpreter is not available: pwsh"


def test_docker_runtime_is_workspace_isolated():
    runtime = describe_shell(settings(shell_backend="docker"), platform="win32")

    assert runtime.backend == "docker"
    assert runtime.interpreter == "bash"
    assert runtime.workspace_sandbox == "docker"
    assert runtime.host_enabled is True


def test_macos_runtime_keeps_seatbelt_sandbox():
    runtime = describe_shell(settings(), platform="darwin")

    assert runtime.backend == "posix-host"
    assert runtime.workspace_sandbox == "macos-seatbelt"
