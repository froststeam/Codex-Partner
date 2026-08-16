"""Codex and npm executable discovery without application state."""

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional


def executable_works(candidate: str) -> bool:
    path = Path(candidate).expanduser()
    if not path.is_file() or not os.access(path, os.X_OK):
        return False
    try:
        result = subprocess.run(
            [str(path), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def vscode_candidates(home: Optional[Path] = None) -> list[str]:
    home = (home or Path.home()).expanduser()
    roots = (
        home / ".vscode/extensions",
        home / ".vscode-server/extensions",
        home / ".vscode-insiders/extensions",
        home / ".vscode-server-insiders/extensions",
    )
    candidates: list[tuple[tuple[int, ...], float, str]] = []
    for root in roots:
        if not root.is_dir():
            continue
        for extension in root.glob("openai.chatgpt-*"):
            version = tuple(int(value) for value in re.findall(r"\d+", extension.name))
            try:
                modified = extension.stat().st_mtime
            except OSError:
                modified = 0.0
            for pattern in ("bin/*/codex", "bin/*/codex.exe"):
                for binary in extension.glob(pattern):
                    candidates.append((version, modified, str(binary.resolve())))
    candidates.sort(reverse=True)
    return [candidate for _version, _modified, candidate in candidates]


def official_candidates(codex_home: Path, home: Optional[Path] = None) -> list[str]:
    home = (home or Path.home()).expanduser()
    candidates = [
        codex_home / "bin/codex",
        home / ".local/bin/codex",
        home / ".npm-global/bin/codex",
        home / ".npm/bin/codex",
        Path("/usr/local/bin/codex"),
        Path("/opt/homebrew/bin/codex"),
        Path("/home/linuxbrew/.linuxbrew/bin/codex"),
        Path("/usr/bin/codex"),
    ]
    candidates[4:4] = sorted((home / ".nvm/versions/node").glob("*/bin/codex"), reverse=True)
    if os.name == "nt":
        local = Path(os.getenv("LOCALAPPDATA", str(home / "AppData/Local")))
        candidates[:0] = [local / "CodexPartner/npm/codex.cmd", local / "CodexDashboard/npm/codex.cmd", local / "npm/codex.cmd"]
    return [str(candidate.expanduser().resolve()) for candidate in candidates]


def find_codex(
    requested: str,
    vscode: list[str],
    official: list[str],
    usable: Callable[[str], bool],
) -> dict:
    """Apply the documented PATH, VS Code, then official-path precedence."""
    checked: list[str] = []
    direct = shutil.which(requested)
    if direct:
        direct = str(Path(direct).expanduser().resolve())
        checked.append(direct)
        if usable(direct):
            return {"available": True, "path": direct, "source": "path", "checked": checked}
    for source, candidates in (("vscode", vscode), ("official", official)):
        for candidate in candidates:
            if candidate in checked:
                continue
            checked.append(candidate)
            if usable(candidate):
                return {"available": True, "path": candidate, "source": source, "checked": checked}
    return {"available": False, "path": "", "source": "missing", "checked": checked}


def find_npm() -> str:
    names = ("npm.cmd", "npm") if os.name == "nt" else ("npm",)
    for name in names:
        if found := shutil.which(name):
            return str(Path(found).expanduser().resolve())
    home = Path.home()
    candidates = [
        Path("/usr/local/nodejs/bin/npm"),
        Path("/usr/local/bin/npm"),
        Path("/opt/homebrew/bin/npm"),
        Path("/home/linuxbrew/.linuxbrew/bin/npm"),
        home / ".local/bin/npm",
    ]
    candidates += sorted((home / ".nvm/versions/node").glob("*/bin/npm"), reverse=True)
    return next((str(path.resolve()) for path in candidates if path.is_file() and os.access(path, os.X_OK)), "")


def install_plan(system: str, npm: str, package: str) -> dict:
    supported = system in {"Linux", "Darwin", "Windows"}
    if system == "Windows":
        prefix = Path(os.getenv("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "CodexPartner/npm"
    else:
        prefix = Path.home() / ".local"
    command = [npm, "install", "--global", "--prefix", str(prefix), package] if npm else []
    if not supported:
        reason = f"暂不支持在 {system} 上自动安装 Codex"
    elif not npm:
        reason = "未找到 npm；请先安装 Node.js LTS（Windows 建议在 WSL 中运行 Codex Partner），然后重试"
    else:
        reason = ""
    display = subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)
    return {
        "supported": bool(supported and npm),
        "os": system,
        "package": package,
        "prefix": str(prefix),
        "command": display,
        "reason": reason,
    }
