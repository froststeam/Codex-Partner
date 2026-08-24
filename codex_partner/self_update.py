"""Safe pip-based self-update helpers for an installed Codex Partner service."""

from __future__ import annotations

import asyncio
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Any


PACKAGE_NAME = "codex-partner"
DEFAULT_INTERVAL_HOURS = 6
PYPI_URL = "https://pypi.org/pypi/codex-partner/json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def installed_version(fallback: str = "") -> str:
    try:
        return metadata.version(PACKAGE_NAME)
    except metadata.PackageNotFoundError:
        return fallback


def load_state(path: Path) -> dict[str, Any]:
    defaults = {
        "auto_update": True,
        "interval_hours": DEFAULT_INTERVAL_HOURS,
        "last_checked_at": "",
        "last_updated_at": "",
        "last_result": "never",
        "last_error": "",
        "previous_version": "",
        "installed_version": "",
        "restart_required": False,
    }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return defaults
    if not isinstance(payload, dict):
        return defaults
    result = defaults | payload
    result["auto_update"] = bool(result.get("auto_update"))
    result["restart_required"] = bool(result.get("restart_required"))
    result["interval_hours"] = DEFAULT_INTERVAL_HOURS
    return result


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    temporary.replace(path)


async def pip_upgrade(
    *,
    python_executable: str = sys.executable,
    package: str = PACKAGE_NAME,
    timeout: float = 600,
) -> dict[str, Any]:
    """Upgrade the installed package with the current interpreter's pip."""
    before = installed_version()
    command = [
        python_executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "--disable-pip-version-check",
        package,
    ]
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError("Codex Partner pip update timed out after 10 minutes")
    text = output.decode(errors="replace")[-12000:]
    if process.returncode:
        raise RuntimeError(
            f"pip install exited with code {process.returncode}:\n{text}"
        )
    after = installed_version(before)
    return {
        "ok": True,
        "previous_version": before,
        "installed_version": after,
        "changed": bool(after and after != before),
        "command": command,
        "output": text,
    }


def version_key(value: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", value or "")
    return tuple(map(int, numbers[:4])) or (0,)


def latest_pypi_version(timeout: float = 10) -> str:
    request = urllib.request.Request(
        PYPI_URL,
        headers={"Accept": "application/json", "User-Agent": "codex-partner-update"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    return str(payload.get("info", {}).get("version") or "")
