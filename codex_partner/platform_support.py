"""Small, dependency-free platform adaptations used by the server runtime."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import ipaddress
from pathlib import Path
from typing import Iterable


SYSTEM = platform.system() or ("Windows" if os.name == "nt" else "Unknown")
IS_WINDOWS = SYSTEM == "Windows"
IS_MACOS = SYSTEM == "Darwin"


def default_data_dir(package_root: Path) -> Path:
    override = os.getenv("CODEX_DASHBOARD_DATA", "").strip()
    if override:
        return Path(override).expanduser()
    if IS_WINDOWS:
        base = Path(os.getenv("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
        return base / "CodexPartner"
    if IS_MACOS:
        return Path.home() / "Library/Application Support/CodexPartner"
    return package_root / "data"


def prepare_subprocess_command(command: Iterable[str]) -> list[str]:
    """Wrap Windows batch shims so asyncio and subprocess can execute them."""

    values = [str(value) for value in command]
    if not values or not IS_WINDOWS or Path(values[0]).suffix.lower() not in {".bat", ".cmd"}:
        return values
    command_processor = os.getenv("COMSPEC") or shutil.which("cmd.exe") or "cmd.exe"
    return [command_processor, "/d", "/s", "/c", subprocess.list2cmdline(values)]


def local_shell_command() -> tuple[list[str], str]:
    if IS_WINDOWS:
        shell = shutil.which("pwsh.exe") or shutil.which("powershell.exe") or os.getenv("COMSPEC") or "cmd.exe"
        args = [shell, "-NoLogo"] if Path(shell).name.lower() != "cmd.exe" else [shell]
        return args, Path(shell).name
    shell = os.getenv("SHELL") or shutil.which("zsh") or shutil.which("bash") or "/bin/sh"
    return [shell, "-i"], shell


def default_auth_mode() -> str:
    # Desktop operating systems do not normally run an SSH server. The default
    # bind address remains loopback-only; remote access must opt into auth.
    return "none" if SYSTEM in {"Darwin", "Windows"} else "ssh"


def validate_bind_auth(bind_host: str, auth_mode: str, auth_explicit: bool) -> None:
    """Prevent desktop defaults from silently exposing an unauthenticated server."""

    if auth_mode != "none" or auth_explicit:
        return
    host = bind_host.strip().strip("[]")
    if host.lower() == "localhost":
        return
    try:
        if ipaddress.ip_address(host).is_loopback:
            return
    except ValueError:
        pass
    raise RuntimeError(
        "CODEX_DASHBOARD_AUTH defaults to 'none' on this platform, so "
        "CODEX_DASHBOARD_HOST must remain loopback-only. Configure authentication "
        "or explicitly set CODEX_DASHBOARD_AUTH=none before exposing the service."
    )
