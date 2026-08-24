"""Linux user-service management for Codex processes that outlive the dashboard."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import shutil
import socket
from pathlib import Path
from typing import Optional


def supported() -> bool:
    return platform.system() == "Linux" and bool(shutil.which("systemctl") and shutil.which("systemd-run"))


def _identity(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def unit_name(key: str) -> str:
    return f"codex-partner-codex-{_identity(key)}.service"


async def _run(*command: str) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    return process.returncode or 0, output.decode(errors="replace")


async def _endpoint_ready(port: int) -> bool:
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()
        await writer.wait_closed()
        return True
    except OSError:
        return False


async def ensure_service(
    key: str,
    codex_bin: str,
    env: dict[str, str],
    data_dir: Path,
) -> Optional[tuple[str, int]]:
    """Start or reuse a Codex app-server in a separate systemd user cgroup."""
    if not supported():
        return None
    identity = _identity(key)
    unit = unit_name(key)
    config_root = data_dir / "appserver"
    config_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    env_path = config_root / f"{identity}.env"
    forwarded = {
        name: value
        for name, value in env.items()
        if name in {"HOME", "PATH", "CODEX_HOME", "OPENAI_API_KEY", "OPENAI_BASE_URL", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"}
        and value
    }
    env_path.write_text("".join(f"{name}={json.dumps(value)}\n" for name, value in forwarded.items()), encoding="utf-8")
    env_path.chmod(0o600)
    endpoint_path = config_root / f"{identity}.endpoint"
    try:
        port = int(endpoint_path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = int(probe.getsockname()[1])
        endpoint_path.write_text(str(port), encoding="ascii")
        endpoint_path.chmod(0o600)
    websocket_url = f"ws://127.0.0.1:{port}"

    active, _ = await _run("systemctl", "--user", "is-active", "--quiet", unit)
    if active == 0 and not await _endpoint_ready(port):
        # A transient unit can remain active while its old endpoint is no
        # longer accepting connections. Never hand that stale address back to
        # a restarted dashboard or session.
        restart_code, _ = await _run("systemctl", "--user", "restart", unit)
        if restart_code == 0:
            for _ in range(100):
                if await _endpoint_ready(port):
                    break
                await asyncio.sleep(0.05)
            else:
                await _run("systemctl", "--user", "stop", unit)
                active = 1
        else:
            await _run("systemctl", "--user", "stop", unit)
            active = 1
    if active != 0:
        await _run("systemctl", "--user", "reset-failed", unit)
        code, output = await _run(
            "systemd-run",
            "--user",
            f"--unit={unit.removesuffix('.service')}",
            "--property=Restart=on-failure",
            "--property=RestartSec=1",
            "--property=TimeoutStopSec=10",
            f"--property=EnvironmentFile={env_path}",
            codex_bin,
            "app-server",
            "--listen",
            websocket_url,
        )
        if code:
            raise RuntimeError(f"Unable to start persistent Codex app-server: {output.strip()}")
        for _ in range(100):
            if await _endpoint_ready(port):
                break
            await asyncio.sleep(0.05)
        else:
            raise RuntimeError("Persistent Codex app-server did not open its control endpoint")

    _, pid_text = await _run("systemctl", "--user", "show", "--property=MainPID", "--value", unit)
    try:
        daemon_pid = int(pid_text.strip())
    except ValueError:
        daemon_pid = 0
    return websocket_url, daemon_pid


async def stop_service(key: str) -> None:
    if supported():
        await _run("systemctl", "--user", "stop", unit_name(key))


async def stop_inactive_services(active_keys: set[str]) -> int:
    """Stop Partner-owned app servers that no active task can recover."""
    if not supported():
        return 0
    active_units = {unit_name(key) for key in active_keys}
    code, output = await _run(
        "systemctl",
        "--user",
        "list-units",
        "--type=service",
        "--state=running",
        "--plain",
        "--no-legend",
        "codex-partner-codex-*.service",
    )
    if code:
        return 0
    stale_units = {
        line.split(None, 1)[0]
        for line in output.splitlines()
        if line.strip() and line.split(None, 1)[0] not in active_units
    }
    if not stale_units:
        return 0
    await asyncio.gather(
        *(_run("systemctl", "--user", "stop", "--no-block", unit) for unit in stale_units)
    )
    return len(stale_units)
