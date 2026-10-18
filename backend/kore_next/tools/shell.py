from __future__ import annotations

import os
import re
import shlex

from ..config import Settings

CONTAINER_BOOTSTRAP = (
    "if [ -f /opt/miniconda3/bin/activate ]; then "
    "source /opt/miniconda3/bin/activate; "
    "conda activate testbed 2>/dev/null || true; fi"
)


def shell_command_argv(settings: Settings, command: str) -> list[str]:
    """Build the argv for a shell call without going through a platform shell."""
    if settings.shell_backend == "docker":
        container = settings.shell_container.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", container):
            raise ValueError("A valid container name is required for the docker shell backend")
        bootstrap = settings.shell_bootstrap.strip() or CONTAINER_BOOTSTRAP
        script = f"{bootstrap}\n{command}" if bootstrap else command
        docker = shlex.split(settings.docker_command, posix=os.name != "nt") or ["docker"]
        return [*docker, "exec", "-w", "/testbed", container, "bash", "-lc", script]
    if settings.shell_backend != "local":
        raise ValueError(f"Unsupported shell backend: {settings.shell_backend}")
    return shlex.split(command, posix=os.name != "nt")
