from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import replace

import pytest

import kore_next.sandbox as sandbox_module
from kore_next.config import Settings
from kore_next.sandbox import SandboxError, SandboxSupervisor
from kore_next.tools.shell import (
    build_shell_invocation,
    describe_shell,
    shell_command_argv,
    shell_sandbox_policy,
)

WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="Windows shell integration test")


def _wsl_ready() -> bool:
    if sys.platform != "win32" or shutil.which("wsl") is None:
        return False
    try:
        result = subprocess.run(
            ["wsl", "--list", "--quiet"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


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


def test_wsl_runtime_reports_workspace_isolation():
    runtime = describe_shell(
        settings(shell_backend="wsl"),
        platform="win32",
        which=lambda name: r"C:\Windows\System32\wsl.exe" if name == "wsl" else None,
    )

    assert runtime.backend == "wsl"
    assert runtime.interpreter == "bash"
    assert runtime.workspace_sandbox == "wsl"
    assert runtime.host_enabled is True


def test_wsl_runtime_reports_missing_wsl():
    runtime = describe_shell(settings(shell_backend="wsl"), platform="win32", which=lambda _: None)

    assert runtime.host_available is False
    assert runtime.reason == "WSL is not available"


def test_macos_runtime_keeps_seatbelt_sandbox():
    runtime = describe_shell(settings(), platform="darwin")

    assert runtime.backend == "posix-host"
    assert runtime.workspace_sandbox == "macos-seatbelt"


def test_windows_powershell_argv_passes_the_command_as_one_argument():
    invocation = build_shell_invocation(
        settings(),
        'Write-Output "hello world" && Get-Date',
        platform="win32",
        os_name="nt",
        which=lambda name: r"C:\Program Files\PowerShell\7\pwsh.exe" if name == "pwsh" else None,
    )

    assert invocation.backend == "windows-host"
    assert invocation.interpreter == "pwsh"
    assert invocation.argv[0].endswith("pwsh.exe")
    assert invocation.argv[1:5] == ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command"]
    assert invocation.argv[5].startswith("[Console]::OutputEncoding")
    assert invocation.argv[5].endswith('Write-Output "hello world" && Get-Date')


def test_windows_cmd_argv_uses_comspec_and_utf8_code_page(monkeypatch):
    comspec = r"C:\Windows\System32\cmd.exe"
    monkeypatch.setenv("COMSPEC", comspec)

    invocation = build_shell_invocation(
        settings(shell_interpreter="cmd"),
        "dir /b",
        platform="win32",
        os_name="nt",
    )

    assert invocation.argv == [comspec, "/d", "/s", "/c", "chcp 65001>nul & dir /b"]


def test_windows_auto_falls_back_to_windows_powershell():
    invocation = build_shell_invocation(
        settings(),
        "Write-Output ok",
        platform="win32",
        os_name="nt",
        which=lambda name: (
            r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" if name == "powershell" else None
        ),
    )

    assert invocation.interpreter == "powershell"
    assert invocation.argv[0].endswith("powershell.exe")


def test_shell_command_argv_remains_a_thin_compatibility_wrapper():
    argv = shell_command_argv(
        settings(),
        "git status",
        platform="win32",
        os_name="nt",
        which=lambda name: r"C:\Program Files\PowerShell\7\pwsh.exe" if name == "pwsh" else None,
    )

    assert argv[0].endswith("pwsh.exe")
    assert argv[-1].endswith("git status")


def test_posix_local_shell_keeps_existing_direct_argv_behavior():
    invocation = build_shell_invocation(
        settings(),
        "git status",
        platform="linux",
        os_name="posix",
    )

    assert invocation.backend == "posix-host"
    assert invocation.interpreter == "direct"
    assert invocation.argv == ["git", "status"]


def test_wsl_invocation_uses_optional_distro_and_bash():
    wsl = r"C:\Windows\System32\wsl.exe"
    invocation = build_shell_invocation(
        settings(shell_backend="wsl", shell_wsl_distro="Ubuntu-22.04"),
        "pwd",
        platform="win32",
        os_name="nt",
        which=lambda name: wsl if name == "wsl" else None,
    )

    assert invocation.backend == "wsl"
    assert invocation.argv == [wsl, "-d", "Ubuntu-22.04", "--", "bash", "-lc", "pwd"]


def test_wsl_backend_is_windows_only():
    with pytest.raises(ValueError, match="only available on Windows"):
        build_shell_invocation(
            settings(shell_backend="wsl"),
            "pwd",
            platform="linux",
            os_name="posix",
            which=lambda _: "/usr/bin/wsl",
        )


def test_isolated_backends_use_host_process_with_their_own_sandbox():
    wsl = r"C:\Windows\System32\wsl.exe"

    assert shell_sandbox_policy(settings(shell_backend="docker"), platform="win32") == ("host", "allowed")
    assert shell_sandbox_policy(
        settings(shell_backend="wsl"),
        platform="win32",
        which=lambda name: wsl if name == "wsl" else None,
    ) == ("host", "allowed")
    assert shell_sandbox_policy(settings(), platform="win32") == ("workspace", "allowed")


def test_decode_output_handles_utf16_windows_diagnostics():
    assert sandbox_module._decode_output("错误".encode("utf-16-le")) == "错误"


def test_clean_environment_keeps_windows_tooling_paths(tmp_path, monkeypatch):
    values = {
        "APPDATA": r"C:\Users\demo\AppData\Roaming",
        "LOCALAPPDATA": r"C:\Users\demo\AppData\Local",
        "PROGRAMFILES": r"C:\Program Files",
        "PSModulePath": r"C:\Program Files\PowerShell\Modules",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    env = SandboxSupervisor(tmp_path).clean_environment()
    normalized = {key.upper(): value for key, value in env.items()}

    for name, value in values.items():
        assert normalized[name.upper()] == value


async def test_windows_workspace_sandbox_error_points_at_host_and_docker(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox_module.sys, "platform", "win32")

    with pytest.raises(SandboxError, match="KORENEXT_SHELL_FILESYSTEM=host"):
        await SandboxSupervisor(tmp_path).run("cmd", ["/c", "echo unsafe"], filesystem="workspace")


@WINDOWS_ONLY
@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("Write-Output 'hello'", "hello"),
        ('python -c "print(1)"', "1"),
        ("Write-Output '你好'", "你好"),
    ],
)
async def test_windows_host_shell_runs_common_commands(tmp_path, command, expected):
    invocation = build_shell_invocation(settings(), command, platform="win32", os_name="nt")

    result = await SandboxSupervisor(tmp_path).run(
        invocation.argv[0],
        invocation.argv[1:],
        filesystem="host",
        network="allowed",
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert expected in result.stdout


@WINDOWS_ONLY
async def test_windows_cmd_runs_builtins_in_the_workspace(tmp_path):
    (tmp_path / "marker.txt").write_text("marker", encoding="utf-8")
    invocation = build_shell_invocation(
        settings(shell_interpreter="cmd"),
        "dir /b",
        platform="win32",
        os_name="nt",
    )

    result = await SandboxSupervisor(tmp_path).run(
        invocation.argv[0],
        invocation.argv[1:],
        filesystem="host",
        network="allowed",
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "marker.txt" in result.stdout


@WINDOWS_ONLY
@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell 7 is required for &&")
async def test_windows_powershell_supports_operators(tmp_path):
    invocation = build_shell_invocation(
        settings(shell_interpreter="pwsh"),
        "Write-Output 'hi' && Write-Output 'bye'",
        platform="win32",
        os_name="nt",
    )

    result = await SandboxSupervisor(tmp_path).run(
        invocation.argv[0],
        invocation.argv[1:],
        filesystem="host",
        network="allowed",
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "hi" in result.stdout
    assert "bye" in result.stdout


@WINDOWS_ONLY
async def test_windows_shell_timeout_terminates_the_process(tmp_path):
    invocation = build_shell_invocation(
        settings(),
        "Start-Sleep -Seconds 10",
        platform="win32",
        os_name="nt",
    )

    with pytest.raises(SandboxError, match="timed out"):
        await SandboxSupervisor(tmp_path).run(
            invocation.argv[0],
            invocation.argv[1:],
            filesystem="host",
            network="allowed",
            timeout=0.5,
        )


@WINDOWS_ONLY
@pytest.mark.skipif(not _wsl_ready(), reason="WSL is not available")
async def test_wsl_backend_runs_in_linux(tmp_path):
    invocation = build_shell_invocation(
        settings(shell_backend="wsl"),
        "printf 'wsl-ok\\n'",
        platform="win32",
        os_name="nt",
    )

    result = await SandboxSupervisor(tmp_path).run(
        invocation.argv[0],
        invocation.argv[1:],
        filesystem="host",
        network="allowed",
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "wsl-ok" in result.stdout
