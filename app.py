from __future__ import annotations

import asyncio
import base64
import binascii
import codecs
import errno
import getpass
import fcntl
import glob
import hashlib
import io
import ipaddress
import json
import mimetypes
import os
import posixpath
import pty
import platform
import re
import signal
import secrets
import shlex
import shutil
import sqlite3
import socket
import struct
import subprocess
import termios
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.middleware.gzip import GZipMiddleware
from dotenv import load_dotenv
import yaml
import pexpect
from PIL import Image, ImageOps, ImageSequence, UnidentifiedImageError

from codex_partner import APP_NAME, APP_VERSION
from codex_partner.commands import SLASH_ALIASES, SLASH_COMMAND_BY_NAME, SLASH_COMMANDS, parse_slash_command
from codex_partner.database import Database
from codex_partner.app_server import AppServerClient
from codex_partner.discovery import (
    executable_works,
    find_codex,
    find_npm,
    install_plan,
    official_candidates,
    vscode_candidates,
)
from codex_partner.schemas import (
    ApprovalResolveIn,
    AvatarIn,
    ContextPatch,
    GoalPatch,
    MemoryIn,
    MemoryResetIn,
    OperationIn,
    ProviderIn,
    ProviderVerifyIn,
    QuickTaskCreate,
    SSHConnectIn,
    SSHLoginIn,
    SkillIn,
    SlashCommandIn,
    TaskCreate,
    TaskMessageIn,
    TaskMessagePatch,
    TaskPatch,
    WorkspaceFileUpdate,
)
from codex_partner.ssh_auth import LoginThrottle, verify_ssh_password

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=False)
STATIC = ROOT / "static"
DATA_DIR = Path(os.getenv("CODEX_DASHBOARD_DATA", str(ROOT / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "dashboard.sqlite3"
CODEX_HOME = Path(os.getenv("CODEX_HOME", str(Path.home() / ".codex"))).expanduser().resolve()
CODEX_MEMORY_DIR = CODEX_HOME / "memories"
AUTH_MODE = os.getenv("CODEX_DASHBOARD_AUTH", "ssh").strip().lower()
if AUTH_MODE not in {"ssh", "none"}:
    raise RuntimeError("CODEX_DASHBOARD_AUTH must be 'ssh' or 'none'")
AUTH_COOKIE = "codex_partner_session"
AUTH_SESSION_TTL = max(300, int(os.getenv("CODEX_DASHBOARD_AUTH_TTL", "43200")))
AUTH_SSH_HOST = os.getenv("CODEX_DASHBOARD_AUTH_SSH_HOST", "127.0.0.1").strip() or "127.0.0.1"
CODEX_APP_SERVER_ENABLED = os.getenv("CODEX_APP_SERVER", "1").lower() not in {"0", "false", "no"}
AUTO_RESUME = os.getenv("CODEX_DASHBOARD_AUTO_RESUME", "1").lower() not in {"0", "false", "no"}
app_shutting_down = False
CODEX_INSTALL_PACKAGE = "@openai/codex@latest"


def configured_port(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer between 1 and 65535, got {raw!r}") from exc
    if not 1 <= value <= 65535:
        raise RuntimeError(f"{name} must be between 1 and 65535, got {value}")
    return value


DASHBOARD_HOST = os.getenv("CODEX_DASHBOARD_HOST", "127.0.0.1").strip() or "127.0.0.1"
DASHBOARD_PORT = configured_port("CODEX_DASHBOARD_PORT", 8787)
AUTH_SSH_PORT = configured_port("CODEX_DASHBOARD_AUTH_SSH_PORT", 22)
SSH_BIN = shutil.which("ssh") or "ssh"
SSH_CONFIG = Path.home() / ".ssh/config"
SSH_FIXED_HOST = os.getenv("CODEX_DASHBOARD_SSH_HOST", "").strip()
SSH_FIXED_USER = os.getenv("CODEX_DASHBOARD_SSH_USER", "").strip()
SSH_FIXED_PORT = configured_port("CODEX_DASHBOARD_SSH_PORT", 22)
SSH_RUNTIME_DIR = Path(os.getenv("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "codex-dashboard-ssh"
try:
    SSH_RUNTIME_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    SSH_RUNTIME_DIR.chmod(0o700)
except OSError:
    SSH_RUNTIME_DIR = DATA_DIR / "ssh-runtime"
    SSH_RUNTIME_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    SSH_RUNTIME_DIR.chmod(0o700)
try:
    EXTERNAL_TURN_GRACE_SECONDS = max(1.0, float(os.getenv("CODEX_DASHBOARD_EXTERNAL_TURN_GRACE", "3")))
except ValueError:
    EXTERNAL_TURN_GRACE_SECONDS = 3.0


codex_candidate_usable = executable_works
vscode_codex_candidates = vscode_candidates


def official_codex_candidates(home: Optional[Path] = None) -> list[str]:
    return official_candidates(CODEX_HOME, home)


def discover_codex() -> dict[str, Any]:
    requested = os.getenv("CODEX_BIN", "").strip() or "codex"
    return find_codex(
        requested,
        vscode_codex_candidates(),
        official_codex_candidates(),
        codex_candidate_usable,
    )


def refresh_codex_discovery() -> dict[str, Any]:
    global CODEX_DISCOVERY, CODEX_BIN, CODEX_AVAILABLE, USE_APP_SERVER
    CODEX_DISCOVERY = discover_codex()
    CODEX_BIN = CODEX_DISCOVERY["path"] or "codex"
    CODEX_AVAILABLE = bool(CODEX_DISCOVERY["available"])
    USE_APP_SERVER = CODEX_APP_SERVER_ENABLED and CODEX_AVAILABLE
    return CODEX_DISCOVERY


CODEX_DISCOVERY: dict[str, Any] = {}
CODEX_BIN = "codex"
CODEX_AVAILABLE = False
USE_APP_SERVER = False
refresh_codex_discovery()


def require_codex() -> None:
    if not CODEX_AVAILABLE:
        raise HTTPException(
            503,
            "未找到可用的 Codex CLI。请点击网页中的“一键安装”，或在服务器执行 npm install -g @openai/codex。",
        )


find_npm_executable = find_npm


def codex_install_plan() -> dict[str, Any]:
    system = platform.system() or os.name
    npm = find_npm_executable() if system in {"Linux", "Darwin", "Windows"} else ""
    return install_plan(system, npm, CODEX_INSTALL_PACKAGE)


def codex_management_snapshot() -> dict[str, Any]:
    """Return the live CLI identity used by this service."""
    version = ""
    error = ""
    if CODEX_AVAILABLE:
        try:
            result = subprocess.run(
                [CODEX_BIN, "--version"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=8,
                check=False,
            )
            version = (result.stdout or "").strip().splitlines()[0][:240] if result.stdout else ""
            if result.returncode != 0:
                error = version or f"exit {result.returncode}"
                version = ""
        except (OSError, subprocess.SubprocessError) as exc:
            error = str(exc)
    return {
        "available": CODEX_AVAILABLE,
        "version": version,
        "path": CODEX_BIN if CODEX_AVAILABLE else "",
        "source": CODEX_DISCOVERY.get("source", "missing"),
        "working_directory": str(Path.cwd()),
        "default_workspace": str(DEFAULT_WORKSPACE),
        "error": error,
        "update_package": CODEX_INSTALL_PACKAGE,
        "update_plan": codex_install_plan(),
    }


def configured_default_workspace() -> Path:
    """Return a stable default that can never silently live in a temp tree."""
    home = Path.home().expanduser().resolve()
    raw = os.getenv("CODEX_DASHBOARD_DEFAULT_WORKSPACE", "").strip()
    supplied = Path(raw).expanduser() if raw else home
    candidate = (supplied if supplied.is_absolute() else home / supplied).resolve()
    temp_roots = tuple(Path(path).resolve() for path in ("/tmp", "/var/tmp", "/dev/shm"))
    if any(candidate == root or root in candidate.parents for root in temp_roots):
        candidate = home
    return candidate if candidate.is_dir() else home


DEFAULT_WORKSPACE = configured_default_workspace()
SESSION_WORKSPACE_ROOT = Path.home().expanduser().resolve() / "codex_partner"


def configured_workspace_roots() -> tuple[Path, ...]:
    """Return directories exposed by the server-side workspace chooser."""
    raw = os.getenv("CODEX_DASHBOARD_WORKSPACE_ROOTS", "").strip()
    configured = raw.split(os.pathsep) if raw else [str(Path.home()), str(DEFAULT_WORKSPACE)]
    if not raw:
        configured.extend(str(path) for path in (Path("/data"), Path("/workspace"), Path("/workspaces")) if path.is_dir())
    roots: list[Path] = []
    for value in configured:
        try:
            candidate = Path(value).expanduser().resolve()
        except OSError:
            continue
        if candidate.is_dir() and candidate not in roots:
            roots.append(candidate)
    return tuple(roots or [Path.home().resolve()])


WORKSPACE_ROOTS = configured_workspace_roots()


def resolve_task_workspace(value: Optional[str] = None) -> Path:
    raw = (value or "").strip()
    if not raw or raw == ".":
        candidate = DEFAULT_WORKSPACE
    else:
        supplied = Path(raw).expanduser()
        candidate = supplied if supplied.is_absolute() else DEFAULT_WORKSPACE / supplied
        candidate = candidate.resolve()
    if not candidate.exists():
        raise HTTPException(400, f"Workspace does not exist: {candidate}")
    if not candidate.is_dir():
        raise HTTPException(400, f"Workspace is not a directory: {candidate}")
    return candidate


def create_session_workspace(task_id: str) -> Path:
    """Create the private default workspace owned by a new local session."""
    root = SESSION_WORKSPACE_ROOT.resolve()
    workspace = (root / task_id).resolve()
    if workspace.parent != root:
        raise HTTPException(400, "Invalid session workspace id")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    workspace.mkdir(mode=0o700, exist_ok=False)
    return workspace


def safe_thread_id(value: str) -> str:
    thread_id = str(value or "").strip()
    if not thread_id or "/" in thread_id or "\\" in thread_id or thread_id in {".", ".."}:
        return ""
    return thread_id


def align_session_workspace(task_id: str, thread_id: str) -> Optional[Path]:
    """Rename an untouched session workspace to the Codex thread ID.

    New sessions need a temporary server ID before Codex creates its thread.
    Once the real thread exists, using that same ID for the folder makes the
    workspace, browser session, and Codex thread unambiguous. Explicitly
    selected workspaces and remote SSH paths are intentionally left alone.
    """
    thread_id = safe_thread_id(thread_id)
    if not thread_id:
        return None
    task = db.one("SELECT id,workspace,ssh_host FROM tasks WHERE id=?", (task_id,))
    if not task or task.get("ssh_host"):
        return None
    root = SESSION_WORKSPACE_ROOT.resolve()
    current = Path(task["workspace"]).expanduser().resolve()
    if current.parent != root:
        return None
    target = (root / thread_id).resolve()
    if target == current:
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        return current
    if current.name != task_id:
        return None
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.exists():
        # A previous interrupted migration may have left the destination.
        # Keep it and remove only an empty placeholder. A missing placeholder
        # means the rename completed before the database update.
        if current.is_dir() and not any(current.iterdir()):
            current.rmdir()
        elif current.exists():
            return None
    elif current.exists():
        current.rename(target)
    else:
        target.mkdir(mode=0o700)
    db.execute("UPDATE tasks SET workspace=?,updated_at=? WHERE id=?", (str(target), now(), task_id))
    return target


def canonicalize_task_thread_id(task_id: str, thread_id: str) -> str:
    """Use the Codex thread id as the dashboard task id when it is safe.

    This keeps browser task ids, Codex thread ids, and default workspace
    folders aligned for ordinary local sessions. If the target id already
    exists, keep the current task id rather than merging unrelated state.
    """
    thread_id = safe_thread_id(thread_id)
    if not thread_id or task_id == thread_id:
        return task_id
    task = db.one("SELECT id,ssh_host FROM tasks WHERE id=?", (task_id,))
    if not task or task.get("ssh_host"):
        return task_id
    if db.one("SELECT id FROM tasks WHERE id=?", (thread_id,)):
        return task_id
    task_id_aliases[task_id] = thread_id
    db.execute("UPDATE tasks SET id=?,updated_at=? WHERE id=?", (thread_id, now(), task_id))
    db.execute("UPDATE sessions SET task_id=? WHERE task_id=?", (thread_id, task_id))
    db.execute("UPDATE events SET task_id=? WHERE task_id=?", (thread_id, task_id))
    db.execute("UPDATE task_messages SET task_id=? WHERE task_id=?", (thread_id, task_id))
    if task_id in running:
        running[thread_id] = running.pop(task_id)
    if task_id in task_workers:
        task_workers[thread_id] = task_workers.pop(task_id)
    if task_id in external_turns:
        external_turns[thread_id] = external_turns.pop(task_id)
    if task_id in external_turn_sets:
        external_turn_sets[thread_id] = external_turn_sets.pop(task_id)
    if task_id in app_thread_bindings:
        app_thread_bindings[thread_id] = app_thread_bindings.pop(task_id)
    if task_id in appserver_turn_tasks:
        appserver_turn_tasks[thread_id] = appserver_turn_tasks.pop(task_id)
    if task_id in appserver_turn_ids:
        appserver_turn_ids[thread_id] = appserver_turn_ids.pop(task_id)
    if task_id in native_history_cache:
        native_history_cache[thread_id] = native_history_cache.pop(task_id)
    if task_id in native_history_locks:
        native_history_locks[thread_id] = native_history_locks.pop(task_id)
    if task_id in task_clients:
        task_clients[thread_id] = task_clients.pop(task_id)
    return thread_id


def canonicalize_session_thread_id(task_id: str, session_id: str, thread_id: str) -> str:
    thread_id = safe_thread_id(thread_id)
    if not thread_id or not session_id or session_id == thread_id:
        return session_id
    session = db.one("SELECT id,attempt FROM sessions WHERE id=? AND task_id=?", (session_id, task_id))
    if not session or int(session.get("attempt", 0)) > 1:
        return session_id
    if db.one("SELECT id FROM sessions WHERE id=?", (thread_id,)):
        return session_id
    db.execute("UPDATE sessions SET id=? WHERE id=?", (thread_id, session_id))
    db.execute("UPDATE events SET session_id=? WHERE session_id=?", (thread_id, session_id))
    db.execute("UPDATE task_messages SET session_id=? WHERE session_id=?", (thread_id, session_id))
    db.execute("UPDATE tasks SET active_session_id=? WHERE active_session_id=?", (thread_id, session_id))
    if task_id in app_thread_bindings:
        key, bound_thread_id, bound_session_id = app_thread_bindings[task_id]
        if bound_session_id == session_id:
            app_thread_bindings[task_id] = (key, bound_thread_id, thread_id)
    return thread_id


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class GoalIncompleteError(RuntimeError):
    """A healthy Codex turn finished, but its persistent Goal needs another turn."""


def is_provider_failure(exc: Exception) -> bool:
    return not app_shutting_down and not isinstance(exc, GoalIncompleteError)


db = Database(DB_PATH, now)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global app_shutting_down
    app_shutting_down = False
    observer_task: Optional[asyncio.Task] = None
    try:
        await sync_native_threads()
    except Exception:
        # The dashboard must still serve its own durable tasks when Codex is
        # temporarily unavailable; the explicit sync endpoint can retry.
        pass
    try:
        await refresh_native_rollouts()
        observer_task = asyncio.create_task(native_rollout_watch_loop())
    except Exception:
        # Rollout observation augments native Codex sessions. A malformed or
        # temporarily locked state database must not prevent the UI starting.
        pass
    if AUTO_RESUME:
        await asyncio.sleep(0)
        for row in db.all("SELECT id FROM tasks WHERE status='queued' ORDER BY updated_at"):
            if external_turns.get(row["id"]):
                continue
            try:
                task = task_or_404(row["id"])
                await launch(row["id"], "resume" if latest_codex_session(task) else "start")
            except Exception:
                continue
    try:
        yield
    finally:
        app_shutting_down = True
        if observer_task:
            observer_task.cancel()
            await asyncio.gather(observer_task, return_exceptions=True)
        workers = list(task_workers.values())
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        task_workers.clear()
        await asyncio.gather(*(close_terminal_session(terminal_id) for terminal_id in list(terminal_sessions)), return_exceptions=True)
        await asyncio.gather(*(server.close() for server in app_servers.values()), return_exceptions=True)


app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)
auth_sessions: dict[str, dict[str, Any]] = {}
login_throttle = LoginThrottle()
running: dict[str, asyncio.subprocess.Process] = {}
running_lock = asyncio.Lock()
task_id_aliases: dict[str, str] = {}
task_clients: dict[str, set[WebSocket]] = {}
overview_clients: set[WebSocket] = set()
overview_client_users: dict[WebSocket, str] = {}
task_queues: dict[str, asyncio.Queue[str]] = {}
task_workers: dict[str, asyncio.Task] = {}
task_message_locks: dict[str, asyncio.Lock] = {}
app_servers: dict[str, "AppServerClient"] = {}
app_thread_bindings: dict[str, tuple[str, str, str]] = {}
turn_waiters: dict[str, asyncio.Future] = {}
appserver_turn_tasks: dict[str, asyncio.Task] = {}
appserver_turn_ids: dict[str, str] = {}
pending_appserver_requests: dict[str, dict[str, Any]] = {}
appserver_lock = asyncio.Lock()
codex_install_lock = asyncio.Lock()
native_sync_lock = asyncio.Lock()
workspace_upload_lock = asyncio.Lock()
ssh_connect_locks: dict[str, asyncio.Lock] = {}
ssh_connection_cache: dict[str, dict[str, Any]] = {}
terminal_sessions: dict[str, dict[str, Any]] = {}
native_rollout_offsets: dict[str, int] = {}
native_rollout_remainders: dict[str, bytes] = {}
external_turns: dict[str, dict[str, Any]] = {}
external_turn_sets: dict[str, dict[str, dict[str, Any]]] = {}
rollout_writer_cache: dict[str, tuple[float, set[int]]] = {}
recent_event_signatures: dict[str, dict[str, float]] = {}
native_history_cache: dict[str, dict[str, Any]] = {}
native_history_locks: dict[str, asyncio.Lock] = {}
runtime_metric_cache: dict[str, tuple[int, list[dict]]] = {}
native_import_attempt_at = 0.0


def appserver_key(provider: Optional[dict], task: Optional[dict] = None) -> str:
    provider_key = (provider or {}).get("id", "default")
    host = (task or {}).get("ssh_host") or ""
    return f"ssh:{host}:{provider_key}" if host else provider_key


async def appserver_for(provider: Optional[dict], task: Optional[dict] = None) -> AppServerClient:
    key = appserver_key(provider, task)
    async with appserver_lock:
        if key not in app_servers:
            env = os.environ.copy()
            host = (task or {}).get("ssh_host") or ""
            if provider and provider.get("base_url") and not host:
                env["OPENAI_BASE_URL"] = provider["base_url"]
            if key_value := provider_api_key(provider) if not host else "":
                env["OPENAI_API_KEY"] = key_value
            if host:
                connection = await require_ssh_connection(host, codex=True)
                remote_command = shlex.join([connection["codex_bin"], "app-server", "--stdio"])
                command = [*ssh_options(host, batch=True), ssh_destination(host), remote_command]
                app_servers[key] = AppServerClient(
                    env,
                    key,
                    command=command,
                    local=False,
                    require_codex=require_codex,
                    notification_handler=handle_appserver_notification,
                    server_request_handler=handle_appserver_server_request,
                    thread_bindings=app_thread_bindings,
                    turn_waiters=turn_waiters,
                    client_name=APP_NAME.lower().replace(" ", "-"),
                    client_version=app.version,
                )
            else:
                app_servers[key] = AppServerClient(
                    env,
                    key,
                    command=[CODEX_BIN, "app-server", "--stdio"],
                    require_codex=require_codex,
                    notification_handler=handle_appserver_notification,
                    server_request_handler=handle_appserver_server_request,
                    thread_bindings=app_thread_bindings,
                    turn_waiters=turn_waiters,
                    client_name=APP_NAME.lower().replace(" ", "-"),
                    client_version=app.version,
                )
        await app_servers[key].start()
        return app_servers[key]


async def invalidate_appserver(key: str) -> None:
    matching = [candidate for candidate in app_servers if candidate == key or candidate.endswith(f":{key}")]
    if any(binding[0] in matching for binding in app_thread_bindings.values()):
        raise HTTPException(409, "Provider is in use by a running task")
    async with appserver_lock:
        servers = [app_servers.pop(candidate) for candidate in matching]
    if servers:
        await asyncio.gather(*(server.close() for server in servers), return_exceptions=True)


def native_goal_rows() -> dict[str, dict]:
    """Read native Codex goal metadata without copying its database."""
    path = CODEX_HOME / "goals_1.sqlite"
    if not path.is_file():
        return {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        rows = conn.execute("SELECT thread_id, objective, status, tokens_used FROM thread_goals").fetchall()
        conn.close()
    except sqlite3.Error:
        return {}
    status_names = {"usage_limited": "usageLimited", "budget_limited": "budgetLimited"}
    return {
        row[0]: {"objective": row[1], "status": status_names.get(row[2], row[2]), "tokensUsed": row[3]}
        for row in rows
    }


def sync_native_providers() -> dict:
    """Mirror safe provider metadata from Codex config, never credentials."""
    path = CODEX_HOME / "config.toml"
    if not path.is_file():
        return {"imported": 0, "updated": 0, "available": False}
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"imported": 0, "updated": 0, "available": False}
    default_provider = str(config.get("model_provider") or "")
    default_model = str(config.get("model") or "")
    imported = updated = 0
    for priority, (provider_name, provider_config) in enumerate((config.get("model_providers") or {}).items(), 10):
        if not isinstance(provider_config, dict):
            continue
        provider_name = str(provider_name)
        existing = db.one("SELECT id FROM providers WHERE model_provider=?", (provider_name,))
        display_name = str(provider_config.get("name") or provider_name)
        if display_name.lower() != provider_name.lower():
            display_name = f"{provider_name} ({display_name})"
        base_url = str(provider_config.get("base_url") or "")
        model = default_model if provider_name == default_provider else ""
        stamp = now()
        if existing:
            db.execute(
                "UPDATE providers SET name=?,kind='codex',model=CASE WHEN model='' THEN ? ELSE model END,"
                "base_url=?,native=1,updated_at=? WHERE id=?",
                (display_name, model, base_url, stamp, existing["id"]),
            )
            updated += 1
        else:
            provider_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"codex-model-provider:{provider_name}"))
            db.execute(
                "INSERT INTO providers (id,name,kind,model,profile,base_url,enabled,priority,created_at,updated_at,model_provider,native) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (provider_id, display_name, "codex", model, "", base_url, 1, priority, stamp, stamp, provider_name, 1),
            )
            imported += 1
    return {"imported": imported, "updated": updated, "available": True, "default": default_provider}


def native_thread_settings() -> dict[str, dict]:
    path = CODEX_HOME / "state_5.sqlite"
    if not path.is_file():
        return {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(threads)")}
        selected = [name for name in ("id", "approval_mode", "sandbox_policy", "model", "model_provider", "archived", "memory_mode") if name in columns]
        rows = conn.execute(f"SELECT {','.join(selected)} FROM threads").fetchall()
        conn.close()
    except sqlite3.Error:
        return {}
    return {row[0]: dict(zip(selected[1:], row[1:])) for row in rows}


def persist_native_thread_model(thread_id: str, model: str) -> None:
    thread_id = safe_thread_id(thread_id)
    if not thread_id:
        return
    path = CODEX_HOME / "state_5.sqlite"
    if not path.is_file():
        return
    try:
        conn = sqlite3.connect(str(path))
        columns = {row[1] for row in conn.execute("PRAGMA table_info(threads)")}
        if "id" not in columns or "model" not in columns:
            conn.close()
            return
        conn.execute("UPDATE threads SET model=? WHERE id=?", (model or "", thread_id))
        conn.commit()
        conn.close()
    except sqlite3.Error:
        return


def native_rollout_rows() -> list[dict[str, str]]:
    """Return Codex's canonical thread-to-rollout mapping."""
    path = CODEX_HOME / "state_5.sqlite"
    if not path.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(threads)")}
        if not {"id", "rollout_path"}.issubset(columns):
            conn.close()
            return []
        rows = conn.execute("SELECT id,rollout_path FROM threads WHERE rollout_path IS NOT NULL AND rollout_path!=''").fetchall()
        conn.close()
    except sqlite3.Error:
        return []
    return [{"thread_id": str(row[0]), "path": str(row[1])} for row in rows]


def rollout_record_phase(record: dict) -> str:
    record_type = str(record.get("type") or "")
    payload = record.get("payload") or {}
    payload_type = str(payload.get("type") or "").lower()
    if record_type == "response_item":
        if payload_type == "reasoning":
            return "正在分析与规划"
        if payload_type in {"custom_tool_call", "function_call"}:
            name = str(payload.get("name") or "").lower()
            if name in {"exec", "exec_command", "write_stdin", "wait"}:
                return "正在运行命令"
            if "patch" in name or "file" in name:
                return "正在修改文件"
            if "search" in name or name == "web":
                return "正在检索资料"
            return "正在使用工具"
    if payload_type == "agent_message":
        return "正在生成回复"
    if payload_type == "user_message":
        return "已接收新的指令"
    if "patch" in payload_type or "file" in payload_type:
        return "正在修改文件"
    if "search" in payload_type:
        return "正在检索资料"
    if "compact" in payload_type:
        return "正在压缩上下文"
    if any(token in payload_type for token in ("tool", "mcp", "collab_agent")):
        return "正在使用工具"
    return ""


def rollout_browser_payload(record: dict) -> Optional[dict]:
    """Translate durable CLI rollout records into the browser's live event vocabulary."""
    record_type = str(record.get("type") or "")
    payload = record.get("payload") or {}
    payload_type = str(payload.get("type") or "")
    lowered = payload_type.lower()
    metadata = payload.get("internal_chat_message_metadata_passthrough") or {}
    turn_id = str(payload.get("turn_id") or payload.get("turnId") or metadata.get("turn_id") or "")
    if record_type == "response_item":
        if lowered == "message":
            text = str(payload.get("text") or "")
            if not text:
                text = "\n".join(
                    str(part.get("text") or "")
                    for part in payload.get("content") or []
                    if isinstance(part, dict) and part.get("text")
                )
            role = str(payload.get("role") or "assistant").lower()
            return {
                "type": "userMessage" if role == "user" else "agentMessage",
                "text": text,
                "item_id": payload.get("id", ""),
                "turn_id": turn_id,
            }
        if lowered == "reasoning":
            return {"type": "reasoning", "text": "正在分析与规划", "item_id": payload.get("id", ""), "turn_id": turn_id}
        if lowered in {"custom_tool_call", "function_call"}:
            name = str(payload.get("name") or "tool")
            if name.lower() in {"exec", "exec_command", "write_stdin", "wait"}:
                event_type = "commandExecution"
            elif "patch" in name.lower() or "file" in name.lower():
                event_type = "fileChange"
            else:
                event_type = "toolCall"
            return {
                "type": event_type,
                "text": name,
                "item_id": payload.get("id", ""),
                "status": payload.get("status", "started"),
                "turn_id": turn_id,
            }
        return None
    if record_type != "event_msg":
        return None
    if lowered == "agent_message":
        return {
            "type": "agentMessage",
            "text": str(payload.get("message") or ""),
            "item_id": f"rollout-{turn_id}-{record.get('timestamp', '')}",
            "phase": payload.get("phase", ""),
            "turn_id": turn_id,
        }
    if lowered == "user_message":
        return {
            "type": "userMessage",
            "text": str(payload.get("message") or ""),
            "client_message_id": payload.get("client_id"),
            "turn_id": turn_id,
        }
    if "patch" in lowered or "file" in lowered:
        changes = payload.get("changes") or {}
        names = [Path(name).name for name in changes] if isinstance(changes, dict) else []
        return {
            "type": "fileChange",
            "text": "文件变更" + (f"：{', '.join(names[:4])}" if names else ""),
            "status": "completed" if lowered.endswith("end") else "started",
            "turn_id": turn_id,
        }
    if "search" in lowered:
        return {
            "type": "search",
            "text": str(payload.get("query") or "检索资料"),
            "status": "completed" if lowered.endswith("end") else "started",
            "turn_id": turn_id,
        }
    if "compact" in lowered:
        return {"type": "contextCompaction", "text": "上下文已压缩", "turn_id": turn_id}
    if any(token in lowered for token in ("tool", "mcp", "collab_agent")):
        return {"type": "toolCall", "text": payload_type, "status": payload.get("status", "started"), "turn_id": turn_id}
    return None


def inspect_rollout_boundary(path: str) -> tuple[int, Optional[dict], str]:
    """Scan backwards until the most recent turn boundary, without replaying history."""
    candidate = Path(path)
    try:
        size = candidate.stat().st_size
        position = size
        suffix = b""
        latest_phase = ""
        with candidate.open("rb") as handle:
            while position > 0:
                start = max(0, position - 131_072)
                handle.seek(start)
                data = handle.read(position - start) + suffix
                lines = data.split(b"\n")
                if start:
                    suffix = lines.pop(0)
                else:
                    suffix = b""
                for raw in reversed(lines):
                    if not raw.strip():
                        continue
                    try:
                        record = json.loads(raw)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if not latest_phase:
                        latest_phase = rollout_record_phase(record)
                    payload = record.get("payload") or {}
                    if record.get("type") == "event_msg" and payload.get("type") in {"task_started", "task_complete", "turn_aborted"}:
                        return size, record, latest_phase
                position = start
        return size, None, latest_phase
    except OSError:
        return 0, None, ""


def rollout_writer_pids(path: str, refresh: bool = False) -> set[int]:
    """Find live processes that currently hold a rollout file open for writing."""
    target = os.path.realpath(path)
    stamp = time.monotonic()
    cached = rollout_writer_cache.get(target)
    if cached and not refresh and stamp - cached[0] < 2:
        return set(cached[1])
    writers: set[int] = set()
    try:
        processes = os.scandir("/proc")
    except OSError:
        rollout_writer_cache[target] = (stamp, writers)
        return writers
    with processes:
        for process in processes:
            if not process.name.isdigit():
                continue
            fd_root = f"/proc/{process.name}/fd"
            try:
                descriptors = os.scandir(fd_root)
            except OSError:
                continue
            with descriptors:
                for descriptor in descriptors:
                    try:
                        linked = os.readlink(descriptor.path)
                        if linked.endswith(" (deleted)"):
                            linked = linked[:-10]
                        if os.path.realpath(linked) != target:
                            continue
                        with open(f"/proc/{process.name}/fdinfo/{descriptor.name}", encoding="ascii") as info:
                            flags_line = next((line for line in info if line.startswith("flags:")), "")
                        if not flags_line or int(flags_line.split()[1], 8) & os.O_ACCMODE == os.O_RDONLY:
                            continue
                        writers.add(int(process.name))
                        break
                    except (OSError, ValueError, StopIteration):
                        continue
    rollout_writer_cache[target] = (stamp, writers)
    return set(writers)


def native_rollout_writer_pids(path: str, refresh: bool = False) -> set[int]:
    """Return writers outside this dashboard's app-server children."""
    dashboard_pids = {
        client.process.pid
        for client in app_servers.values()
        if client.process and client.process.returncode is None
    }
    return rollout_writer_pids(path, refresh=refresh) - dashboard_pids - {os.getpid()}


def rollout_is_live(path: str) -> bool:
    """Treat a turn as live only while its writer exists, with a short startup grace."""
    try:
        age = max(0.0, time.time() - Path(path).stat().st_mtime)
    except OSError:
        return False
    if age <= EXTERNAL_TURN_GRACE_SECONDS:
        return True
    return bool(native_rollout_writer_pids(path))


def interrupt_rollout_writers(path: str) -> set[int]:
    writers = native_rollout_writer_pids(path, refresh=True)
    for pid in writers:
        try:
            command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
            if b"app-server" in command:
                continue
            os.kill(pid, signal.SIGINT)
        except OSError:
            continue
    return writers


def read_rollout_append(path: str, offset: int, remainder: bytes) -> tuple[int, bytes, list[dict]]:
    candidate = Path(path)
    try:
        size = candidate.stat().st_size
        if size < offset:
            offset, remainder = 0, b""
        if size == offset:
            return offset, remainder, []
        with candidate.open("rb") as handle:
            handle.seek(offset)
            data = handle.read()
        offset += len(data)
    except OSError:
        return offset, remainder, []
    chunks = (remainder + data).split(b"\n")
    remainder = chunks.pop() if chunks else b""
    records = []
    for raw in chunks:
        if not raw.strip():
            continue
        try:
            records.append(json.loads(raw))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
    return offset, remainder, records


def dashboard_owns_task(task_id: str) -> bool:
    turn_task = appserver_turn_tasks.get(task_id)
    return task_id in running or bool(turn_task and not turn_task.done())


def rollout_stamp(record: dict, field: str = "") -> str:
    payload = record.get("payload") or {}
    if field and payload.get(field) is not None:
        return native_stamp(payload.get(field), str(record.get("timestamp") or now()))
    return str(record.get("timestamp") or now())


def rollout_turn_id(record: dict) -> str:
    payload = record.get("payload") or {}
    metadata = payload.get("internal_chat_message_metadata_passthrough") or {}
    return str(payload.get("turn_id") or payload.get("turnId") or metadata.get("turn_id") or "")


def refresh_external_primary(task_id: str) -> Optional[dict]:
    turns = external_turn_sets.get(task_id) or {}
    if not turns:
        external_turn_sets.pop(task_id, None)
        external_turns.pop(task_id, None)
        return None
    primary = max(turns.values(), key=lambda turn: str(turn.get("started_at") or ""))
    external_turns[task_id] = primary
    return primary


def register_external_turn(task_id: str, turn: dict) -> dict:
    key = str(turn.get("turn_id") or turn.get("session_id") or turn.get("started_at") or uuid.uuid4())
    external_turn_sets.setdefault(task_id, {})[key] = turn
    return refresh_external_primary(task_id) or turn


def persist_external_task_status(task_id: str, dashboard_active: Optional[bool] = None) -> Optional[dict]:
    external = refresh_external_primary(task_id)
    if not external:
        return None
    task = db.one("SELECT active_session_id,run_mode FROM tasks WHERE id=?", (task_id,)) or {}
    if dashboard_active is None:
        dashboard_active = dashboard_owns_task(task_id)
    active_session_id = task.get("active_session_id") if dashboard_active else external.get("session_id")
    persisted_run_mode = str(task.get("run_mode") or "")
    # An explicit browser Goal start can adopt the already-running terminal
    # turn. Rollout polling must not erase that intent back to "terminal".
    run_mode = persisted_run_mode if dashboard_active or persisted_run_mode == "goal_resume" else "terminal"
    db.execute(
        "UPDATE tasks SET status='running',active_session_id=?,execution_source=?,execution_turn_id=?,run_mode=?,last_error='',updated_at=? WHERE id=?",
        (
            active_session_id,
            "mixed" if dashboard_active else "terminal",
            external.get("turn_id", ""),
            run_mode,
            now(),
            task_id,
        ),
    )
    return external


def remove_external_turn(task_id: str, turn_id: str) -> Optional[dict]:
    turns = external_turn_sets.get(task_id) or {}
    current = turns.pop(turn_id, None) if turn_id else (turns.pop(next(iter(turns))) if len(turns) == 1 else None)
    refresh_external_primary(task_id)
    return current


def clear_external_turns(task_id: str) -> list[dict]:
    turns = list((external_turn_sets.pop(task_id, None) or {}).values())
    external_turns.pop(task_id, None)
    return turns


def live_event_signature(payload: dict) -> str:
    event_type = str(payload.get("type") or "").lower()
    # Streaming deltas are ordered fragments. Repeated text fragments are
    # valid output, and rollout observation never produces delta events.
    if event_type == "agent_delta":
        return ""
    if event_type in {"agentmessage", "agent_message"}:
        return f"assistant:{str(payload.get('text') or payload.get('message') or '').strip()}"
    if event_type in {"usermessage", "user_message", "browsermessage"}:
        return f"user:{str(payload.get('text') or payload.get('message') or '').strip()}"
    item_id = str(payload.get("item_id") or payload.get("id") or "")
    if item_id:
        return f"item:{event_type}:{item_id}:{payload.get('status') or payload.get('phase') or ''}"
    return ""


def duplicate_live_event(task_id: str, payload: dict, remember: bool = True) -> bool:
    signature = live_event_signature(payload)
    if not signature:
        return False
    stamp = time.monotonic()
    signatures = recent_event_signatures.setdefault(task_id, {})
    for key, seen_at in list(signatures.items()):
        if stamp - seen_at > 12:
            signatures.pop(key, None)
    duplicate = signature in signatures
    if remember:
        signatures[signature] = stamp
    return duplicate


async def apply_external_turn_boundary(
    task_id: str,
    thread_id: str,
    record: dict,
    phase: str = "",
    initial: bool = False,
    path: str = "",
) -> None:
    payload = record.get("payload") or {}
    boundary = payload.get("type")
    turn_id = rollout_turn_id(record)
    if boundary == "task_started":
        stamp = rollout_stamp(record, "started_at")
        task = task_or_404(task_id)
        dashboard_turn_id = str(appserver_turn_ids.get(task_id) or "")
        persisted_dashboard_turn = task.get("execution_source") == "dashboard" and str(task.get("execution_turn_id") or "") == turn_id
        # A terminal and the dashboard can legitimately write the same Codex
        # turn when they attach to one thread. Only suppress a dashboard-only
        # boundary; a non-dashboard writer proves the terminal surface is live.
        terminal_writer = bool(path and native_rollout_writer_pids(path, refresh=True))
        if turn_id and (turn_id == dashboard_turn_id or persisted_dashboard_turn) and not terminal_writer:
            return
        session_id = f"external:{thread_id}:{turn_id or stamp}"
        db.execute(
            "INSERT OR IGNORE INTO sessions (id,task_id,status,attempt,command,started_at,codex_session_id,summary) VALUES (?,?,?,?,?,?,?,?)",
            (session_id, task_id, "running", 0, shlex.join([CODEX_BIN, "resume", thread_id]), stamp, thread_id, "External Codex turn"),
        )
        db.execute("UPDATE sessions SET status='running',finished_at=NULL,exit_code=NULL WHERE id=?", (session_id,))
        current = {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "started_at": stamp,
            "phase": phase or "正在分析任务",
            "session_id": session_id,
            "path": path,
        }
        register_external_turn(task_id, current)
        persist_external_task_status(task_id)
        await broadcast_task(task_id, {"type": "task_status", "task": task_or_404(task_id), "source": {"kind": "rollout", "surface": "terminal", "initial": initial}})
        await broadcast_overview(task_id, {"kind": "rollout", "surface": "terminal", "initial": initial})
        live_payload = {"type": "externalTurnStarted", "thread_id": thread_id, "turn_id": turn_id}
        db.execute("INSERT INTO events (session_id,ts,stream,payload) VALUES (?,?,?,?)", (session_id, stamp, "rollout", json.dumps(live_payload, ensure_ascii=False)))
        if not initial:
            await broadcast_task(task_id, {"type": "event", "session_id": session_id, "stream": "rollout", "payload": live_payload, "ts": stamp})
        return
    if boundary not in {"task_complete", "turn_aborted"}:
        return
    native_history_cache.pop(task_id, None)
    current = remove_external_turn(task_id, turn_id)
    if not current:
        return
    stamp = rollout_stamp(record, "completed_at")
    task = task_or_404(task_id)
    live_payload = {
        "type": "turn_completed" if boundary == "task_complete" else "turn_aborted",
        "status": "completed" if boundary == "task_complete" else str(payload.get("reason") or "interrupted"),
        "thread_id": thread_id,
        "turn_id": turn_id or current.get("turn_id", ""),
    }
    session_id = current.get("session_id") or f"external:{thread_id}:{live_payload['turn_id']}"
    session_status = "succeeded" if boundary == "task_complete" else "stopped"
    db.execute(
        "UPDATE sessions SET status=?,finished_at=?,exit_code=?,summary=? WHERE id=?",
        (session_status, stamp, 0 if boundary == "task_complete" else 130, "External Codex turn completed" if boundary == "task_complete" else "External Codex turn interrupted", session_id),
    )
    remaining = refresh_external_primary(task_id)
    dashboard_running = dashboard_owns_task(task_id)
    if remaining:
        persist_external_task_status(task_id, dashboard_active=dashboard_running)
    elif dashboard_running:
        db.execute(
            "UPDATE tasks SET status='running',execution_source=?,execution_turn_id=?,updated_at=? WHERE id=?",
            ("dashboard", str(appserver_turn_ids.get(task_id) or ""), stamp, task_id),
        )
    else:
        status = "archived" if task.get("archived") else ("available" if boundary == "task_complete" else "stopped")
        db.execute(
            "UPDATE tasks SET status=?,active_session_id=NULL,execution_source='',execution_turn_id='',updated_at=? WHERE id=?",
            (status, stamp, task_id),
        )
    db.execute("INSERT INTO events (session_id,ts,stream,payload) VALUES (?,?,?,?)", (session_id, stamp, "rollout", json.dumps(live_payload, ensure_ascii=False)))
    await broadcast_task(task_id, {"type": "event", "session_id": session_id, "stream": "rollout", "payload": live_payload, "ts": stamp})
    await broadcast_task(task_id, {"type": "task_status", "task": task_or_404(task_id), "source": {"kind": "external_rollout", "boundary": boundary}})
    await broadcast_overview(task_id, {"kind": "external_rollout", "boundary": boundary})
    schedule_task_drain(task_id)


async def settle_inactive_external_turn(task_id: str, reason: str, current: Optional[dict] = None) -> None:
    """Close an orphaned external turn so persisted state cannot stay running forever."""
    if dashboard_owns_task(task_id):
        return
    tracked = clear_external_turns(task_id)
    current = current or (tracked[-1] if tracked else None)
    task = task_or_404(task_id)
    if task.get("status") not in {"running", "retrying", "queued"}:
        return
    stamp = now()
    db.execute(
        "UPDATE sessions SET status='stopped',finished_at=COALESCE(finished_at,?),exit_code=COALESCE(exit_code,130),summary=? "
        "WHERE task_id=? AND status IN ('running','retrying')",
        (stamp, reason, task_id),
    )
    db.execute(
        "UPDATE tasks SET status='stopped',active_session_id=NULL,execution_source='',execution_turn_id='',run_mode='',last_error=?,updated_at=? WHERE id=?",
        (reason, stamp, task_id),
    )
    session_id = (current or {}).get("session_id")
    if session_id:
        payload = {"type": "turn_aborted", "status": "stopped", "reason": reason}
        db.execute(
            "INSERT INTO events (session_id,ts,stream,payload) VALUES (?,?,?,?)",
            (session_id, stamp, "rollout", json.dumps(payload, ensure_ascii=False)),
        )
        await broadcast_task(task_id, {"type": "event", "session_id": session_id, "stream": "rollout", "payload": payload, "ts": stamp})
    await broadcast_task(task_id, {"type": "task_status", "task": task_or_404(task_id), "source": {"kind": "rollout_reconcile"}})
    await broadcast_overview(task_id, {"kind": "rollout_reconcile"})
    schedule_task_drain(task_id)


async def reconcile_initial_rollout(task_id: str, thread_id: str, path: str, boundary: dict, phase: str) -> None:
    boundary_type = str((boundary.get("payload") or {}).get("type") or "")
    if boundary_type == "task_started" and await asyncio.to_thread(rollout_is_live, path):
        await apply_external_turn_boundary(task_id, thread_id, boundary, phase, initial=True, path=path)
        return
    task = task_or_404(task_id)
    if task.get("status") not in {"running", "retrying", "queued"} or dashboard_owns_task(task_id):
        return
    # A dashboard-owned goal interrupted by a service restart remains queued
    # for the normal auto-resume path. Native terminal turns have no owner id.
    if boundary_type == "task_started" and task.get("execution_source") == "dashboard" and task.get("active_session_id") and task.get("retry_forever") and task.get("goal"):
        return
    if boundary_type == "task_complete":
        stamp = rollout_stamp(boundary, "completed_at")
        status = "archived" if task.get("archived") else "available"
        db.execute(
            "UPDATE tasks SET status=?,active_session_id=NULL,execution_source='',execution_turn_id='',last_error='',updated_at=? WHERE id=?",
            (status, stamp, task_id),
        )
        await broadcast_task(task_id, {"type": "task_status", "task": task_or_404(task_id), "source": {"kind": "rollout_reconcile"}})
        await broadcast_overview(task_id, {"kind": "rollout_reconcile"})
        return
    await settle_inactive_external_turn(task_id, "Codex process ended without a completion event")


async def process_native_rollout_record(task_id: str, thread_id: str, record: dict, path: str = "") -> None:
    record_payload = record.get("payload") or {}
    payload_type = record_payload.get("type")
    if record.get("type") == "event_msg" and payload_type in {"task_started", "task_complete", "turn_aborted"}:
        await apply_external_turn_boundary(task_id, thread_id, record, rollout_record_phase(record), path=path)
        return
    if record.get("type") == "event_msg" and payload_type == "thread_goal_updated":
        goal = record_payload.get("goal") or {}
        db.execute(
            "UPDATE tasks SET goal=?,goal_status=?,goal_tokens_used=?,updated_at=? WHERE id=?",
            (goal.get("objective", ""), goal.get("status", "active"), int(goal.get("tokensUsed", 0) or 0), rollout_stamp(record), task_id),
        )
        await broadcast_task(task_id, {"type": "task_status", "task": task_or_404(task_id), "source": {"kind": "external_goal"}})
        return
    active = external_turns.get(task_id)
    if not active:
        return
    phase = rollout_record_phase(record)
    browser_payload = rollout_browser_payload(record)
    if phase:
        active["phase"] = phase
    if not browser_payload:
        return
    browser_payload["thread_id"] = thread_id
    if duplicate_live_event(task_id, browser_payload):
        return
    turn_id = str(browser_payload.get("turn_id") or active.get("turn_id") or "")
    session_id = active.get("session_id") or f"external:{thread_id}:{turn_id}"
    stamp = rollout_stamp(record)
    db.execute("INSERT INTO events (session_id,ts,stream,payload) VALUES (?,?,?,?)", (session_id, stamp, "rollout", json.dumps(browser_payload, ensure_ascii=False)))
    await broadcast_task(
        task_id,
        {
            "type": "event",
            "session_id": session_id,
            "stream": "rollout",
            "payload": browser_payload,
            "ts": stamp,
        },
    )


async def refresh_native_rollouts() -> None:
    global native_import_attempt_at
    rows = await asyncio.to_thread(native_rollout_rows)
    tasks = db.all("SELECT id,codex_session_id FROM tasks WHERE native=1 AND codex_session_id!=''")
    task_by_thread = {str(task["codex_session_id"]): task["id"] for task in tasks}
    unknown = [row["thread_id"] for row in rows if row["thread_id"] not in task_by_thread]
    loop_time = asyncio.get_running_loop().time()
    if unknown and USE_APP_SERVER and loop_time - native_import_attempt_at >= 2:
        native_import_attempt_at = loop_time
        before = {task["id"] for task in tasks}
        try:
            await sync_native_threads()
        except Exception:
            pass
        tasks = db.all("SELECT id,codex_session_id FROM tasks WHERE native=1 AND codex_session_id!=''")
        task_by_thread = {str(task["codex_session_id"]): task["id"] for task in tasks}
        for task in tasks:
            if task["id"] not in before:
                await broadcast_overview(task["id"], {"kind": "native_import"})
    live_paths = set()
    for row in rows:
        task_id = task_by_thread.get(row["thread_id"])
        path = row["path"]
        if not task_id or not path:
            continue
        live_paths.add(path)
        if path not in native_rollout_offsets:
            offset, boundary, phase = await asyncio.to_thread(inspect_rollout_boundary, path)
            native_rollout_offsets[path] = offset
            native_rollout_remainders[path] = b""
            if boundary:
                await reconcile_initial_rollout(task_id, row["thread_id"], path, boundary, phase)
            continue
        offset, remainder, records = await asyncio.to_thread(
            read_rollout_append,
            path,
            native_rollout_offsets[path],
            native_rollout_remainders.get(path, b""),
        )
        native_rollout_offsets[path] = offset
        native_rollout_remainders[path] = remainder
        for record in records:
            await process_native_rollout_record(task_id, row["thread_id"], record, path)
        active = external_turns.get(task_id)
        if active:
            persist_external_task_status(task_id)
        if active and not dashboard_owns_task(task_id) and not await asyncio.to_thread(rollout_is_live, path):
            await settle_inactive_external_turn(task_id, "Codex process ended without a completion event", active)
    for path in set(native_rollout_offsets) - live_paths:
        native_rollout_offsets.pop(path, None)
        native_rollout_remainders.pop(path, None)
        rollout_writer_cache.pop(os.path.realpath(path), None)


async def native_rollout_watch_loop() -> None:
    while True:
        try:
            await refresh_native_rollouts()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(0.35)


def memory_file(name: str) -> Path:
    clean = Path(name).as_posix()
    if not clean or clean.startswith("/") or clean in {".", ".."} or ".." in Path(clean).parts:
        raise HTTPException(400, "Invalid memory path")
    path = (CODEX_MEMORY_DIR / clean).resolve()
    if CODEX_MEMORY_DIR.resolve() not in path.parents or path.suffix.lower() not in {".md", ".markdown"}:
        raise HTTPException(400, "Memory files must be Markdown under CODEX_HOME/memories")
    return path


def workspace_path(task: dict, relative: str = "") -> tuple[Path, Path]:
    """Resolve a browser workspace path without allowing traversal."""
    root = Path(task["workspace"]).expanduser().resolve()
    candidate = (root / (relative or "")).resolve()
    if candidate != root and root not in candidate.parents:
        raise HTTPException(400, "Workspace path escapes the task workspace")
    return root, candidate


def workspace_picker_roots(task: dict) -> tuple[Path, ...]:
    roots = list(WORKSPACE_ROOTS)
    try:
        current = Path(task["workspace"]).expanduser().resolve()
    except OSError:
        current = None
    if current and current.is_dir() and not any(current == root or root in current.parents for root in roots):
        roots.append(current)
    return tuple(roots)


def workspace_picker_path(task: dict, value: str) -> tuple[Path, Path, tuple[Path, ...]]:
    roots = workspace_picker_roots(task)
    try:
        candidate = Path(value).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise HTTPException(400, f"Invalid workspace path: {exc}")
    boundaries = [root for root in roots if candidate == root or root in candidate.parents]
    if not boundaries:
        raise HTTPException(403, "Directory is outside the configured workspace roots")
    if not candidate.exists():
        raise HTTPException(404, "Workspace directory not found")
    if not candidate.is_dir():
        raise HTTPException(400, "Workspace path is not a directory")
    boundary = max(boundaries, key=lambda path: len(path.parts))
    return boundary, candidate, roots


def workspace_upload_name(filename: str) -> str:
    value = filename.strip()
    if not value or value in {".", ".."} or "/" in value or "\\" in value or Path(value).name != value:
        raise HTTPException(400, "Upload filename must be a single file name")
    return value


def workspace_entry(path: Path, root: Path) -> dict:
    try:
        stat = path.stat()
    except OSError:
        return {"name": path.name, "path": path.relative_to(root).as_posix(), "kind": "unavailable"}
    relative = path.relative_to(root).as_posix()
    return {
        "name": path.name,
        "path": "" if relative == "." else relative,
        "kind": "directory" if path.is_dir() else "file",
        "size": stat.st_size if path.is_file() else None,
        "updated_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
    }


def workspace_hidden(path: Path) -> bool:
    name = path.name.lower()
    return name in {".env", "credentials", "credentials.json", "secrets.json"} or path.suffix.lower() in {".key", ".pem", ".p12", ".pfx"}


def validate_ssh_host(host: str) -> str:
    value = host.strip()
    if not value or value.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.@%:+-]+", value):
        raise HTTPException(400, "Invalid SSH host alias")
    return value


def ssh_config_aliases(path: Path = SSH_CONFIG, seen: Optional[set[Path]] = None) -> list[str]:
    """Return concrete Host aliases while following OpenSSH Include files."""
    seen = seen or set()
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        return []
    if resolved in seen or not resolved.is_file():
        return []
    seen.add(resolved)
    aliases: list[str] = []
    try:
        lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            parts = shlex.split(line, comments=True, posix=True)
        except ValueError:
            continue
        if not parts:
            continue
        keyword = parts[0].lower()
        if keyword == "include":
            for pattern in parts[1:]:
                expanded = Path(pattern).expanduser()
                if not expanded.is_absolute():
                    expanded = Path.home() / ".ssh" / expanded
                for match in sorted(glob.glob(str(expanded))):
                    aliases.extend(ssh_config_aliases(Path(match), seen))
        elif keyword == "host":
            for alias in parts[1:]:
                if alias.startswith("!") or any(marker in alias for marker in "*?"):
                    continue
                try:
                    aliases.append(validate_ssh_host(alias))
                except HTTPException:
                    continue
    return list(dict.fromkeys(aliases))


def ssh_target_parts(value: str) -> tuple[str, str, int, bool]:
    """Split a direct user@host[:port] target while preserving config aliases."""
    raw = value.strip()
    user = ""
    host = raw
    if "@" in host:
        user, host = host.rsplit("@", 1)
    port = 22
    direct = bool(user or (host.count(":") == 1 and host.rsplit(":", 1)[-1].isdigit()))
    if host.count(":") == 1 and host.rsplit(":", 1)[-1].isdigit():
        host, raw_port = host.rsplit(":", 1)
        port = int(raw_port)
    if not direct:
        return "", raw, 22, False
    return user, host, port, True


def ssh_destination(value: str) -> str:
    user, host, _port, direct = ssh_target_parts(value)
    return f"{user}@{host}" if direct and user else host


def ssh_control_path(host: str) -> Path:
    digest = hashlib.sha256(host.encode()).hexdigest()[:20]
    return SSH_RUNTIME_DIR / f"cm-{digest}"


def ssh_options(host: str, *, batch: bool = True, tty: bool = False) -> list[str]:
    options = [
        SSH_BIN,
        "-o", f"ControlPath={ssh_control_path(host)}",
        "-o", "ControlMaster=auto",
        "-o", "ControlPersist=600",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=3",
        "-o", f"BatchMode={'yes' if batch else 'no'}",
    ]
    user, _hostname, port, direct = ssh_target_parts(host)
    if direct and user:
        options.extend(["-l", user])
    if direct and port != 22:
        options.extend(["-p", str(port)])
    options.append("-tt" if tty else "-T")
    return options


def ssh_effective_config(host: str) -> dict[str, Any]:
    host = validate_ssh_host(host)
    user, hostname, port, direct = ssh_target_parts(host)
    try:
        command = [SSH_BIN, "-G"]
        if direct and user:
            command += ["-l", user]
        if direct and port != 22:
            command += ["-p", str(port)]
        command.append(hostname if direct else host)
        result = subprocess.run(command, capture_output=True, text=True, timeout=4, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"alias": host, "hostname": host, "user": "", "port": 22, "identity_files": [], "error": str(exc)}
    values: dict[str, list[str]] = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition(" ")
        if key and value:
            values.setdefault(key.lower(), []).append(value.strip())
    try:
        port = int((values.get("port") or ["22"])[0])
    except ValueError:
        port = 22
    return {
        "alias": host,
        "hostname": (values.get("hostname") or [host])[0],
        "user": (values.get("user") or [""])[0],
        "port": port,
        "identity_files": [str(Path(value).expanduser()) for value in values.get("identityfile", [])],
        "proxy_jump": (values.get("proxyjump") or [""])[0],
    }


def ssh_master_alive(host: str) -> bool:
    control = ssh_control_path(host)
    if not control.exists():
        return False
    try:
        result = subprocess.run(
            [SSH_BIN, "-o", f"ControlPath={control}", "-O", "check", ssh_destination(host)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def clean_ssh_error(value: str) -> str:
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    return (lines[-1] if lines else "SSH connection failed")[-500:]


def ssh_failure_status(value: str) -> str:
    lowered = value.lower()
    authentication_markers = ("permission denied", "authentication failed", "no supported authentication methods")
    return "needs_password" if any(marker in lowered for marker in authentication_markers) else "failed"


def ssh_capture(host: str, remote_command: str, timeout: int = 12) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*ssh_options(host, batch=True), ssh_destination(host), remote_command],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def remote_codex_discovery(host: str) -> dict[str, str]:
    home_result = ssh_capture(host, "printf '%s' \"$HOME\"")
    remote_home = home_result.stdout.strip() if home_result.returncode == 0 else ""
    python_result = ssh_capture(host, "command -v python3 || command -v python || true")
    python_bin = python_result.stdout.strip().splitlines()[0] if python_result.stdout.strip() else ""
    discovery_script = r'''
if command -v codex >/dev/null 2>&1; then command -v codex; exit 0; fi
for root in "$HOME/.vscode/extensions" "$HOME/.vscode-server/extensions" "$HOME/.vscode-insiders/extensions" "$HOME/.vscode-server-insiders/extensions"; do
  [ -d "$root" ] || continue
  found=$(find "$root" -type f \( -path '*/openai.chatgpt-*/bin/*/codex' -o -path '*/openai.chatgpt-*/bin/*/codex.exe' \) -perm -u+x 2>/dev/null | sort -V | tail -n 1)
  [ -n "$found" ] && { printf '%s\n' "$found"; exit 0; }
done
for candidate in "$HOME/.local/share/codex-dashboard/npm/bin/codex" "$HOME/.codex/bin/codex" "$HOME/.local/bin/codex" "$HOME/.npm-global/bin/codex" /usr/local/bin/codex /opt/homebrew/bin/codex /home/linuxbrew/.linuxbrew/bin/codex /usr/bin/codex; do
  [ -x "$candidate" ] && { printf '%s\n' "$candidate"; exit 0; }
done
exit 1
'''.strip()
    codex_result = ssh_capture(host, shlex.join(["sh", "-lc", discovery_script]))
    codex_bin = codex_result.stdout.strip().splitlines()[0] if codex_result.returncode == 0 and codex_result.stdout.strip() else ""
    if codex_bin:
        verify = ssh_capture(host, shlex.join([codex_bin, "--version"]))
        if verify.returncode != 0:
            codex_bin = ""
    return {"remote_home": remote_home, "python_bin": python_bin, "codex_bin": codex_bin}


def password_ssh_connect(host: str, password: str) -> tuple[bool, str]:
    args = [
        *ssh_options(host, batch=False),
        "-o", "ConnectTimeout=10",
        "-o", "NumberOfPasswordPrompts=1",
        ssh_destination(host),
        "true",
    ]
    child = pexpect.spawn(args[0], args[1:], encoding="utf-8", timeout=15)
    output: list[str] = []
    sent = False
    try:
        while True:
            matched = child.expect([r"(?i)(?:password|passphrase).*:", r"(?i)are you sure you want to continue connecting", pexpect.EOF, pexpect.TIMEOUT])
            output.append(child.before or "")
            if matched == 0:
                if sent:
                    child.close(force=True)
                    return False, "SSH password or key passphrase was rejected"
                child.sendline(password)
                sent = True
                continue
            if matched == 1:
                child.close(force=True)
                return False, "SSH host key is not trusted; verify it with the system ssh command first"
            if matched == 3:
                child.close(force=True)
                return False, "SSH connection timed out"
            child.close()
            return child.exitstatus == 0, clean_ssh_error("\n".join(output))
    finally:
        if child.isalive():
            child.close(force=True)


def connect_ssh_host_sync(host: str, password: str = "") -> dict[str, Any]:
    host = validate_ssh_host(host)
    details = ssh_effective_config(host)
    if not ssh_master_alive(host):
        try:
            automatic = subprocess.run(
        [*ssh_options(host, batch=True), "-o", "ConnectTimeout=7", ssh_destination(host), "true"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=12,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            error = clean_ssh_error((exc.stderr or "") if isinstance(exc.stderr, str) else "SSH connection timed out")
            row = {**details, "status": "failed", "connected": False, "last_error": error}
            ssh_connection_cache[host] = row
            return row
        if automatic.returncode != 0:
            if not password:
                row = {**details, "status": ssh_failure_status(automatic.stderr), "connected": False, "last_error": clean_ssh_error(automatic.stderr)}
                ssh_connection_cache[host] = row
                return row
            connected, error = password_ssh_connect(host, password)
            if not connected:
                row = {**details, "status": "failed", "connected": False, "last_error": error}
                ssh_connection_cache[host] = row
                return row
    metadata = remote_codex_discovery(host)
    row = {
        **details,
        **metadata,
        "status": "connected" if metadata.get("codex_bin") else "connected_no_codex",
        "connected": True,
        "last_error": "" if metadata.get("codex_bin") else "Remote Codex CLI was not found",
        "connected_at": now(),
    }
    ssh_connection_cache[host] = row
    return row


async def connect_ssh_host(host: str, password: str = "") -> dict[str, Any]:
    host = validate_ssh_host(host)
    lock = ssh_connect_locks.setdefault(host, asyncio.Lock())
    async with lock:
        return await asyncio.to_thread(connect_ssh_host_sync, host, password)


async def require_ssh_connection(host: str, *, codex: bool = False) -> dict[str, Any]:
    result = await connect_ssh_host(host)
    if not result.get("connected"):
        raise HTTPException(428, f"SSH host {host} requires a password or unlocked private key")
    if codex and not result.get("codex_bin"):
        raise HTTPException(503, f"Codex CLI was not found on SSH host {host}")
    return result


def remote_workspace_path(value: str, remote_home: str) -> str:
    candidate = (value or remote_home or "/").strip()
    if candidate == "~":
        candidate = remote_home
    elif candidate.startswith("~/"):
        candidate = posixpath.join(remote_home, candidate[2:])
    if not candidate.startswith("/") or "\0" in candidate:
        raise HTTPException(400, "Remote workspace must be an absolute POSIX path")
    return posixpath.normpath(candidate)


REMOTE_FS_SCRIPT = r'''
import json, os, sys
from datetime import datetime, timezone
from pathlib import Path

def fail(status, message):
    print(json.dumps({"status": status, "error": message}, ensure_ascii=False))
    raise SystemExit(1)

def hidden(path):
    name = path.name.lower()
    return name in {".env", "credentials", "credentials.json", "secrets.json"} or path.suffix.lower() in {".key", ".pem", ".p12", ".pfx"}

def entry(path, root):
    stat = path.stat()
    relative = path.relative_to(root).as_posix()
    return {
        "name": path.name,
        "path": "" if relative == "." else relative,
        "kind": "directory" if path.is_dir() else "file",
        "size": stat.st_size if path.is_file() else None,
        "updated_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
    }

action, root_value, path_value = sys.argv[1:4]
root = Path(root_value).expanduser().resolve()
if action == "picker":
    candidate = Path(path_value).expanduser().resolve()
    if candidate != root and root not in candidate.parents:
        fail(403, "Directory is outside the remote workspace root")
    if not candidate.exists(): fail(404, "Remote directory not found")
    if not candidate.is_dir(): fail(400, "Remote path is not a directory")
    rows = []
    for child in sorted(candidate.iterdir(), key=lambda item: item.name.lower()):
        try:
            resolved = child.resolve()
            if child.name.startswith(".") or not child.is_dir() or (resolved != root and root not in resolved.parents):
                continue
            rows.append({"name": child.name, "path": str(resolved)})
        except OSError:
            continue
        if len(rows) >= 300: break
    print(json.dumps({"path": str(candidate), "parent": str(candidate.parent) if candidate != root else None, "entries": rows}, ensure_ascii=False))
    raise SystemExit(0)

target = (root / path_value).resolve()
if target != root and root not in target.parents:
    fail(400, "Workspace path escapes the task workspace")
if not target.exists(): fail(404, "Remote workspace path not found")
if hidden(target): fail(403, "Sensitive workspace files are not available in the browser")
if action == "browse":
    if target.is_file():
        size = target.stat().st_size
        if size > 512000: fail(413, "File is too large for the browser preview")
        raw = target.read_bytes()
        editable = b"\0" not in raw
        try: content = raw.decode("utf-8")
        except UnicodeDecodeError:
            content = raw.decode("utf-8", errors="replace")
            editable = False
        print(json.dumps({"root": str(root), "entry": entry(target, root), "content": content, "editable": editable}, ensure_ascii=False))
    else:
        rows = []
        for child in sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
            if child.name in {".git", ".venv", "node_modules", "__pycache__"} or hidden(child): continue
            try:
                resolved = child.resolve()
                if resolved != root and root not in resolved.parents: continue
                rows.append(entry(child, root))
            except OSError: continue
            if len(rows) >= 300: break
        print(json.dumps({"root": str(root), "entry": entry(target, root), "entries": rows}, ensure_ascii=False))
elif action == "stat":
    print(json.dumps({"root": str(root), "entry": entry(target, root)}, ensure_ascii=False))
else:
    fail(400, "Unknown remote filesystem action")
'''.strip()


REMOTE_WRITE_SCRIPT = r'''
import json, os, sys, uuid
from datetime import datetime, timezone
from pathlib import Path

def fail(status, message):
    print(json.dumps({"status": status, "error": message}, ensure_ascii=False))
    raise SystemExit(1)

root_value, path_value, overwrite_value = sys.argv[1:4]
root = Path(root_value).expanduser().resolve()
target = (root / path_value).resolve()
if target != root and root not in target.parents: fail(400, "Workspace path escapes the task workspace")
name = target.name.lower()
if name in {".env", "credentials", "credentials.json", "secrets.json"} or target.suffix.lower() in {".key", ".pem", ".p12", ".pfx"}: fail(403, "Sensitive workspace files are not available for browser editing")
if not target.parent.is_dir(): fail(404, "Remote destination directory not found")
if target.exists() and target.is_dir(): fail(409, "A directory already uses this name")
if target.exists() and overwrite_value != "1": fail(409, "A file with this name already exists")
mode = target.stat().st_mode & 0o7777 if target.exists() else 0o644
temporary = target.parent / ("." + target.name + ".write-" + uuid.uuid4().hex)
total = 0
try:
    with temporary.open("xb") as handle:
        while True:
            chunk = sys.stdin.buffer.read(1024 * 1024)
            if not chunk: break
            total += len(chunk)
            handle.write(chunk)
        handle.flush(); os.fsync(handle.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, target)
finally:
    try: temporary.unlink()
    except FileNotFoundError: pass
stat = target.stat()
relative = target.relative_to(root).as_posix()
entry = {"name": target.name, "path": relative, "kind": "file", "size": stat.st_size, "updated_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()}
print(json.dumps({"ok": True, "entry": entry, "bytes": total}, ensure_ascii=False))
'''.strip()


async def remote_fs_json(task: dict, action: str, path: str = "") -> dict[str, Any]:
    host = task.get("ssh_host") or ""
    connection = await require_ssh_connection(host)
    python_bin = connection.get("python_bin")
    if not python_bin:
        raise HTTPException(503, f"Python 3 was not found on SSH host {host}")
    root = task["workspace"]
    command = shlex.join([python_bin, "-c", REMOTE_FS_SCRIPT, action, root, path])
    result = await asyncio.to_thread(ssh_capture, host, command, 20)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = {}
    if result.returncode != 0:
        raise HTTPException(int(payload.get("status", 502)), payload.get("error") or clean_ssh_error(result.stderr))
    return payload


async def remote_write_stream(task: dict, path: str, request: Request, overwrite: bool) -> dict[str, Any]:
    host = task.get("ssh_host") or ""
    connection = await require_ssh_connection(host)
    python_bin = connection.get("python_bin")
    if not python_bin:
        raise HTTPException(503, f"Python 3 was not found on SSH host {host}")
    command = shlex.join([python_bin, "-c", REMOTE_WRITE_SCRIPT, task["workspace"], path, "1" if overwrite else "0"])
    process = await asyncio.create_subprocess_exec(
        *ssh_options(host, batch=True), ssh_destination(host), command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=4 * 1024 * 1024,
    )
    assert process.stdin and process.stdout and process.stderr
    try:
        async for chunk in request.stream():
            process.stdin.write(chunk)
            await process.stdin.drain()
        process.stdin.close()
        stdout, stderr = await process.communicate()
    except BaseException:
        process.kill()
        await process.wait()
        raise
    try:
        payload = json.loads(stdout.decode(errors="replace"))
    except json.JSONDecodeError:
        payload = {}
    if process.returncode != 0:
        raise HTTPException(int(payload.get("status", 502)), payload.get("error") or clean_ssh_error(stderr.decode(errors="replace")))
    return payload


def disconnect_ssh_host(host: str) -> None:
    host = validate_ssh_host(host)
    subprocess.run(
        [SSH_BIN, "-o", f"ControlPath={ssh_control_path(host)}", "-O", "exit", ssh_destination(host)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=4,
        check=False,
    )
    ssh_control_path(host).unlink(missing_ok=True)
    ssh_connection_cache.pop(host, None)


def ssh_host_row(host: str) -> dict[str, Any]:
    details = ssh_effective_config(host)
    cached = ssh_connection_cache.get(host, {})
    connected = ssh_master_alive(host)
    status = cached.get("status") if connected else (cached.get("status") if cached.get("status") in {"needs_password", "failed"} else "disconnected")
    return {**details, **cached, "connected": connected, "status": status or "disconnected"}


def configured_ssh_hosts() -> list[str]:
    if SSH_FIXED_HOST:
        target = SSH_FIXED_HOST
        if SSH_FIXED_USER and "@" not in target:
            target = f"{SSH_FIXED_USER}@{target}"
        if SSH_FIXED_PORT != 22 and ":" not in target.rsplit("@", 1)[-1]:
            target = f"{target}:{SSH_FIXED_PORT}"
        return [validate_ssh_host(target)]
    saved_hosts = [row["alias"] for row in db.all("SELECT alias FROM ssh_saved_hosts ORDER BY created_at")]
    task_hosts = [row["ssh_host"] for row in db.all("SELECT DISTINCT ssh_host FROM tasks WHERE COALESCE(ssh_host,'')!=''")]
    return list(dict.fromkeys([*ssh_config_aliases(), *saved_hosts, *task_hosts]))


def memory_rows(query: str = "") -> list[dict]:
    if not CODEX_MEMORY_DIR.is_dir():
        return []
    needle = query.strip().lower()
    rows = []
    for path in sorted(CODEX_MEMORY_DIR.rglob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            stat = path.stat()
        except OSError:
            continue
        rel = path.relative_to(CODEX_MEMORY_DIR).as_posix()
        if needle and needle not in rel.lower() and needle not in content.lower():
            continue
        rows.append({"name": rel, "size": stat.st_size, "updated_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(), "preview": content[:320]})
    return rows


def generated_memory_rows(query: str = "") -> list[dict]:
    path = CODEX_HOME / "memories_1.sqlite"
    if not path.is_file():
        return []
    needle = query.strip().lower()
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT thread_id,source_updated_at,rollout_summary,rollout_slug,generated_at,usage_count,last_usage,selected_for_phase2 "
            "FROM stage1_outputs ORDER BY generated_at DESC"
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return []
    result = []
    for thread_id, source_updated_at, summary, slug, generated_at, usage_count, last_usage, selected in rows:
        searchable = f"{thread_id} {slug or ''} {summary or ''}".lower()
        if needle and needle not in searchable:
            continue
        result.append({
            "thread_id": thread_id,
            "slug": slug or "",
            "source_updated_at": source_updated_at,
            "generated_at": generated_at,
            "usage_count": usage_count or 0,
            "last_usage": last_usage,
            "selected_for_phase2": bool(selected),
            "preview": (summary or "")[:500],
        })
    return result


def native_stamp(value: Any, fallback: str) -> str:
    if isinstance(value, str):
        text = value.strip()
        if text:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc).isoformat()
            except ValueError:
                pass
    try:
        seconds = float(value)
        if seconds > 10_000_000_000:
            seconds /= 1000
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return fallback


async def sync_native_threads() -> dict:
    """Import the local Codex thread index into the dashboard task index."""
    async with native_sync_lock:
        if not USE_APP_SERVER:
            return {"imported": 0, "updated": 0, "available": False}
        provider_result = sync_native_providers()
        client = await appserver_for(None)
        threads: list[dict] = []
        for archived in (False, True):
            cursor = None
            seen_cursors: set[str] = set()
            for _ in range(100):
                params: dict[str, Any] = {"limit": 100, "archived": archived}
                if cursor:
                    params["cursor"] = cursor
                result = await client.request("thread/list", params)
                batch = result.get("data") or []
                for thread in batch:
                    thread["archived"] = archived
                threads.extend(batch)
                cursor = result.get("nextCursor")
                if not cursor or cursor in seen_cursors or not batch:
                    break
                seen_cursors.add(cursor)
        threads = list({str(thread.get("id")): thread for thread in threads if thread.get("id")}.values())
        goals = native_goal_rows()
        settings = native_thread_settings()
        providers = {row.get("model_provider"): row["id"] for row in db.all("SELECT id,model_provider FROM providers") if row.get("model_provider")}
        imported = updated = 0
        for thread in threads:
            thread_id = str(thread.get("id") or "").strip()
            if not thread_id or thread.get("ephemeral"):
                continue
            created = native_stamp(thread.get("createdAt"), now())
            updated_at = native_stamp(thread.get("updatedAt") or thread.get("recencyAt"), created)
            title = (thread.get("name") or thread.get("preview") or thread.get("firstUserMessage") or "Codex session").strip()
            prompt = (thread.get("firstUserMessage") or thread.get("preview") or title).strip()
            workspace = str(Path(thread.get("cwd") or str(Path.home())).expanduser().resolve())
            goal = goals.get(thread_id) or {}
            objective = goal.get("objective", "")
            goal_status = goal.get("status", "none" if not objective else "active")
            model_provider = thread.get("modelProvider") or ""
            native_settings = settings.get(thread_id, {})
            model_provider = model_provider or native_settings.get("model_provider") or ""
            model = thread.get("model") or native_settings.get("model") or ""
            yolo = str(native_settings.get("approval_mode") or "").lower() in {"never", "off"}
            archived = bool(thread.get("archived") or native_settings.get("archived"))
            memory_mode = str(native_settings.get("memory_mode") or "enabled")
            provider_id = providers.get(model_provider)
            existing = db.one("SELECT id,status,retry_forever,retry_explicit,provider_id,model FROM tasks WHERE codex_session_id=?", (thread_id,))
            if existing:
                # Imported native threads may be edited from the dashboard. Do
                # not let the periodic native index sync erase those dashboard
                # choices before the next turn can use them.
                provider_id = existing.get("provider_id")
                model = existing.get("model", "")
                aligned_workspace = align_session_workspace(existing["id"], thread_id)
                if aligned_workspace:
                    workspace = str(aligned_workspace)
                existing_full = db.one("SELECT trashed FROM tasks WHERE id=?", (existing["id"],)) or {}
                status = "trashed" if existing_full.get("trashed") else (existing["status"] if existing["status"] in {"running", "retrying", "queued", "stopped"} else ("archived" if archived else "available"))
                retry_forever = int(existing.get("retry_forever", 0)) if existing.get("retry_explicit") else int(bool(objective))
                db.execute(
                    "UPDATE tasks SET name=?,prompt=?,goal=?,workspace=?,model=?,provider_id=?,goal_status=?,goal_tokens_used=?,"
                    "retry_forever=?,yolo=?,native=1,archived=?,memory_mode=?,status=?,updated_at=? WHERE id=?",
                    (title[:160], prompt, objective, workspace, model, provider_id, goal_status, int(goal.get("tokensUsed", 0) or 0),
                     retry_forever, int(yolo), int(archived), memory_mode, status, updated_at, existing["id"]),
                )
                persist_native_thread_model(thread_id, model)
                task_id = existing["id"]
                task_id = canonicalize_task_thread_id(task_id, thread_id)
                updated += 1
            else:
                task_id = thread_id
                if db.one("SELECT id FROM tasks WHERE id=?", (task_id,)):
                    task_id = str(uuid.uuid4())
                db.execute(
                    "INSERT INTO tasks (id,name,prompt,goal,workspace,status,yolo,max_retries,retry_forever,provider_id,model,context,codex_session_id,goal_status,created_at,updated_at,native,archived,memory_mode) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (task_id, title[:160], prompt, objective, workspace, "archived" if archived else "available", int(yolo), 3, int(bool(objective)), provider_id, model, "", thread_id, goal_status, created, updated_at, 1, int(archived), memory_mode),
                )
                persist_native_thread_model(thread_id, model)
                imported += 1
            if not db.one("SELECT id FROM sessions WHERE task_id=? AND codex_session_id=?", (task_id, thread_id)):
                session_id = thread_id if not db.one("SELECT id FROM sessions WHERE id=?", (thread_id,)) else str(uuid.uuid4())
                db.execute(
                    "INSERT INTO sessions (id,task_id,status,attempt,provider_id,command,started_at,finished_at,exit_code,summary,codex_session_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (session_id, task_id, "imported", 0, model_provider or None, shlex.join([CODEX_BIN, "app-server", "thread/resume", thread_id]), created, updated_at, 0, "Imported from local Codex", thread_id),
                )
        return {"imported": imported, "updated": updated, "available": True, "threads": len(threads), "providers": provider_result}


async def native_history_events(task: dict) -> list[dict]:
    if not task.get("native") or not task.get("codex_session_id"):
        return []
    cached = native_history_cache.get(task["id"])
    if cached and cached.get("thread_id") == task["codex_session_id"]:
        return cached["rows"]
    lock = native_history_locks.setdefault(task["id"], asyncio.Lock())
    async with lock:
        cached = native_history_cache.get(task["id"])
        if cached and cached.get("thread_id") == task["codex_session_id"]:
            return cached["rows"]
        client = await appserver_for(None)
        result = await client.request("thread/read", {"threadId": task["codex_session_id"], "includeTurns": True})
        thread = result.get("thread") or {}
        session = db.one("SELECT id FROM sessions WHERE task_id=? ORDER BY attempt LIMIT 1", (task["id"],))
        session_id = (session or {}).get("id", "")
        rows = []
        for turn in thread.get("turns") or []:
            for item in turn.get("items") or []:
                item_type = item.get("type", "item")
                text = item.get("text", "")
                if not text and item_type == "userMessage":
                    text = "\n".join(
                        part.get("text", "") for part in item.get("content", []) if isinstance(part, dict) and part.get("text")
                    )
                if not text and item_type in {"reasoning", "plan"}:
                    text = json.dumps(item.get("summary") or item.get("content") or item, ensure_ascii=False)
                if not text:
                    if item_type == "fileChange":
                        changes = item.get("changes") or []
                        text = "\n".join(
                            f"{change.get('kind', 'update')}: {change.get('path', '')}" for change in changes if isinstance(change, dict)
                        ) or f"file changes: {item.get('status', 'unknown')}"
                    elif item_type == "commandExecution":
                        command = item.get("command") or ""
                        text = f"{command}\n{item.get('aggregatedOutput') or ''}".strip()
                    elif item_type == "contextCompaction":
                        text = "Context compacted"
                    else:
                        text = json.dumps(item, ensure_ascii=False)[:12000]
                rows.append({
                    "id": f"native-{turn.get('id', '')}-{item.get('id', len(rows))}",
                    "session_id": session_id,
                    "ts": native_stamp(turn.get("completedAt") or turn.get("startedAt"), now()),
                    "stream": "native",
                    "payload": json.dumps({
                        "type": item_type,
                        "text": text,
                        "item_id": item.get("id", ""),
                        "client_message_id": item.get("clientId"),
                        "turn_id": turn.get("id"),
                        "status": turn.get("status"),
                    }, ensure_ascii=False),
                })
        native_history_cache[task["id"]] = {
            "thread_id": task["codex_session_id"],
            "rows": rows,
            "built_at": time.monotonic(),
        }
        return rows


async def handle_appserver_notification(server_key: str, message: dict) -> None:
    method, params = message.get("method", ""), message.get("params") or {}
    thread_id = params.get("threadId") or (params.get("thread") or {}).get("id")
    if not thread_id:
        return
    binding = next(((task_id, sid) for task_id, (key, tid, sid) in app_thread_bindings.items() if key == server_key and tid == thread_id), None)
    if not binding:
        return
    task_id, session_id = binding
    if method == "item/agentMessage/delta":
        payload = {
            "type": "agent_delta",
            "delta": params.get("delta", ""),
            "item_id": params.get("itemId", ""),
            "turn_id": params.get("turnId", ""),
            "thread_id": thread_id,
        }
    elif method in {"item/started", "item/completed"}:
        item = params.get("item") or {}
        item_type = item.get("type", "item")
        item_id = item.get("id", "")
        if item_type == "userMessage":
            if method == "item/started":
                return
            text = item.get("text", "") or "\n".join(
                part.get("text", "")
                for part in item.get("content", [])
                if isinstance(part, dict) and part.get("text")
            )
            payload = {
                "type": "userMessage",
                "text": text,
                "item_id": item_id,
                "client_message_id": item.get("clientId"),
                "turn_id": params.get("turnId", ""),
                "thread_id": thread_id,
                "phase": "completed" if method == "item/completed" else "started",
            }
        elif item_type == "agentMessage":
            payload = {
                "type": "agentMessage" if method == "item/completed" else "agentMessageStarted",
                "text": item.get("text", ""),
                "item_id": item_id,
                "turn_id": params.get("turnId", ""),
                "thread_id": thread_id,
            }
        else:
            payload = {
                "type": item_type,
                "text": item.get("text", ""),
                "item_id": item_id,
                "status": "completed" if method == "item/completed" else "started",
                "turn_id": params.get("turnId", ""),
                "thread_id": thread_id,
                "item": item,
            }
    elif method == "thread/goal/updated":
        goal = params.get("goal") or {}
        payload = {"type": "goal_updated", "goal": goal, "thread_id": thread_id}
        objective = goal.get("objective")
        fields = ["goal_status=?", "goal_tokens_used=?", "updated_at=?"]
        values: list[Any] = [goal.get("status", "active"), goal.get("tokensUsed", 0), now()]
        if objective is not None:
            fields.insert(0, "goal=?")
            values.insert(0, str(objective or ""))
        values.append(task_id)
        db.execute(
            f"UPDATE tasks SET {','.join(fields)} WHERE id=?",
            tuple(values),
        )
    elif method == "turn/completed":
        turn = params.get("turn") or {}
        turn_status = turn.get("status") or "completed"
        payload = {"type": "turn_completed", "status": turn_status, "thread_id": thread_id}
        native_history_cache.pop(task_id, None)
        waiter = turn_waiters.pop(thread_id, None)
        if waiter and not waiter.done():
            if turn_status in {"failed", "interrupted"}:
                waiter.set_exception(RuntimeError(json.dumps(turn.get("error") or {"status": turn_status}, ensure_ascii=False)))
            else:
                waiter.set_result(turn)
    else:
        payload = {"type": "codex", "method": method, "params": params}
    stamp = now()
    if duplicate_live_event(task_id, payload):
        return
    db.execute("INSERT INTO events (session_id,ts,stream,payload) VALUES (?,?,?,?)", (session_id, stamp, "app-server", json.dumps(payload, ensure_ascii=False)))
    runtime_metric_cache.pop(task_id, None)
    await broadcast_task(task_id, {"type": "event", "session_id": session_id, "stream": "app-server", "payload": payload, "ts": stamp})


async def handle_appserver_server_request(server_key: str, message: dict) -> None:
    """Route Codex approval/input RPCs to every browser viewing the task."""
    params = message.get("params") or {}
    thread_id = params.get("threadId")
    binding = next(((task_id, sid) for task_id, (key, tid, sid) in app_thread_bindings.items() if key == server_key and tid == thread_id), None)
    client = app_servers.get(server_key)
    if not client:
        return
    method, request_id = message.get("method", ""), message.get("id")
    public_id = ""
    allow = bool(binding and task_or_404(binding[0]).get("yolo"))
    approval_methods = {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "item/permissions/requestApproval",
    }
    if not binding or method not in approval_methods | {"item/tool/requestUserInput"}:
        result = {"decision": "decline"} if method in approval_methods else {}
    elif allow and method in approval_methods:
        result = {"permissions": params.get("permissions") or {}, "scope": "turn"} if method == "item/permissions/requestApproval" else {"decision": "accept"}
    else:
        public_id = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        request = {
            "id": public_id,
            "task_id": binding[0],
            "server_key": server_key,
            "request_id": request_id,
            "method": method,
            "params": params,
            "created_at": now(),
            "future": future,
        }
        pending_appserver_requests[public_id] = request
        await broadcast_task(binding[0], {"type": "server_request", "request": public_server_request(request)})
        try:
            result = await future
        except asyncio.CancelledError:
            pending_appserver_requests.pop(public_id, None)
            await broadcast_task(binding[0], {"type": "server_request_resolved", "request_id": public_id})
            raise
    try:
        async with client.write_lock:
            if client.process and client.process.stdin:
                client.process.stdin.write((json.dumps({"id": request_id, "result": result}, ensure_ascii=False) + "\n").encode())
                await client.process.stdin.drain()
    finally:
        if public_id:
            pending_appserver_requests.pop(public_id, None)
        if binding and public_id:
            await broadcast_task(binding[0], {"type": "server_request_resolved", "request_id": public_id})
    if binding and not public_id:
        await broadcast_task(binding[0], {"type": "server_request_auto_resolved", "method": method})


def public_server_request(request: dict[str, Any]) -> dict[str, Any]:
    """Return the browser-safe portion of a pending app-server request."""
    return {
        "id": request["id"],
        "method": request["method"],
        "params": request["params"],
        "created_at": request["created_at"],
    }


def pending_requests_for_task(task_id: str) -> list[dict[str, Any]]:
    return [
        public_server_request(request)
        for request in pending_appserver_requests.values()
        if request["task_id"] == task_id and not request["future"].done()
    ]


def approval_result(request: dict[str, Any], payload: ApprovalResolveIn) -> dict[str, Any]:
    method, params = request["method"], request["params"]
    if method == "item/tool/requestUserInput":
        answers = {
            question.get("id", "answer"): {"answers": payload.answers.get(question.get("id", "answer"), [])}
            for question in params.get("questions", [])
        }
        return {"answers": answers}
    if method == "item/permissions/requestApproval":
        accepted = payload.decision in {"accept", "acceptForSession"}
        return {
            "permissions": params.get("permissions") or {} if accepted else {},
            "scope": "session" if payload.decision == "acceptForSession" else "turn",
        }
    return {"decision": payload.decision}


async def launch_appserver(
    task: dict,
    provider: Optional[dict],
    mode: str,
    message: str,
    message_id: str,
    attempted_provider_ids: Optional[set[str]] = None,
) -> dict:
    session_id = str(uuid.uuid4())
    stamp = now()
    attempt = int(task["retry_count"]) + 1
    provider_key = (provider or {}).get("id", "default")
    resume_id = latest_codex_session(task) if mode != "start" else ""
    codex_label = f"ssh {task['ssh_host']} codex" if task.get("ssh_host") else CODEX_BIN
    command = shlex.join([codex_label, "app-server", "thread/resume" if resume_id else "thread/start", "turn/start"])
    run_mode = requested_run_mode(task, mode, message_id)
    db.execute("INSERT INTO sessions (id,task_id,status,attempt,provider_id,command,started_at,codex_session_id) VALUES (?,?,?,?,?,?,?,?)", (session_id, task["id"], "running", attempt, provider_key if provider_key != "default" else None, command, stamp, resume_id))
    db.execute(
        "UPDATE tasks SET status='running',retry_count=?,active_session_id=?,execution_source='dashboard',execution_turn_id='',run_mode=?,updated_at=? WHERE id=?",
        (attempt, session_id, run_mode, stamp, task["id"]),
    )
    turn_task = asyncio.create_task(
        supervise_appserver_turn(task, provider, mode, message, message_id, session_id, attempted_provider_ids or set())
    )
    appserver_turn_tasks[task["id"]] = turn_task
    return task_or_404(task["id"]) | {"session_id": session_id, "mode": mode, "message_id": message_id, "thread_id": resume_id, "shared_owner": True}


async def supervise_appserver_turn(
    task: dict,
    provider: Optional[dict],
    mode: str,
    message: str,
    message_id: str,
    session_id: str,
    attempted_provider_ids: set[str],
) -> None:
    client = None
    thread_id = ""
    turn_id = ""
    waiter: Optional[asyncio.Future] = None
    try:
        client = await appserver_for(provider, task)
        key = client.key
        approval = "never" if task["yolo"] else "on-request"
        sandbox = "danger-full-access" if task["yolo"] else "workspace-write"
        sandbox_policy = {"type": "dangerFullAccess"} if task["yolo"] else {"type": "workspaceWrite", "writableRoots": [task["workspace"]]}
        binding = app_thread_bindings.get(task["id"])
        thread_id = latest_codex_session(task)
        model_provider = (provider or {}).get("model_provider") or None
        if binding and binding[0] == key:
            thread_id = binding[1]
        if thread_id:
            params = {"threadId": thread_id, "cwd": task["workspace"], "approvalPolicy": approval}
            if task.get("permission_profile"):
                params["permissions"] = task["permission_profile"]
            else:
                params["sandbox"] = sandbox
            if model_provider:
                params["modelProvider"] = model_provider
            if task.get("model") or (provider or {}).get("model"):
                params["model"] = task.get("model") or provider.get("model")
            result = await client.request("thread/resume", params)
            thread_id = (result.get("thread") or {}).get("id", thread_id)
        else:
            params = {"cwd": task["workspace"], "model": task.get("model") or (provider or {}).get("model") or None, "approvalPolicy": approval}
            if task.get("permission_profile"):
                params["permissions"] = task["permission_profile"]
            else:
                params["sandbox"] = sandbox
            if model_provider:
                params["modelProvider"] = model_provider
            result = await client.request("thread/start", params)
            thread_id = (result.get("thread") or {}).get("id")
        if not thread_id:
            raise RuntimeError("Codex app-server did not return a thread id")
        aligned_workspace = align_session_workspace(task["id"], thread_id)
        if aligned_workspace:
            task["workspace"] = str(aligned_workspace)
        canonical_task_id = canonicalize_task_thread_id(task["id"], thread_id)
        if canonical_task_id != task["id"]:
            task["id"] = canonical_task_id
            if message_id:
                await broadcast_task(task["id"], {"type": "message", "message_id": message_id, "status": "queued", "session_id": session_id})
        session_id = canonicalize_session_thread_id(task["id"], session_id, thread_id)
        persist_native_thread_model(thread_id, task.get("model") or "")
        # Bind the persisted session before goal/turn notifications can arrive.
        app_thread_bindings[task["id"]] = (key, thread_id, session_id)
        db.execute("UPDATE tasks SET codex_session_id=?,updated_at=? WHERE id=?", (thread_id, now(), task["id"]))
        native_history_cache.pop(task["id"], None)
        goal_task = task_or_404(task["id"])
        if goal_task.get("goal"):
            goal_result = await client.request("thread/goal/set", {"threadId": thread_id, "objective": goal_task["goal"], "status": goal_task.get("goal_status") if goal_task.get("goal_status") not in {None, "none"} else "active"})
            goal = goal_result.get("goal") or {}
            db.execute("UPDATE tasks SET goal_status=?, goal_tokens_used=?, updated_at=? WHERE id=?", (goal.get("status", "active"), goal.get("tokensUsed", 0), now(), task["id"]))
        codex_label = f"ssh {task['ssh_host']} codex" if task.get("ssh_host") else CODEX_BIN
        command = shlex.join([codex_label, "app-server", "thread/resume" if mode != "start" else "thread/start", thread_id, "turn/start"])
        db.execute("UPDATE sessions SET command=?, codex_session_id=? WHERE id=?", (command, thread_id, session_id))
        app_thread_bindings[task["id"]] = (key, thread_id, session_id)
        running[task["id"]] = client.process  # owner marker; browsers never spawn a second resume
        waiter = asyncio.get_running_loop().create_future()
        turn_waiters[thread_id] = waiter
        turn_params = {
            "threadId": thread_id,
            "input": appserver_turn_inputs(task, message),
            "approvalPolicy": approval,
            "clientUserMessageId": message_id or None,
            **turn_settings(task, provider, sandbox_policy),
        }
        turn_result = await client.request("turn/start", turn_params)
        turn_id = (turn_result.get("turn") or {}).get("id") or ""
        if message_id:
            db.execute("UPDATE task_messages SET status='running', session_id=?, error='' WHERE id=?", (session_id, message_id))
            await broadcast_task(task["id"], {"type": "message", "message_id": message_id, "status": "running", "session_id": session_id})
        if turn_id:
            appserver_turn_ids[task["id"]] = turn_id
            external_candidate = (external_turn_sets.get(task["id"]) or {}).get(str(turn_id))
            external_live = bool(
                external_candidate
                and external_candidate.get("path")
                and await asyncio.to_thread(rollout_is_live, external_candidate["path"])
            )
            misclassified = None if external_live else remove_external_turn(task["id"], turn_id)
            if misclassified and misclassified.get("session_id"):
                db.execute(
                    "UPDATE sessions SET status='merged',finished_at=?,exit_code=0,summary='Turn is owned by the shared dashboard app-server' WHERE id=?",
                    (now(), misclassified["session_id"]),
                )
            db.execute(
                "UPDATE tasks SET execution_source=?,execution_turn_id=?,updated_at=? WHERE id=?",
                ("mixed" if external_live else "dashboard", turn_id, now(), task["id"]),
            )
            try:
                await asyncio.wait_for(waiter, timeout=86400)
            except asyncio.TimeoutError:
                await client.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
                raise RuntimeError("Codex turn timed out")
        if task_or_404(task["id"])["status"] == "stopped":
            raise asyncio.CancelledError
        goal_task = task_or_404(task["id"])
        if goal_task.get("goal"):
            goal_result = await client.request("thread/goal/get", {"threadId": thread_id})
            goal = goal_result.get("goal") or {}
            goal_status = goal.get("status", "active")
            db.execute("UPDATE tasks SET goal_status=?, goal_tokens_used=?, updated_at=? WHERE id=?", (goal_status, goal.get("tokensUsed", 0), now(), task["id"]))
            if goal_status != "complete":
                record_provider_outcome(provider, True, "Codex turn completed; Goal continues")
                raise GoalIncompleteError(f"Codex goal is not complete: {goal_status}")
        record_provider_outcome(provider, True, "Codex app-server turn completed")
        db.execute("UPDATE sessions SET status='succeeded', finished_at=?, exit_code=0, summary=? WHERE id=?", (now(), "Codex app-server turn completed", session_id))
        external = refresh_external_primary(task["id"])
        if external:
            db.execute(
                "UPDATE tasks SET status='running',active_session_id=?,execution_source='terminal',execution_turn_id=?,run_mode='terminal',last_error='',updated_at=? WHERE id=?",
                (external.get("session_id"), external.get("turn_id", ""), now(), task["id"]),
            )
        else:
            db.execute(
                "UPDATE tasks SET status='succeeded',active_session_id=NULL,execution_source='',execution_turn_id='',last_error='',updated_at=? WHERE id=?",
                (now(), task["id"]),
            )
        if message_id:
            db.execute("UPDATE task_messages SET status='sent', finished_at=?, session_id=?, error='' WHERE id=?", (now(), session_id, message_id))
            await broadcast_task(task["id"], {"type": "message", "message_id": message_id, "status": "sent", "session_id": session_id})
        await broadcast_task(task["id"], {"type": "session", "session_id": session_id, "status": "succeeded"})
    except asyncio.CancelledError:
        # stop_task marks the task first, then cancels this supervisor. Keep
        # the session/message lifecycle consistent with the subprocess path.
        if task_or_404(task["id"])["status"] == "stopped":
            db.execute("UPDATE sessions SET status='stopped', finished_at=?, exit_code=130, summary=? WHERE id=?", (now(), "Stopped by user", session_id))
            if message_id:
                db.execute("UPDATE task_messages SET status='failed', finished_at=?, error=?, session_id=? WHERE id=?", (now(), "Stopped by user", session_id, message_id))
                await broadcast_task(task["id"], {"type": "message", "message_id": message_id, "status": "failed", "error": "Stopped by user", "session_id": session_id})
            await broadcast_task(task["id"], {"type": "session", "session_id": session_id, "status": "stopped"})
            return
        raise
    except Exception as exc:
        error = str(exc)
        stopped = task_or_404(task["id"])["status"] == "stopped"
        provider_failed = is_provider_failure(exc)
        if app_shutting_down and not stopped:
            db.execute("UPDATE sessions SET status='interrupted',finished_at=?,exit_code=130,summary='Dashboard is restarting' WHERE id=?", (now(), session_id))
            db.execute("UPDATE tasks SET status='queued',execution_source='dashboard',execution_turn_id='',last_error='Dashboard is restarting; task queued for resume',updated_at=? WHERE id=?", (now(), task["id"]))
            if message_id:
                db.execute("UPDATE task_messages SET status='queued',started_at=NULL,finished_at=NULL,session_id=NULL,error='Dashboard is restarting' WHERE id=?", (message_id,))
            return
        external = refresh_external_primary(task["id"])
        if external and not stopped:
            db.execute(
                "UPDATE sessions SET status='interrupted',finished_at=?,exit_code=1,summary=? WHERE id=?",
                (now(), f"Dashboard attach ended while terminal turn remained active: {error}", session_id),
            )
            if message_id:
                db.execute(
                    "UPDATE task_messages SET status='queued',started_at=NULL,finished_at=NULL,session_id=NULL,error=? WHERE id=?",
                    (error, message_id),
                )
                await broadcast_task(task["id"], {"type": "message", "message_id": message_id, "status": "queued", "error": error})
            persist_external_task_status(task["id"], dashboard_active=False)
            await broadcast_task(task["id"], {"type": "task_status", "task": task_or_404(task["id"]), "source": {"kind": "terminal_owner"}})
            await broadcast_overview(task["id"], {"kind": "terminal_owner"})
            return
        if provider and not stopped and provider_failed:
            record_provider_outcome(provider, False, error)
        tried_provider_ids = set(attempted_provider_ids)
        if provider and provider.get("id"):
            tried_provider_ids.add(provider["id"])
        fallback = next((p for p in provider_rows() if p.get("id") not in tried_provider_ids), None) if provider_failed else None
        if not stopped and fallback and fallback.get("id") != task.get("provider_id"):
            db.execute("INSERT INTO events (session_id,ts,stream,payload) VALUES (?,?,?,?)", (session_id, now(), "system", json.dumps({"type": "provider_failover", "from": (provider or {}).get("name"), "to": fallback.get("name"), "reason": error}, ensure_ascii=False)))
            db.execute("UPDATE sessions SET status='failed', finished_at=?, exit_code=1, summary=? WHERE id=?", (now(), error, session_id))
            if message_id:
                db.execute("UPDATE task_messages SET status='queued', session_id=NULL, error=? WHERE id=?", (error, message_id))
                await broadcast_task(task["id"], {"type": "message", "message_id": message_id, "status": "queued", "error": error})
            db.execute(
                "UPDATE tasks SET status='queued',execution_source='dashboard',provider_id=?,last_error=?,updated_at=? WHERE id=?",
                (fallback["id"], error, now(), task["id"]),
            )
            await broadcast_task(task["id"], {"type": "provider_failover", "from": (provider or {}).get("name"), "to": fallback.get("name"), "error": error})
            if running.get(task["id"]) is getattr(client, "process", None):
                running.pop(task["id"], None)
            await launch(task["id"], mode, message, message_id, tried_provider_ids)
            return
        current_task = task_or_404(task["id"])
        retries = int(current_task["retry_count"])
        goal_continues = isinstance(exc, GoalIncompleteError)
        should_retry = bool(current_task.get("goal") and current_task.get("retry_forever")) if goal_continues else bool(current_task.get("retry_forever") or retries <= int(current_task["max_retries"]))
        pending_message = db.one(
            "SELECT id FROM task_messages WHERE task_id=? AND status='queued' ORDER BY created_at, id LIMIT 1",
            (task["id"],),
        )
        if not stopped and goal_continues and pending_message:
            # A user message queued during this Goal turn is the next turn.
            # Its completion will resume the still-active Goal if necessary.
            db.execute(
                "UPDATE sessions SET status='succeeded',finished_at=?,exit_code=0,summary='Goal turn completed; queued message takes priority' WHERE id=?",
                (now(), session_id),
            )
            db.execute(
                "UPDATE tasks SET status='succeeded',active_session_id=NULL,execution_source='',execution_turn_id='',last_error='',updated_at=? WHERE id=?",
                (now(), task["id"]),
            )
            if message_id:
                db.execute("UPDATE task_messages SET status='sent',finished_at=?,session_id=?,error='' WHERE id=?", (now(), session_id, message_id))
                await broadcast_task(task["id"], {"type": "message", "message_id": message_id, "status": "sent", "session_id": session_id})
            await broadcast_task(task["id"], {"type": "session", "session_id": session_id, "status": "succeeded"})
            return
        if not stopped and should_retry:
            db.execute("UPDATE sessions SET status='retrying', finished_at=?, exit_code=1, summary=? WHERE id=?", (now(), error, session_id))
            next_run_mode = "goal_resume" if goal_continues else task_or_404(task["id"]).get("run_mode", "")
            db.execute(
                "UPDATE tasks SET status='retrying',execution_source='dashboard',run_mode=?,last_error=?,updated_at=? WHERE id=?",
                (next_run_mode, error, now(), task["id"]),
            )
            if goal_continues:
                # The user message already completed this turn. Goal resume
                # must continue the same Codex thread without replaying it.
                if message_id:
                    db.execute("UPDATE task_messages SET status='sent', finished_at=?, session_id=?, error='' WHERE id=?", (now(), session_id, message_id))
                    await broadcast_task(task["id"], {"type": "message", "message_id": message_id, "status": "sent", "session_id": session_id})
            elif message_id:
                db.execute("UPDATE task_messages SET status='queued', error=?, session_id=NULL WHERE id=?", (error, message_id))
                await broadcast_task(task["id"], {"type": "message", "message_id": message_id, "status": "queued", "error": error})
            await broadcast_task(task["id"], {"type": "session", "session_id": session_id, "status": "retrying", "error": error})
            await asyncio.sleep(min(30, 2 ** min(retries, 4)))
            if goal_continues and not task_or_404(task["id"]).get("goal"):
                db.execute("UPDATE sessions SET status='succeeded',exit_code=0,summary='Goal cleared after completed turn' WHERE id=?", (session_id,))
                db.execute("UPDATE tasks SET status='succeeded',active_session_id=NULL,execution_source='',execution_turn_id='',last_error='',updated_at=? WHERE id=?", (now(), task["id"]))
                await broadcast_task(task["id"], {"type": "session", "session_id": session_id, "status": "succeeded"})
                await broadcast_overview(task["id"], {"kind": "goal_cleared"})
                return
            db.execute("UPDATE tasks SET status='queued',execution_source='dashboard',updated_at=? WHERE id=?", (now(), task["id"]))
            if goal_continues:
                await launch(task["id"], "resume", "", "", set())
            else:
                await launch(task["id"], "message" if message_id else "resume", message, message_id, set())
            return
        db.execute("UPDATE sessions SET status=?, finished_at=?, exit_code=1, summary=? WHERE id=?", ("stopped" if stopped else "failed", now(), "Stopped by user" if stopped else error, session_id))
        db.execute(
            "UPDATE tasks SET status=?,execution_source='',execution_turn_id='',last_error=?,updated_at=? WHERE id=?",
            ("stopped" if stopped else "failed", "Stopped by user" if stopped else error, now(), task["id"]),
        )
        if message_id:
            db.execute("UPDATE task_messages SET status='failed', finished_at=?, error=?, session_id=? WHERE id=?", (now(), "Stopped by user" if stopped else error, session_id, message_id))
            await broadcast_task(task["id"], {"type": "message", "message_id": message_id, "status": "failed", "error": "Stopped by user" if stopped else error, "session_id": session_id})
        await broadcast_task(task["id"], {"type": "session", "session_id": session_id, "status": "stopped" if stopped else "failed", "error": error})
    finally:
        if thread_id and turn_waiters.get(thread_id) is waiter:
            turn_waiters.pop(thread_id, None)
        if turn_id and appserver_turn_ids.get(task["id"]) == turn_id:
            appserver_turn_ids.pop(task["id"], None)
        if appserver_turn_tasks.get(task["id"]) is asyncio.current_task():
            appserver_turn_tasks.pop(task["id"], None)
        binding = app_thread_bindings.get(task["id"])
        if binding and binding[2] == session_id:
            app_thread_bindings.pop(task["id"], None)
        if running.get(task["id"]) is getattr(client, "process", None):
            running.pop(task["id"], None)
        # Cover early returns from retry/failover/stop paths as well as the
        # normal success path.  The worker will wait if another owner is still
        # active and dispatch queued rows as soon as this turn is idle.
        schedule_task_drain(task["id"])
    await drain_task_messages(task["id"])


def active_auth_session(token: str) -> Optional[dict[str, Any]]:
    session = auth_sessions.get(token)
    if not session:
        return None
    if float(session.get("expires_at", 0)) <= time.time():
        auth_sessions.pop(token, None)
        return None
    return session


def local_server_addresses() -> set[str]:
    """Return addresses owned by this host without trusting request headers."""
    addresses = {"127.0.0.1", "::1"}
    for name in {socket.gethostname(), socket.getfqdn()}:
        try:
            for item in socket.getaddrinfo(name, None):
                addresses.add(str(item[4][0]).split("%", 1)[0])
        except socket.gaierror:
            continue
    if platform.system() == "Linux":
        for _index, name in socket.if_nameindex():
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                    packed = fcntl.ioctl(probe.fileno(), 0x8915, struct.pack("256s", name.encode()[:15]))
                addresses.add(socket.inet_ntoa(packed[20:24]))
            except OSError:
                continue
        try:
            for line in Path("/proc/net/if_inet6").read_text(encoding="ascii").splitlines():
                addresses.add(str(ipaddress.IPv6Address(int(line.split()[0], 16))))
        except (OSError, ValueError, IndexError):
            pass
    return addresses


LOCAL_SERVER_ADDRESSES = local_server_addresses()


def direct_local_client(client_host: str, headers: Any) -> bool:
    """Allow passwordless access only for direct connections from this host."""
    if any(headers.get(name) for name in ("forwarded", "x-forwarded-for", "x-real-ip")):
        return False
    host = str(client_host or "").split("%", 1)[0]
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    return host in LOCAL_SERVER_ADDRESSES


def local_auth_session(client_host: str, headers: Any) -> Optional[dict[str, Any]]:
    if direct_local_client(client_host, headers):
        return {"username": getpass.getuser(), "expires_at": 0, "local": True}
    return None


def request_auth_session(request: Request) -> Optional[dict[str, Any]]:
    if AUTH_MODE == "none":
        return {"username": getpass.getuser(), "expires_at": 0}
    if session := local_auth_session(request.client.host if request.client else "", request.headers):
        return session
    return active_auth_session(request.cookies.get(AUTH_COOKIE, ""))


def websocket_auth_session(websocket: WebSocket) -> Optional[dict[str, Any]]:
    if AUTH_MODE == "none":
        return {"username": getpass.getuser(), "expires_at": 0}
    if session := local_auth_session(websocket.client.host if websocket.client else "", websocket.headers):
        return session
    return active_auth_session(websocket.cookies.get(AUTH_COOKIE, ""))


def profile_avatar_path(username: str) -> Path:
    profile_id = hashlib.sha256(username.encode("utf-8")).hexdigest()
    root = DATA_DIR / "profiles" / profile_id
    gif = root.with_suffix(".gif")
    return gif if gif.is_file() else root.with_suffix(".webp")


def profile_snapshot(username: str) -> dict[str, Any]:
    path = profile_avatar_path(username)
    version = str(path.stat().st_mtime_ns) if path.is_file() else ""
    return {
        "username": username,
        "avatar_url": f"/api/profile/avatar?v={version}" if version else "",
        "avatar_version": version,
    }


def avatar_file(data_url: str) -> tuple[bytes, str]:
    match = re.fullmatch(r"data:image/[A-Za-z0-9.+-]+;base64,([A-Za-z0-9+/=\r\n]+)", data_url.strip())
    if not match:
        raise HTTPException(400, "Avatar must be a base64 image")
    try:
        raw = base64.b64decode(match.group(1), validate=True)
        with Image.open(io.BytesIO(raw)) as source:
            if getattr(source, "is_animated", False) and getattr(source, "n_frames", 1) > 1:
                frames = []
                durations = []
                default_duration = int(source.info.get("duration", 100) or 100)
                for frame in ImageSequence.Iterator(source):
                    rendered = frame.convert("RGBA")
                    rendered.thumbnail((512, 512), Image.Resampling.LANCZOS)
                    frames.append(rendered.copy())
                    durations.append(max(20, int(frame.info.get("duration", default_duration) or default_duration)))
                output = io.BytesIO()
                frames[0].save(
                    output,
                    "GIF",
                    save_all=True,
                    append_images=frames[1:],
                    duration=durations,
                    loop=int(source.info.get("loop", 0) or 0),
                    disposal=2,
                )
                return output.getvalue(), ".gif"
            image = ImageOps.exif_transpose(source)
            image.thumbnail((512, 512), Image.Resampling.LANCZOS)
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGBA" if "transparency" in image.info else "RGB")
            output = io.BytesIO()
            image.save(output, "WEBP", quality=88, method=4)
            return output.getvalue(), ".webp"
    except (binascii.Error, UnidentifiedImageError, OSError, ValueError) as exc:
        raise HTTPException(415, "Avatar is not a supported image") from exc


async def auth(request: Request):
    session = request_auth_session(request)
    if not session:
        raise HTTPException(401, "SSH login required")
    return session


def task_or_404(task_id: str) -> dict:
    canonical_id = task_id_aliases.get(task_id, task_id)
    task = db.one("SELECT * FROM tasks WHERE id=?", (canonical_id,))
    if not task:
        raise HTTPException(404, "Task not found")
    task["yolo"] = bool(task["yolo"])
    task["retry_forever"] = bool(task.get("retry_forever", 0))
    task["native"] = bool(task.get("native", 0))
    task["archived"] = bool(task.get("archived", 0))
    task["trashed"] = bool(task.get("trashed", 0))
    task["goal_status"] = task.get("goal_status") or ("active" if task.get("goal") else "none")
    if external := external_turns.get(task_id):
        task["external_running"] = True
        task["external_started_at"] = external.get("started_at")
        task["external_phase"] = external.get("phase")
        task["external_turn_id"] = external.get("turn_id")
        task["external_turn_count"] = len(external_turn_sets.get(task_id) or {})
    return task


def provider_rows() -> list[dict]:
    return db.all("SELECT * FROM providers WHERE enabled=1 ORDER BY priority ASC, created_at ASC")


def native_default_provider_name() -> str:
    path = CODEX_HOME / "config.toml"
    if not path.is_file():
        return ""
    try:
        return str(tomllib.loads(path.read_text(encoding="utf-8")).get("model_provider") or "")
    except (OSError, ValueError):
        return ""


def provider_public_rows() -> list[dict]:
    rows = db.all("SELECT * FROM providers ORDER BY priority,name")
    default_provider = native_default_provider_name()
    active = {
        row["provider_id"]: row["count"]
        for row in db.all(
            "SELECT s.provider_id,COUNT(*) count FROM tasks t "
            "JOIN sessions s ON s.id=t.active_session_id "
            "WHERE t.status IN ('running','retrying') AND s.provider_id IS NOT NULL "
            "GROUP BY s.provider_id"
        )
    }
    for row in rows:
        direct_key = bool(row.get("api_key"))
        legacy_key = bool(row.get("api_key_env") and os.getenv(row["api_key_env"]))
        row["enabled"] = bool(row["enabled"])
        row["native"] = bool(row.get("native", 0))
        row["has_key"] = bool(direct_key or legacy_key or row["native"])
        row["has_saved_key"] = direct_key
        row["credential_source"] = "已保存 API Key" if direct_key else ("Codex 配置" if row["native"] else ("旧环境变量" if legacy_key else "未设置 API Key"))
        row["is_default"] = bool(row.get("model_provider") and row["model_provider"] == default_provider)
        row["in_use_count"] = int(active.get(row["id"], 0))
        row["health_status"] = row.get("health_status") or "unchecked"
        row["success_count"] = int(row.get("success_count") or 0)
        row["failure_count"] = int(row.get("failure_count") or 0)
        row.pop("api_key", None)
        row.pop("api_key_env", None)
    return rows


def provider_api_key(provider: Optional[dict]) -> str:
    if not provider:
        return ""
    if provider.get("api_key"):
        return str(provider["api_key"])
    legacy_env = str(provider.get("api_key_env") or "")
    return os.getenv(legacy_env, "") if legacy_env else ""


def normalize_provider_url(value: str) -> str:
    """Normalize a provider endpoint for duplicate detection and storage."""
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        return ""
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return raw
    hostname = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        port = None
    default_port = (parsed.scheme.lower() == "http" and port == 80) or (parsed.scheme.lower() == "https" and port == 443)
    netloc = hostname
    if parsed.username or parsed.password:
        # Credentials are not accepted as provider settings, but preserve a
        # deterministic comparison key if an older record contains them.
        netloc = parsed.netloc.lower()
    elif port and not default_port:
        netloc = f"{hostname}:{port}"
    path = parsed.path.rstrip("/")
    return urllib.parse.urlunsplit((parsed.scheme.lower(), netloc, path, parsed.query, ""))


def provider_name_from_url(base_url: str) -> str:
    parsed = urllib.parse.urlparse(base_url)
    return (parsed.hostname or "").strip() or "Custom Provider"


def provider_model_provider_from_url(base_url: str) -> str:
    parsed = urllib.parse.urlparse(base_url)
    source = f"{parsed.hostname or 'custom'}{parsed.path or ''}"
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", source).strip("-._").lower()
    return (slug or "custom-provider")[:120]


def provider_duplicate(base_url: str, exclude_id: str = "") -> Optional[dict]:
    normalized = normalize_provider_url(base_url)
    for row in db.all("SELECT id,name,base_url FROM providers"):
        if row.get("id") == exclude_id:
            continue
        if normalize_provider_url(row.get("base_url") or "") == normalized:
            return row
    return None


def provider_models(raw: bytes) -> list[dict[str, str]]:
    """Extract OpenAI-compatible model ids from common response shapes."""
    try:
        decoded = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
    except (TypeError, ValueError, UnicodeDecodeError):
        return []
    if isinstance(decoded, dict):
        values = decoded.get("data") or decoded.get("models") or decoded.get("results") or []
    elif isinstance(decoded, list):
        values = decoded
    else:
        values = []
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in values:
        if isinstance(value, str):
            model_id, label = value.strip(), value.strip()
        elif isinstance(value, dict):
            model_id = str(value.get("id") or value.get("model") or value.get("slug") or value.get("name") or "").strip()
            label = str(value.get("display_name") or value.get("displayName") or value.get("name") or model_id).strip()
        else:
            continue
        if model_id and model_id not in seen:
            seen.add(model_id)
            result.append({"id": model_id, "label": label or model_id})
    return result


def merge_model_rows(*groups: list[Any]) -> list[dict[str, Any]]:
    """Merge model/list and provider /models rows without losing metadata."""
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in groups:
        for value in group or []:
            if isinstance(value, str):
                model_id = value.strip()
                item: dict[str, Any] = {"id": model_id, "label": model_id}
            elif isinstance(value, dict):
                model_id = str(value.get("id") or value.get("model") or value.get("slug") or value.get("name") or "").strip()
                item = value
            else:
                continue
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            merged.append(item)
    return merged


def provider_probe(provider: dict) -> dict:
    started = time.monotonic()
    base_url = str(provider.get("base_url") or "").strip().rstrip("/")
    if not base_url:
        return {"status": "configured", "detail": "Uses the Codex default endpoint", "latency_ms": 0, "models": []}
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return {"status": "invalid", "detail": "Base URL must use http or https", "latency_ms": 0, "models": []}
    url = f"{base_url}/models"
    headers = {"Accept": "application/json", "User-Agent": "codex-dashboard-provider-check"}
    key = provider_api_key(provider)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=6) as response:
            status_code = int(response.status)
            reader = getattr(response, "read", None)
            raw = reader() if callable(reader) else b""
        status = "healthy" if 200 <= status_code < 300 else "reachable"
        detail = f"HTTP {status_code} from {url}"
        models = provider_models(raw)
    except urllib.error.HTTPError as exc:
        status_code = int(exc.code)
        if status_code in {401, 403}:
            if key:
                status, detail = "auth_error", f"HTTP {status_code}; check the saved API key"
            elif provider.get("native"):
                status, detail = "reachable", f"HTTP {status_code}; credentials are managed by Codex"
            else:
                status, detail = "needs_key", f"HTTP {status_code}; save an API key for this provider"
        elif status_code >= 500:
            status, detail = "degraded", f"HTTP {status_code} from {url}"
        else:
            status, detail = "reachable", f"HTTP {status_code} from {url}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        status, detail = "unavailable", str(getattr(exc, "reason", exc))
    latency = max(0, round((time.monotonic() - started) * 1000))
    return {"status": status, "detail": detail[:1000], "latency_ms": latency, "models": locals().get("models", [])}


def store_provider_probe(provider_id: str, result: dict) -> None:
    db.execute(
        "UPDATE providers SET health_status=?,health_detail=?,health_checked_at=?,health_latency_ms=? WHERE id=?",
        (result["status"], result["detail"], now(), result["latency_ms"], provider_id),
    )


def record_provider_outcome(provider: Optional[dict], succeeded: bool, detail: str = "") -> None:
    if not provider or not provider.get("id"):
        return
    stamp = now()
    if succeeded:
        db.execute(
            "UPDATE providers SET health_status='healthy',health_detail=?,health_checked_at=?,success_count=success_count+1,last_success_at=? WHERE id=?",
            ((detail or "Codex turn completed")[:1000], stamp, stamp, provider["id"]),
        )
    else:
        db.execute(
            "UPDATE providers SET health_status='error',health_detail=?,health_checked_at=?,failure_count=failure_count+1,last_failure_at=? WHERE id=?",
            ((detail or "Codex turn failed")[:1000], stamp, stamp, provider["id"]),
        )


def installed_skill_roots() -> tuple[tuple[str, Path, bool], ...]:
    return (
        ("Codex", CODEX_HOME / "skills", True),
        ("Agent", Path.home() / ".agents/skills", True),
    )


def parse_skill_document(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("---\n"):
        return {}, text
    marker = text.find("\n---\n", 4)
    if marker < 0:
        return {}, text
    try:
        metadata = yaml.safe_load(text[4:marker]) or {}
    except yaml.YAMLError:
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    return metadata, text[marker + 5:]


def installed_skill_rows() -> list[dict]:
    rows = []
    for source, root, root_editable in installed_skill_roots():
        try:
            resolved_root = root.expanduser().resolve()
        except OSError:
            continue
        if not resolved_root.is_dir():
            continue
        for path in sorted(resolved_root.rglob("SKILL.md")):
            try:
                resolved = path.resolve()
                if resolved_root not in resolved.parents:
                    continue
                relative = resolved.relative_to(resolved_root)
                metadata, body = parse_skill_document(resolved)
                stat = resolved.stat()
            except (OSError, ValueError):
                continue
            system = source == "Codex" and ".system" in relative.parts
            name = str(metadata.get("name") or relative.parent.name)
            rows.append({
                "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"installed-skill:{resolved}")),
                "name": name,
                "description": str(metadata.get("description") or ""),
                "content": body.strip(),
                "enabled": True,
                "installed": True,
                "source": "Codex system" if system else source,
                "editable": bool(root_editable),
                "deletable": bool(root_editable and not system),
                "path": str(resolved),
                "root": str(resolved_root),
                "updated_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            })
    return sorted(rows, key=lambda row: (row["source"], row["name"].lower()))


def installed_skill_or_404(skill_id: str) -> dict:
    row = next((item for item in installed_skill_rows() if item["id"] == skill_id), None)
    if not row:
        raise HTTPException(404, "Installed Skill not found")
    return row


def skill_slug(value: str) -> str:
    slug = value.strip().lower().replace("_", "-")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise HTTPException(400, "Skill name must use lowercase letters, numbers, and hyphens")
    return slug


def write_skill_document(path: Path, name: str, description: str, content: str, preserve: bool = False) -> None:
    metadata: dict[str, Any] = {}
    if preserve and path.is_file():
        metadata, _body = parse_skill_document(path)
    metadata["name"] = name
    metadata["description"] = description.strip()
    header = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False, default_flow_style=False).strip()
    body = content.strip() or f"# {name}\n"
    document = f"---\n{header}\n---\n\n{body.rstrip()}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(document, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def skills_prompt() -> str:
    rows = db.all("SELECT name, content FROM skills WHERE enabled=1 ORDER BY name")
    return "\n\n".join(f"## Skill: {r['name']}\n{r['content']}" for r in rows)


def prompt_for(task: dict, override: str = "") -> str:
    prompt = override or task["prompt"]
    if task.get("context"):
        prompt = f"{prompt}\n\nProject context:\n{task['context']}"
    skills = skills_prompt()
    if skills:
        prompt = f"{prompt}\n\nAvailable skills:\n{skills}"
    return prompt


CODEX_INPUT_MARKER = re.compile(r"\[\[codex-input:(localImage|localAudio|mention):([^\]]+)\]\]")
LEGACY_FILE_MARKER = re.compile(r"\[\[codex-file:([^\]]+)\]\]")
IMAGE_FILE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def appserver_attachment_path(task: dict, encoded_path: str) -> tuple[str, str] | None:
    """Resolve a browser attachment without allowing it to escape the task workspace."""
    relative = urllib.parse.unquote(encoded_path).strip().replace("\\", "/")
    candidate_path = PurePosixPath(relative)
    if not relative or "\0" in relative or candidate_path.is_absolute() or ".." in candidate_path.parts:
        return None
    normalized = candidate_path.as_posix()
    if task.get("ssh_host"):
        return normalized, posixpath.join(str(task["workspace"]), normalized)
    try:
        _root, candidate = workspace_path(task, normalized)
    except HTTPException:
        return None
    if not candidate.is_file() or workspace_hidden(candidate):
        return None
    return normalized, str(candidate)


def appserver_turn_inputs(task: dict, message: str) -> list[dict[str, Any]]:
    """Translate durable chat markers into the structured app-server input protocol."""
    attachments: list[tuple[str, str]] = []

    def collect(match: re.Match, legacy: bool = False) -> str:
        encoded_path = match.group(1) if legacy else match.group(2)
        resolved = appserver_attachment_path(task, encoded_path)
        if resolved:
            relative, absolute = resolved
            kind = "localImage" if legacy and PurePosixPath(relative).suffix.lower() in IMAGE_FILE_SUFFIXES else ("mention" if legacy else match.group(1))
            attachments.append((kind, absolute))
        return ""

    clean_message = CODEX_INPUT_MARKER.sub(collect, message)
    clean_message = LEGACY_FILE_MARKER.sub(lambda match: collect(match, True), clean_message)
    inputs: list[dict[str, Any]] = []
    prompt = prompt_for(task, clean_message.strip())
    if prompt:
        inputs.append({"type": "text", "text": prompt})
    seen: set[tuple[str, str]] = set()
    for kind, path in attachments:
        if (kind, path) in seen:
            continue
        seen.add((kind, path))
        if kind == "mention":
            inputs.append({"type": "mention", "name": PurePosixPath(path).name, "path": path})
        else:
            item: dict[str, Any] = {"type": kind, "path": path}
            if kind == "localImage":
                item["detail"] = "original"
            inputs.append(item)
    return inputs


def requested_run_mode(task: dict, mode: str, message_id: str = "") -> str:
    """Describe why the current turn exists for synchronized browser controls."""
    if message_id or mode == "message":
        return "message"
    if task.get("goal") and mode == "resume":
        return "goal_resume"
    return "operation"


def turn_settings(task: dict, provider: Optional[dict], sandbox_policy: dict) -> dict[str, Any]:
    """Build sticky turn settings shared by regular and resumed browser turns."""
    settings: dict[str, Any] = {}
    model = task.get("model") or (provider or {}).get("model") or ""
    if model:
        settings["model"] = model
    if task.get("reasoning_effort"):
        settings["effort"] = task["reasoning_effort"]
    if task.get("service_tier"):
        settings["serviceTier"] = task["service_tier"]
    if task.get("personality"):
        settings["personality"] = task["personality"]
    if task.get("collaboration_mode") == "plan":
        settings["collaborationMode"] = {
            "mode": "plan",
            "settings": {
                "model": model,
                "reasoning_effort": task.get("reasoning_effort") or None,
                "developer_instructions": None,
            },
        }
    if task.get("permission_profile"):
        settings["permissions"] = task["permission_profile"]
    else:
        settings["sandboxPolicy"] = sandbox_policy
    return settings


def command_for(task: dict, provider: Optional[dict], resume_id: str = "", prompt_override: str = "") -> list[str]:
    # Codex has no literal --yolo flag; these are its current equivalent.
    cmd = [CODEX_BIN, "exec"]
    if resume_id:
        cmd.append("resume")
    cmd += ["--json", "--color", "never", "--skip-git-repo-check"]
    if task["yolo"]:
        cmd += ["--ask-for-approval", "never", "--dangerously-bypass-approvals-and-sandbox"]
    if task.get("model") or (provider and provider.get("model")):
        cmd += ["--model", task.get("model") or provider.get("model")]
    if provider and provider.get("profile"):
        cmd += ["--profile", provider["profile"]]
    cmd += ["-C", task["workspace"]]
    if resume_id:
        cmd.append(resume_id)
    prompt = prompt_for(task, prompt_override)
    # The legacy CLI fallback has no app-server goal RPC, so preserve the
    # native slash directive only on that transport.
    if task.get("goal"):
        prompt = f"/goal {task['goal']}\n\n{prompt}"
    cmd.append(prompt)
    return cmd


def safe_json(value: str) -> Any:
    try:
        return json.loads(value)
    except Exception:
        return value


def task_summary(task_id: str) -> Optional[dict]:
    row = db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not row:
        return None
    row["yolo"] = bool(row["yolo"])
    row["retry_forever"] = bool(row.get("retry_forever", 0))
    row["native"] = bool(row.get("native", 0))
    row["archived"] = bool(row.get("archived", 0))
    row["trashed"] = bool(row.get("trashed", 0))
    row["goal_status"] = row.get("goal_status") or ("active" if row.get("goal") else "none")
    if external := external_turns.get(task_id):
        row["external_running"] = True
        row["external_started_at"] = external.get("started_at")
        row["external_phase"] = external.get("phase")
        row["external_turn_id"] = external.get("turn_id")
        row["external_turn_count"] = len(external_turn_sets.get(task_id) or {})
    return row


def all_task_summaries() -> list[dict]:
    rows = db.all("SELECT id FROM tasks WHERE trashed=0 ORDER BY updated_at DESC")
    return [summary for row in rows if (summary := task_summary(row["id"]))]


async def broadcast_overview(task_id: str, source: Optional[dict] = None) -> None:
    """Publish one durable task snapshot to every browser's sidebar channel."""
    task = task_summary(task_id)
    if not task:
        return
    payload = {"type": "task_status", "task": task}
    if source:
        payload["source"] = source
    stale = []
    for websocket in list(overview_clients):
        try:
            await websocket.send_json(payload)
        except Exception:
            stale.append(websocket)
    for websocket in stale:
        overview_clients.discard(websocket)
        overview_client_users.pop(websocket, None)


async def broadcast_overview_removed(task_id: str) -> None:
    stale = []
    payload = {"type": "task_removed", "task_id": task_id}
    for websocket in list(overview_clients):
        try:
            await websocket.send_json(payload)
        except Exception:
            stale.append(websocket)
    for websocket in stale:
        overview_clients.discard(websocket)
        overview_client_users.pop(websocket, None)


async def broadcast_profile(username: str, profile: Optional[dict] = None) -> None:
    payload = {"type": "profile_updated", "profile": profile or profile_snapshot(username)}
    stale = []
    for websocket, connected_user in list(overview_client_users.items()):
        if connected_user != username:
            continue
        try:
            await websocket.send_json(payload)
        except Exception:
            stale.append(websocket)
    for websocket in stale:
        overview_clients.discard(websocket)
        overview_client_users.pop(websocket, None)


async def broadcast_task(task_id: str, payload: dict) -> None:
    """Fan out one canonical task event to every browser attached to the task."""
    overview_source = payload
    if payload.get("type") == "task_status":
        task = payload.get("task") or {}
        mutable_fields = {
            "name", "goal", "workspace", "status", "yolo", "retry_count", "retry_forever",
            "provider_id", "model", "reasoning_effort", "service_tier", "last_error",
            "active_session_id", "goal_status", "goal_tokens_used", "updated_at", "archived",
            "trashed", "execution_source", "execution_turn_id", "run_mode", "external_running",
            "external_started_at", "external_phase", "external_turn_id", "external_turn_count",
        }
        payload = {
            "type": "task_patch",
            "task_id": task_id,
            "patch": {key: value for key, value in task.items() if key in mutable_fields},
            "source": payload.get("source") or {},
        }
    clients = list(task_clients.get(task_id, set()))
    stale = []
    for websocket in clients:
        try:
            await websocket.send_json(payload)
        except Exception:
            stale.append(websocket)
    for websocket in stale:
        task_clients.get(task_id, set()).discard(websocket)
    if overview_source.get("type") in {"message", "session", "provider_failover", "task_status"}:
        await broadcast_overview(task_id, overview_source)


async def close_terminal_session(terminal_id: str) -> None:
    session = terminal_sessions.pop(terminal_id, None)
    if not session:
        return
    session["closed"] = True
    fd, pid = session.get("fd"), session.get("pid")
    loop = session.get("loop")
    if loop and fd is not None:
        try:
            loop.remove_reader(fd)
        except Exception:
            pass
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
    if pid:
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        for _ in range(20):
            try:
                finished, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                break
            if finished:
                break
            await asyncio.sleep(0.02)
        else:
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def set_terminal_size(fd: int, cols: int, rows: int) -> None:
    cols = max(20, min(int(cols), 400))
    rows = max(4, min(int(rows), 160))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


@app.websocket("/ws/terminal/{task_id}")
async def terminal_socket(websocket: WebSocket, task_id: str):
    if not websocket_auth_session(websocket):
        await websocket.close(code=4401)
        return
    task = db.one("SELECT id,workspace,ssh_host FROM tasks WHERE id=?", (task_id,))
    if not task:
        await websocket.close(code=4404)
        return
    ssh_host = task.get("ssh_host") or ""
    if ssh_host:
        connection = await connect_ssh_host(ssh_host)
        if not connection.get("connected"):
            await websocket.close(code=4428, reason="SSH login required")
            return
    await websocket.accept()
    terminal_id = str(uuid.uuid4())
    cwd = task["workspace"] if ssh_host else (task["workspace"] if Path(task["workspace"]).is_dir() else str(DEFAULT_WORKSPACE))
    shell = f"ssh:{ssh_host}" if ssh_host else os.environ.get("SHELL", "/bin/bash")
    if not ssh_host and not Path(shell).is_file():
        shell = "/bin/bash"
    pid, fd = pty.fork()
    if pid == 0:
        try:
            env = os.environ.copy()
            env.update({"TERM": "xterm-256color", "COLORTERM": "truecolor"})
            if ssh_host:
                remote_command = f"cd -- {shlex.quote(cwd)} && exec \"${{SHELL:-/bin/bash}}\" -il"
                command = [*ssh_options(ssh_host, batch=True, tty=True), ssh_destination(ssh_host), remote_command]
                os.execvpe(command[0], command, env)
            else:
                os.chdir(cwd)
                os.execvpe(shell, [shell, "-i"], env)
        except Exception as exc:
            os.write(2, f"terminal start failed: {exc}\n".encode())
        os._exit(127)

    loop = asyncio.get_running_loop()
    output_queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue()
    session = {"pid": pid, "fd": fd, "loop": loop, "task_id": task_id, "closed": False}
    terminal_sessions[terminal_id] = session
    os.set_blocking(fd, False)
    set_terminal_size(fd, 120, 32)

    def on_pty_readable() -> None:
        if session["closed"]:
            return
        try:
            data = os.read(fd, 65536)
        except BlockingIOError:
            return
        except OSError as exc:
            if exc.errno not in {errno.EIO, errno.EBADF}:
                return
            data = b""
        if data:
            output_queue.put_nowait(data)
        else:
            try:
                loop.remove_reader(fd)
            except Exception:
                pass
            output_queue.put_nowait(None)

    loop.add_reader(fd, on_pty_readable)
    await websocket.send_json({"type": "ready", "terminal_id": terminal_id, "cwd": cwd, "shell": shell})

    async def pump_output() -> None:
        while True:
            data = await output_queue.get()
            if data is None:
                try:
                    code = (await asyncio.to_thread(os.waitpid, pid, 0))[1]
                    code = os.waitstatus_to_exitcode(code)
                except (ChildProcessError, OSError):
                    code = None
                await websocket.send_json({"type": "exit", "code": code})
                return
            await websocket.send_json({"type": "output", "data": data.decode(errors="replace")})

    async def receive_input() -> None:
        while True:
            incoming = await websocket.receive_json()
            kind = incoming.get("type")
            if kind == "input":
                value = incoming.get("data", incoming.get("input", ""))
                if isinstance(value, str) and value:
                    os.write(fd, value.encode("utf-8", errors="replace"))
            elif kind == "resize":
                try:
                    set_terminal_size(fd, incoming.get("cols", 120), incoming.get("rows", 32))
                    os.kill(pid, signal.SIGWINCH)
                except (OSError, ValueError, TypeError):
                    continue
            elif kind == "ping":
                await websocket.send_json({"type": "pong"})
            elif kind == "close":
                return

    pump_task = asyncio.create_task(pump_output())
    receive_task = asyncio.create_task(receive_input())
    try:
        done, pending = await asyncio.wait({pump_task, receive_task}, return_when=asyncio.FIRST_COMPLETED)
        for task_waiter in pending:
            task_waiter.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if pump_task in done:
            try:
                await websocket.close()
            except Exception:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        for task_waiter in (pump_task, receive_task):
            if not task_waiter.done():
                task_waiter.cancel()
        await asyncio.gather(pump_task, receive_task, return_exceptions=True)
        await close_terminal_session(terminal_id)


def latest_codex_session(task: dict) -> str:
    if task.get("codex_session_id"):
        return task["codex_session_id"]
    row = db.one("SELECT codex_session_id FROM sessions WHERE task_id=? AND codex_session_id!='' ORDER BY started_at DESC LIMIT 1", (task["id"],))
    return (row or {}).get("codex_session_id", "") or ""


async def drain_task_messages(task_id: str) -> None:
    """Continue queued browser work when the task has become idle.

    The task worker owns the ordering.  This function intentionally leaves the
    row queued while a turn is active, so a message sent during a turn can
    never compete with that turn or be lost at the completion boundary. An
    adopted terminal Goal turn resumes only after queued user messages.
    """
    lock = task_message_locks.setdefault(task_id, asyncio.Lock())
    async with lock:
        current = task_or_404(task_id)
        if task_turn_active(task_id, current):
            return
        row = db.one("SELECT * FROM task_messages WHERE task_id=? AND status='queued' ORDER BY created_at, id LIMIT 1", (task_id,))
        if not row:
            goal_resume = bool(
                current.get("goal")
                and current.get("retry_forever")
                and current.get("run_mode") == "goal_resume"
                and current.get("goal_status") not in {"paused", "complete", "none"}
            )
            if not goal_resume:
                return
            # A browser can adopt a terminal-owned Goal turn. Once that turn
            # ends, continue the same Goal through the shared app-server.
            db.execute(
                "UPDATE tasks SET status='queued',execution_source='dashboard',execution_turn_id='',updated_at=? WHERE id=?",
                (now(), task_id),
            )
            try:
                await launch(task_id, "resume")
            except Exception as exc:
                if not is_task_busy_error(exc):
                    db.execute("UPDATE tasks SET status='failed',last_error=?,updated_at=? WHERE id=?", (str(exc), now(), task_id))
            return
        # launch() only reserves an async supervisor; it does not mean Codex
        # has accepted the turn yet. Keep the row in the compact queue until
        # the subprocess/app-server reports that it actually started.
        db.execute("UPDATE task_messages SET status='dispatching', started_at=? WHERE id=?", (now(), row["id"]))
        await broadcast_task(task_id, {"type": "message", "message_id": row["id"], "status": "dispatching", "body": row["body"]})
        try:
            result = await launch(task_id, "message", row["body"], row["id"])
            db.execute("UPDATE task_messages SET session_id=? WHERE id=?", (result["session_id"], row["id"]))
        except Exception as exc:
            # A turn can start between the idle check and launch().  Keep the
            # durable message queued for the worker's next pass instead of
            # reporting a false failure or requiring the user to resend it.
            if is_task_busy_error(exc):
                error = str(exc)
                db.execute(
                    "UPDATE task_messages SET status='queued',started_at=NULL,finished_at=NULL,error=? WHERE id=?",
                    (error, row["id"]),
                )
                await broadcast_task(task_id, {"type": "message", "message_id": row["id"], "status": "queued", "body": row["body"], "error": error})
                return
            db.execute("UPDATE task_messages SET status='failed', finished_at=?, error=? WHERE id=?", (now(), str(exc), row["id"]))
            await broadcast_task(task_id, {"type": "message", "message_id": row["id"], "status": "failed", "error": str(exc)})


def task_turn_active(task_id: str, task: Optional[dict] = None) -> bool:
    """Return whether another owner currently controls the Codex turn."""
    current = task or task_or_404(task_id)
    return bool(
        task_id in running
        or task_id in appserver_turn_tasks
        or task_id in external_turns
        or current.get("status") in {"running", "retrying"}
    )


def is_task_busy_error(exc: Exception) -> bool:
    """Identify a launch race that should be retried, not failed."""
    if isinstance(exc, HTTPException) and exc.status_code == 409:
        return True
    text = str(exc).lower()
    return "already running" in text or "turn is already active" in text or "current turn" in text and "active" in text


def schedule_task_drain(task_id: str) -> None:
    worker = task_workers.get(task_id)
    if worker and not worker.done():
        return

    async def run() -> None:
        try:
            # Keep one lightweight worker alive while queued rows remain.  It
            # sleeps during the active turn and wakes naturally after the
            # completion path clears the owner, so late queue fallbacks are
            # dispatched without another browser action.
            while not app_shutting_down:
                await drain_task_messages(task_id)
                try:
                    current = task_or_404(task_id)
                except HTTPException:
                    return
                pending = db.one("SELECT id FROM task_messages WHERE task_id=? AND status='queued' LIMIT 1", (task_id,))
                if not pending:
                    return
                # Keep a small backoff even after a launch race.  This avoids
                # hammering the app-server while its turn boundary is settling.
                await asyncio.sleep(0.35 if task_turn_active(task_id, current) else 0.1)
        finally:
            if task_workers.get(task_id) is asyncio.current_task():
                task_workers.pop(task_id, None)

    task_workers[task_id] = asyncio.create_task(run())


async def enqueue_task_message(task_id: str, body: str, client_message_id: Optional[str] = None, delivery: str = "auto") -> dict:
    task_or_404(task_id)
    sample = body[:10000]
    if "\x00" in sample or "�PNG" in sample or ("IHDR" in sample and "IDAT" in sample and sample.count("�") > 8):
        raise HTTPException(400, "检测到被当作文本发送的二进制文件。请刷新页面后重新粘贴，文件会上传到工作区。")
    message_id = client_message_id or str(uuid.uuid4())
    # Client ids are idempotent, so two tabs retrying the same request do not
    # create duplicate turns in the Codex session.
    existing = db.one("SELECT * FROM task_messages WHERE id=?", (message_id,))
    if existing:
        return existing
    # Every regular browser message enters the durable FIFO first. If a turn
    # is active the worker waits; if the task is idle it dispatches immediately.
    # Explicit queue dispatch remains the only path that can steer a live turn.
    stamp = now()
    db.execute("INSERT INTO task_messages (id,task_id,body,status,created_at) VALUES (?,?,?,?,?)", (message_id, task_id, body, "queued", stamp))
    db.execute("UPDATE tasks SET updated_at=? WHERE id=?", (stamp, task_id))
    await broadcast_task(task_id, {"type": "message", "message_id": message_id, "status": "queued", "body": body, "created_at": stamp})
    schedule_task_drain(task_id)
    return db.one("SELECT * FROM task_messages WHERE id=?", (message_id,)) or {"id": message_id, "status": "queued"}


def command_event_session(task: dict) -> str:
    if task.get("active_session_id") and db.one("SELECT id FROM sessions WHERE id=?", (task["active_session_id"],)):
        return task["active_session_id"]
    row = db.one("SELECT id FROM sessions WHERE task_id=? ORDER BY started_at DESC LIMIT 1", (task["id"],))
    if row:
        return row["id"]
    session_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO sessions (id,task_id,status,attempt,command,started_at,finished_at,exit_code,summary,codex_session_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (session_id, task["id"], "interactive", 0, "browser slash commands", now(), now(), 0, "Browser command channel", latest_codex_session(task)),
    )
    return session_id


async def record_command_event(task: dict, event_type: str, text: str, command: str, ok: bool = True, client_message_id: Optional[str] = None) -> dict:
    session_id = command_event_session(task)
    payload = {"type": event_type, "text": text, "command": command, "ok": ok}
    if client_message_id:
        payload["client_message_id"] = client_message_id
    stamp = now()
    db.execute(
        "INSERT INTO events (session_id,ts,stream,payload) VALUES (?,?,?,?)",
        (session_id, stamp, "system", json.dumps(payload, ensure_ascii=False)),
    )
    db.execute("UPDATE tasks SET updated_at=? WHERE id=?", (stamp, task["id"]))
    await broadcast_task(task["id"], {"type": "event", "session_id": session_id, "stream": "system", "payload": payload, "ts": stamp})
    await broadcast_overview(task["id"], {"type": "command", "command": command})
    return payload


async def appserver_for_task(task: dict) -> tuple[AppServerClient, Optional[dict]]:
    provider = db.one("SELECT * FROM providers WHERE id=?", (task.get("provider_id"),)) if task.get("provider_id") else None
    return await appserver_for(provider, task), provider


async def ensure_thread_loaded(task: dict, client: AppServerClient, provider: Optional[dict] = None) -> str:
    """Load an imported native thread into this shared app-server process."""
    thread_id = latest_codex_session(task)
    if not thread_id:
        return ""
    try:
        await client.request("thread/read", {"threadId": thread_id, "includeTurns": False})
        return thread_id
    except RuntimeError:
        approval = "never" if task.get("yolo") else "on-request"
        params: dict[str, Any] = {"threadId": thread_id, "cwd": task["workspace"], "approvalPolicy": approval, "excludeTurns": True}
        if task.get("permission_profile"):
            params["permissions"] = task["permission_profile"]
        else:
            params["sandbox"] = "danger-full-access" if task.get("yolo") else "workspace-write"
        model = task.get("model") or (provider or {}).get("model")
        if model:
            params["model"] = model
        if (provider or {}).get("model_provider"):
            params["modelProvider"] = provider["model_provider"]
        if task.get("personality"):
            params["personality"] = task["personality"]
        await client.request("thread/resume", params)
        return thread_id


def task_is_running(task: dict) -> bool:
    return task["id"] in running or task["id"] in appserver_turn_tasks or task.get("status") == "running"


def markdown_rows(rows: list[dict], fields: list[tuple[str, str]], empty: str) -> str:
    if not rows:
        return empty
    result = []
    for row in rows:
        values = [str(row.get(key, "")) for key, _label in fields if row.get(key) not in {None, ""}]
        result.append(f"- {' · '.join(values)}")
    return "\n".join(result)


def secure_request_cookie(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
    return request.url.scheme == "https" or forwarded == "https"


@app.get("/api/auth/status")
async def auth_status(request: Request):
    session = request_auth_session(request)
    return {
        "mode": AUTH_MODE,
        "authenticated": bool(session),
        "local_access": bool((session or {}).get("local")),
        "username": (session or {}).get("username", ""),
        "server_hostname": socket.gethostname(),
        "ssh_host": AUTH_SSH_HOST,
        "ssh_port": AUTH_SSH_PORT,
    }


@app.get("/api/profile")
async def get_profile(session: Any = Depends(auth)):
    return profile_snapshot(session["username"])


@app.get("/api/profile/avatar")
async def get_profile_avatar(session: Any = Depends(auth)):
    path = profile_avatar_path(session["username"])
    if not path.is_file():
        raise HTTPException(404, "Avatar not found")
    media_type = "image/gif" if path.suffix == ".gif" else "image/webp"
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "private, max-age=31536000, immutable"})


@app.put("/api/profile/avatar")
async def update_profile_avatar(payload: AvatarIn, session: Any = Depends(auth)):
    content, suffix = avatar_file(payload.data_url)
    profile_id = hashlib.sha256(session["username"].encode("utf-8")).hexdigest()
    path = DATA_DIR / "profiles" / f"{profile_id}{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(content)
    temporary.chmod(0o600)
    temporary.replace(path)
    path.with_suffix(".webp" if suffix == ".gif" else ".gif").unlink(missing_ok=True)
    profile = profile_snapshot(session["username"])
    await broadcast_profile(session["username"], profile)
    return profile


@app.get("/api/live")
async def live():
    return {"ok": True, "version": app.version}


@app.post("/api/auth/login")
async def ssh_login(payload: SSHLoginIn, request: Request):
    if AUTH_MODE == "none" or local_auth_session(request.client.host if request.client else "", request.headers):
        return {"authenticated": True, "username": getpass.getuser()}
    client = request.client.host if request.client else "unknown"
    if not login_throttle.allowed(client):
        raise HTTPException(429, "Too many failed SSH login attempts; try again later")
    username = payload.username.strip()
    valid, _detail = await asyncio.to_thread(
        verify_ssh_password,
        username,
        payload.password.get_secret_value(),
        host=AUTH_SSH_HOST,
        port=AUTH_SSH_PORT,
        known_hosts=DATA_DIR / "ssh-login-known-hosts",
    )
    if not valid:
        login_throttle.fail(client)
        raise HTTPException(401, "SSH username or password is incorrect")
    login_throttle.clear(client)
    token = secrets.token_urlsafe(32)
    auth_sessions[token] = {"username": username, "expires_at": time.time() + AUTH_SESSION_TTL}
    response = JSONResponse({"authenticated": True, "username": username})
    response.set_cookie(
        AUTH_COOKIE,
        token,
        max_age=AUTH_SESSION_TTL,
        httponly=True,
        secure=secure_request_cookie(request),
        samesite="strict",
        path="/",
    )
    return response


@app.post("/api/auth/logout")
async def ssh_logout(request: Request, response: Response):
    token = request.cookies.get(AUTH_COOKIE, "")
    if token:
        auth_sessions.pop(token, None)
    response.delete_cookie(AUTH_COOKIE, path="/", samesite="strict")
    return {"authenticated": False}


@app.get("/api/commands")
async def list_slash_commands(_: Any = Depends(auth)):
    return {"version": app.version, "commands": SLASH_COMMANDS}


@app.get("/api/health")
async def health(_: Any = Depends(auth)):
    active = db.one("SELECT COUNT(*) count FROM tasks WHERE status IN ('running','retrying')")["count"]
    return {
        "ok": True,
        "codex_bin": CODEX_BIN,
        "codex_available": CODEX_AVAILABLE,
        "codex_bin_source": CODEX_DISCOVERY.get("source", "missing"),
        "codex_install": None if CODEX_AVAILABLE else codex_install_plan(),
        "auth_mode": AUTH_MODE,
        "running": active,
        "server_user": getpass.getuser(),
        "server_hostname": socket.gethostname(),
        "default_workspace": str(DEFAULT_WORKSPACE),
        "bind_host": DASHBOARD_HOST,
        "bind_port": DASHBOARD_PORT,
    }


@app.get("/api/codex/manage")
async def codex_manage(_: Any = Depends(auth)):
    return codex_management_snapshot()


@app.get("/api/ssh/hosts")
async def list_ssh_hosts(probe: bool = True, _: Any = Depends(auth)):
    hosts = configured_ssh_hosts()
    if probe and hosts:
        results = await asyncio.gather(*(connect_ssh_host(host) for host in hosts), return_exceptions=True)
        for host, result in zip(hosts, results):
            if isinstance(result, Exception):
                ssh_connection_cache[host] = {
                    **ssh_effective_config(host),
                    "status": "failed",
                    "connected": False,
                    "last_error": clean_ssh_error(str(result)),
                }
    return {"config": str(SSH_CONFIG), "hosts": [ssh_host_row(host) for host in hosts]}


@app.post("/api/ssh/connect")
async def connect_ssh(payload: SSHConnectIn, _: Any = Depends(auth)):
    raw_host = payload.host.strip()
    if payload.username.strip() and "@" not in raw_host:
        raw_host = f"{payload.username.strip()}@{raw_host}"
    if payload.port != 22 and ":" not in raw_host.rsplit("@", 1)[-1]:
        raw_host = f"{raw_host}:{payload.port}"
    host = validate_ssh_host(raw_host)
    result = await connect_ssh_host(host, payload.password.get_secret_value() if payload.password else "")
    if not SSH_FIXED_HOST:
        db.execute("INSERT OR IGNORE INTO ssh_saved_hosts (alias,created_at) VALUES (?,?)", (host, now()))
    return result


@app.post("/api/ssh/disconnect")
async def disconnect_ssh(host: str, _: Any = Depends(auth)):
    await asyncio.to_thread(disconnect_ssh_host, host)
    return {"ok": True, "host": validate_ssh_host(host), "status": "disconnected"}


@app.post("/api/ssh/install-codex")
async def install_remote_codex(host: str, _: Any = Depends(auth)):
    host = validate_ssh_host(host)
    connection = await require_ssh_connection(host)
    if connection.get("codex_bin"):
        return connection
    script = r'''
set -eu
npm=$(command -v npm || true)
[ -n "$npm" ] || { printf '%s\n' 'Node.js/npm is not installed on the remote host' >&2; exit 127; }
prefix="$HOME/.local/share/codex-dashboard/npm"
mkdir -p "$prefix"
"$npm" install --global --prefix "$prefix" @openai/codex@latest
"$prefix/bin/codex" --version >/dev/null
'''.strip()
    result = await asyncio.to_thread(ssh_capture, host, shlex.join(["sh", "-lc", script]), 600)
    if result.returncode != 0:
        raise HTTPException(502, clean_ssh_error(result.stderr or result.stdout))
    ssh_connection_cache.pop(host, None)
    return await connect_ssh_host(host)


@app.delete("/api/ssh/hosts/{host}")
async def delete_ssh_host(host: str, _: Any = Depends(auth)):
    host = validate_ssh_host(host)
    if db.one("SELECT id FROM tasks WHERE ssh_host=? LIMIT 1", (host,)):
        raise HTTPException(409, "SSH host is still used by a task")
    await asyncio.to_thread(disconnect_ssh_host, host)
    db.execute("DELETE FROM ssh_saved_hosts WHERE alias=?", (host,))
    return {"ok": True}


@app.post("/api/codex/install")
async def install_codex(force: bool = False, _: Any = Depends(auth)):
    if CODEX_AVAILABLE and not force:
        return {
            "ok": True,
            "already_installed": True,
            "codex_bin": CODEX_BIN,
            "source": CODEX_DISCOVERY.get("source", "path"),
        }
    plan = codex_install_plan()
    if not plan["supported"]:
        raise HTTPException(409, plan["reason"])
    if codex_install_lock.locked():
        raise HTTPException(409, "Codex 正在安装，请稍候")
    async with codex_install_lock:
        prefix = Path(plan["prefix"])
        prefix.mkdir(parents=True, exist_ok=True)
        npm = find_npm_executable()
        if not npm:
            raise HTTPException(409, "安装前未找到 npm，请先安装 Node.js LTS")
        command = [npm, "install", "--global", "--prefix", str(prefix), CODEX_INSTALL_PACKAGE]
        env = os.environ.copy()
        extra_bin = prefix if os.name == "nt" else prefix / "bin"
        env["PATH"] = os.pathsep.join((str(Path(npm).parent), str(extra_bin), env.get("PATH", "")))
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            output, _ = await asyncio.wait_for(process.communicate(), timeout=300)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise HTTPException(504, "Codex 安装超过 5 分钟，已终止；请检查服务器网络和 npm 配置")
        except OSError as exc:
            raise HTTPException(500, f"无法启动 npm：{exc}")
        text = output.decode(errors="replace")[-8000:]
        if process.returncode != 0:
            raise HTTPException(500, f"Codex 安装失败（npm exit {process.returncode}）：\n{text}")
        discovery = refresh_codex_discovery()
        if not discovery["available"]:
            raise HTTPException(500, f"npm 安装完成，但仍未找到可运行的 Codex。安装输出：\n{text}")
        try:
            await sync_native_threads()
        except Exception:
            pass
        return {
            "ok": True,
            "already_installed": False,
            "codex_bin": CODEX_BIN,
            "source": discovery["source"],
            "output": text,
        }


@app.post("/api/codex/update")
async def update_codex(_: Any = Depends(auth)):
    """Install the latest Codex CLI package using the configured user prefix."""
    return await install_codex(force=True, _=None)


@app.get("/api/overview")
async def overview(_: Any = Depends(auth)):
    counts = {r["status"]: r["count"] for r in db.all("SELECT status, COUNT(*) count FROM tasks GROUP BY status")}
    active = sum(counts.get(status, 0) for status in ("running", "retrying"))
    return {"counts": counts, "running": active, "providers": len(provider_rows()), "skills": db.one("SELECT COUNT(*) count FROM skills")["count"]}


@app.get("/api/tasks")
async def list_tasks(_: Any = Depends(auth)):
    rows = db.all("SELECT * FROM tasks WHERE trashed=0 ORDER BY updated_at DESC")
    for r in rows:
        r["yolo"] = bool(r["yolo"])
        r["retry_forever"] = bool(r.get("retry_forever", 0))
        r["native"] = bool(r.get("native", 0))
        r["archived"] = bool(r.get("archived", 0))
        r["trashed"] = bool(r.get("trashed", 0))
    return rows


@app.post("/api/native/sync")
async def native_sync(_: Any = Depends(auth)):
    try:
        return await sync_native_threads()
    except Exception as exc:
        raise HTTPException(503, f"Codex thread sync failed: {exc}")


@app.post("/api/tasks")
async def create_task(payload: TaskCreate, _: Any = Depends(auth)):
    requested_thread_id = safe_thread_id(payload.codex_session_id)
    task_id, stamp = requested_thread_id or str(uuid.uuid4()), now()
    if requested_thread_id and db.one("SELECT id FROM tasks WHERE id=?", (requested_thread_id,)):
        task_id = str(uuid.uuid4())
    goal_status = "active" if payload.goal.strip() else "none"
    ssh_host = validate_ssh_host(payload.ssh_host) if payload.ssh_host.strip() else ""
    if ssh_host:
        connection = await require_ssh_connection(ssh_host, codex=True)
        workspace = remote_workspace_path(payload.workspace, connection.get("remote_home", ""))
    else:
        workspace = str(resolve_task_workspace(payload.workspace)) if payload.workspace.strip() else str(create_session_workspace(task_id))
    db.execute(
        "INSERT INTO tasks (id,name,prompt,goal,workspace,status,yolo,max_retries,retry_forever,provider_id,model,context,codex_session_id,goal_status,created_at,updated_at,native,reasoning_effort,service_tier,personality,collaboration_mode,permission_profile,ssh_host) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (task_id, payload.name, payload.prompt, payload.goal, workspace, "queued", int(payload.yolo), payload.max_retries, int(payload.retry_forever or bool(payload.goal.strip())), payload.provider_id, payload.model, payload.context, requested_thread_id, goal_status, stamp, stamp, 0, payload.reasoning_effort, payload.service_tier, payload.personality, payload.collaboration_mode, payload.permission_profile, ssh_host),
    )
    result = task_or_404(task_id)
    await broadcast_overview(task_id, {"type": "created"})
    return result


@app.post("/api/tasks/quick")
async def create_quick_task(payload: QuickTaskCreate = QuickTaskCreate(), _: Any = Depends(auth)):
    """Create an idle, YOLO-enabled session for the one-click new-session flow."""
    task_id, stamp = str(uuid.uuid4()), now()
    ssh_host = validate_ssh_host(payload.ssh_host) if payload.ssh_host.strip() else ""
    if ssh_host:
        connection = await require_ssh_connection(ssh_host, codex=True)
        workspace = remote_workspace_path(payload.workspace, connection.get("remote_home", ""))
    else:
        workspace = str(resolve_task_workspace(payload.workspace)) if payload.workspace.strip() else str(create_session_workspace(task_id))
    db.execute(
        "INSERT INTO tasks (id,name,prompt,goal,workspace,status,yolo,max_retries,retry_forever,provider_id,model,context,codex_session_id,goal_status,created_at,updated_at,native,reasoning_effort,service_tier,personality,collaboration_mode,permission_profile,ssh_host) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (task_id, payload.name.strip() or "新 Codex 会话", "", "", workspace, "available", 1, 3, 0, None, "", "", "", "none", stamp, stamp, 0, "", "", "", "default", "", ssh_host),
    )
    result = task_or_404(task_id)
    await broadcast_overview(task_id, {"type": "created", "quick": True})
    return result


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str, _: Any = Depends(auth)):
    task = task_or_404(task_id)
    task_id = task["id"]
    task["sessions"] = db.all("SELECT * FROM sessions WHERE task_id=? ORDER BY attempt DESC LIMIT 40", (task_id,))
    task["session_count"] = db.one("SELECT COUNT(*) count FROM sessions WHERE task_id=?", (task_id,))["count"]
    return task


@app.get("/api/trash")
async def list_trash(_: Any = Depends(auth)):
    rows = db.all("SELECT * FROM tasks WHERE trashed=1 ORDER BY trashed_at DESC, updated_at DESC")
    for row in rows:
        row["yolo"] = bool(row.get("yolo"))
        row["retry_forever"] = bool(row.get("retry_forever"))
        row["native"] = bool(row.get("native"))
        row["archived"] = bool(row.get("archived"))
        row["trashed"] = True
    return rows


@app.post("/api/trash/{task_id}/restore")
async def restore_trash(task_id: str, _: Any = Depends(auth)):
    task = task_or_404(task_id)
    if not task.get("trashed"):
        raise HTTPException(409, "Task is not in the recycle bin")
    status = "archived" if task.get("archived") else "available"
    db.execute("UPDATE tasks SET trashed=0,trashed_at=NULL,status=?,updated_at=? WHERE id=?", (status, now(), task_id))
    result = task_or_404(task_id)
    await broadcast_overview(task_id, {"type": "trash", "operation": "restore"})
    return result


@app.delete("/api/trash/{task_id}")
async def permanently_delete_trash(task_id: str, _: Any = Depends(auth)):
    task = task_or_404(task_id)
    if not task.get("trashed"):
        raise HTTPException(409, "Task must be moved to the recycle bin first")
    thread_id = latest_codex_session(task)
    if thread_id:
        provider = db.one("SELECT * FROM providers WHERE id=?", (task.get("provider_id"),)) if task.get("provider_id") else None
        try:
            client = await appserver_for(provider, task)
            await ensure_thread_loaded(task, client, provider)
            await client.request("thread/delete", {"threadId": thread_id})
        except Exception as exc:
            raise HTTPException(502, f"Codex thread deletion failed: {exc}")
    db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    native_history_cache.pop(task_id, None)
    native_history_locks.pop(task_id, None)
    runtime_metric_cache.pop(task_id, None)
    await broadcast_overview_removed(task_id)
    return {"ok": True, "deleted": True, "task_id": task_id}


@app.get("/api/tasks/{task_id}/models")
async def task_models(task_id: str, _: Any = Depends(auth)):
    task = task_or_404(task_id)
    provider = db.one("SELECT * FROM providers WHERE id=?", (task.get("provider_id"),)) if task.get("provider_id") else None
    appserver_models: list[Any] = []
    appserver_error: Optional[Exception] = None
    try:
        client = await appserver_for(provider, task)
        result = await client.request("model/list", {"includeHidden": False, "limit": 10000})
        appserver_models = result.get("data") or result.get("models") or []
    except Exception as exc:
        appserver_error = exc
    provider_result = await asyncio.to_thread(provider_probe, provider) if provider else {"models": []}
    provider_models_rows = provider_result.get("models") or []
    if appserver_error and not provider_models_rows:
        raise HTTPException(502, f"Codex model list failed: {appserver_error}")
    models = merge_model_rows(appserver_models, provider_models_rows)
    return {
        "models": models,
        "current": task.get("model") or "",
        "reasoning_effort": task.get("reasoning_effort") or "",
        "sources": {
            "app_server": len(appserver_models),
            "provider": len(provider_models_rows),
            "provider_status": provider_result.get("status", "not_configured"),
        },
    }


@app.patch("/api/tasks/{task_id}")
async def patch_task(task_id: str, payload: TaskPatch, _: Any = Depends(auth)):
    current = task_or_404(task_id)
    values = payload.model_dump(exclude_unset=True)
    if not values:
        return current
    target_host = validate_ssh_host(values.get("ssh_host") or current.get("ssh_host")) if (values.get("ssh_host") or current.get("ssh_host")) else ""
    if "ssh_host" in values and target_host != (current.get("ssh_host") or "") and current["status"] in {"running", "retrying", "queued"}:
        raise HTTPException(409, "Stop the active Codex turn before changing its SSH host")
    connection = await require_ssh_connection(target_host, codex=True) if target_host else None
    if "workspace" in values or "ssh_host" in values:
        requested_workspace = values.get("workspace")
        if requested_workspace is None and target_host != (current.get("ssh_host") or ""):
            requested_workspace = (connection or {}).get("remote_home", "") if target_host else str(DEFAULT_WORKSPACE)
        workspace = remote_workspace_path(requested_workspace or current["workspace"], (connection or {}).get("remote_home", "")) if target_host else str(resolve_task_workspace(requested_workspace or current["workspace"]))
        if workspace != current["workspace"] and current["status"] in {"running", "retrying", "queued"}:
            raise HTTPException(409, "Stop the active Codex turn before changing its workspace")
        values["workspace"] = workspace
        values["ssh_host"] = target_host
    if "goal" in values and "goal_status" not in values:
        values["goal_status"] = "active" if (values["goal"] or "").strip() else "none"
    goal_default_retry = "goal" in values and "retry_forever" not in values
    if goal_default_retry:
        values["retry_forever"] = bool((values["goal"] or "").strip())
    allowed = {
        "name", "prompt", "goal", "workspace", "yolo", "max_retries", "retry_forever", "provider_id", "model",
        "codex_session_id", "goal_status", "reasoning_effort", "service_tier", "personality", "collaboration_mode",
        "permission_profile", "ssh_host",
    }
    sets, args = [], []
    for key, value in values.items():
        if key not in allowed:
            continue
        sets.append(f"{key}=?")
        args.append(int(value) if key in {"yolo", "retry_forever"} else value)
    if "retry_forever" in values:
        sets.append("retry_explicit=?")
        args.append(0 if goal_default_retry else 1)
    sets.append("updated_at=?"); args.append(now()); args.append(task_id)
    db.execute(f"UPDATE tasks SET {','.join(sets)} WHERE id=?", tuple(args))
    result = task_or_404(task_id)
    if "model" in values:
        persist_native_thread_model(result.get("codex_session_id") or task_id, str(values.get("model") or ""))
    await broadcast_overview(task_id, {"type": "updated"})
    return result


@app.put("/api/tasks/{task_id}/context")
async def patch_context(task_id: str, payload: ContextPatch, _: Any = Depends(auth)):
    task_or_404(task_id)
    db.execute("UPDATE tasks SET context=?, updated_at=? WHERE id=?", (payload.context, now(), task_id))
    return task_or_404(task_id)


@app.put("/api/tasks/{task_id}/goal")
async def patch_goal(task_id: str, payload: GoalPatch, _: Any = Depends(auth)):
    task = task_or_404(task_id)
    remote_goal: dict[str, Any] = {}
    thread_id = latest_codex_session(task)
    if thread_id and (payload.objective is not None or payload.status is not None):
        binding = app_thread_bindings.get(task_id)
        client = app_servers.get(binding[0]) if binding else None
        if not client:
            client, _provider = await appserver_for_task(task)
            await ensure_thread_loaded(task, client)
        try:
            if payload.objective is not None and not payload.objective.strip():
                await client.request("thread/goal/clear", {"threadId": thread_id})
            else:
                objective = payload.objective if payload.objective is not None else task.get("goal", "")
                status = payload.status or ("active" if objective else None)
                remote_goal = (await client.request(
                    "thread/goal/set",
                    {"threadId": thread_id, "objective": objective, "status": status},
                )).get("goal") or {}
        except Exception as exc:
            raise HTTPException(502, f"Codex Goal sync failed: {exc}")
    updates = {}
    if payload.objective is not None:
        updates["goal"] = payload.objective
        updates["goal_status"] = remote_goal.get("status") or payload.status or ("active" if payload.objective.strip() else "none")
        updates["goal_tokens_used"] = int(remote_goal.get("tokensUsed", 0) or 0)
        if payload.objective.strip():
            # A newly set Goal is durable work by default. Browser disconnects
            # do not stop it, and transport/process failures keep resuming the
            # same thread until Codex reports the Goal complete.
            updates["retry_forever"] = 1
            updates["retry_explicit"] = 0
        else:
            updates["retry_forever"] = 0
            updates["retry_explicit"] = 0
    elif payload.status is not None:
        updates["goal_status"] = remote_goal.get("status") or payload.status
    if updates:
        sets = ",".join(f"{key}=?" for key in updates)
        db.execute(f"UPDATE tasks SET {sets}, updated_at=? WHERE id=?", tuple(updates.values()) + (now(), task_id))
    result = task_or_404(task_id)
    await broadcast_task(task_id, {"type": "task_status", "task": result, "source": {"kind": "goal"}})
    await broadcast_overview(task_id, {"kind": "goal"})
    return result


def history_event_identity(event: dict) -> Optional[tuple[str, str, str]]:
    try:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else json.loads(event.get("payload") or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    event_type = str(payload.get("type") or "").lower()
    turn_id = str(payload.get("turn_id") or payload.get("turnId") or "")
    if not event_type or not turn_id:
        return None
    if event_type in {"usermessage", "browsermessage"}:
        return (turn_id, "user", str(payload.get("text") or ""))
    if event_type == "agentmessage":
        return (turn_id, "agent", str(payload.get("text") or ""))
    item_id = str(payload.get("item_id") or "")
    return (turn_id, event_type, item_id or str(payload.get("text") or ""))


def runtime_metric_events(events: list[dict], limit: int = 3) -> list[dict]:
    """Keep timing events for recent turns without replaying their chat content."""
    parsed: list[tuple[dict, dict, str]] = []
    turns: list[str] = []
    for event in events:
        if event.get("stream") != "app-server":
            continue
        try:
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else json.loads(event.get("payload") or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        turn_id = str(payload.get("turn_id") or payload.get("turnId") or params.get("turnId") or "")
        if not turn_id:
            continue
        parsed.append((event, payload, turn_id))
        if turn_id not in turns:
            turns.append(turn_id)
    keep = set(turns[-limit:])
    metric_types = {"usermessage", "agentmessagestarted", "agent_delta", "agentmessage", "turn_completed", "turn_aborted"}
    selected: list[tuple[dict, dict, str]] = []
    for event, payload, turn_id in parsed:
        event_type = str(payload.get("type") or "").lower()
        token_usage = event_type == "codex" and str(payload.get("method") or "") == "thread/tokenUsage/updated"
        if turn_id not in keep or (event_type not in metric_types and not token_usage):
            continue
        selected.append((event, payload, turn_id))
    delta_boundaries: set[int] = set()
    delta_groups: dict[tuple[str, str], list[int]] = {}
    for index, (_event, payload, turn_id) in enumerate(selected):
        if str(payload.get("type") or "").lower() != "agent_delta":
            continue
        delta_groups.setdefault((turn_id, str(payload.get("item_id") or "")), []).append(index)
    for indexes in delta_groups.values():
        delta_boundaries.update({indexes[0], indexes[-1]})
    result = []
    for index, (event, payload, _turn_id) in enumerate(selected):
        if str(payload.get("type") or "").lower() == "agent_delta" and index not in delta_boundaries:
            continue
        copy = dict(event)
        copy["stream"] = "metrics"
        result.append(copy)
    return result


def recent_chat_events(events: list[dict], limit: int = 600) -> list[dict]:
    """Return compact persisted chat items without replaying token deltas."""
    # Rollout events already provide compact tool/activity history. App-server
    # rows are used here only for authoritative chat turns; their raw tool
    # payloads can contain megabytes of command output and request metadata.
    visible_types = {"usermessage", "browsermessage", "agentmessage", "contextcompaction"}
    result = []
    for event in events:
        try:
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else json.loads(event.get("payload") or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or str(payload.get("type") or "").lower() not in visible_types:
            continue
        result.append(event)
    return result[-limit:]


def encode_history_cursor(value: dict[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_history_cursor(value: str) -> dict[str, Any]:
    if not value:
        return {}
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        decoded = json.loads(raw)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "Invalid history cursor") from exc
    if not isinstance(decoded, dict):
        raise HTTPException(400, "Invalid history cursor")
    return decoded


def history_event_key(event: dict) -> tuple[str, str]:
    stamp = native_stamp(event.get("ts"), "") or ""
    return stamp, str(event.get("id") or "")


async def native_timeline_events(task: dict) -> tuple[list[dict], list[dict]]:
    """Merge one cached native snapshot with small live/persisted overlays."""
    native_events = list(await native_history_events(task))
    persisted = db.all(
        "SELECT * FROM (SELECT * FROM events "
        "WHERE task_id=? AND stream IN ('system','rollout') ORDER BY id DESC LIMIT 1500) "
        "ORDER BY ts,id",
        (task["id"],),
    )
    metric_version_row = db.one(
        "SELECT id FROM events WHERE task_id=? AND stream='app-server' ORDER BY id DESC LIMIT 1",
        (task["id"],),
    )
    metric_version = int((metric_version_row or {}).get("id") or 0)
    cached_metrics = runtime_metric_cache.get(task["id"])
    if cached_metrics and cached_metrics[0] == metric_version:
        metrics = cached_metrics[1]
    else:
        metric_source = db.all(
            "SELECT * FROM (SELECT * FROM events "
            "WHERE task_id=? AND stream='app-server' ORDER BY id DESC LIMIT 2000) ORDER BY id",
            (task["id"],),
        )
        metrics = runtime_metric_events(metric_source)
        runtime_metric_cache[task["id"]] = (metric_version, metrics)
    rollout_events = [event for event in persisted if event.get("stream") == "rollout"]
    rollout_identities = {identity for event in rollout_events if (identity := history_event_identity(event))}
    native_events = [event for event in native_events if history_event_identity(event) not in rollout_identities]
    merged = native_events + persisted
    merged.sort(key=history_event_key)
    return merged, metrics


@app.get("/api/tasks/{task_id}/timeline")
async def task_timeline(task_id: str, before: str = "", limit: int = 160, _: Any = Depends(auth)):
    """Return one newest-first cursor page, rendered oldest-to-newest by clients."""
    task = task_or_404(task_id)
    task_id = task["id"]
    limit = max(25, min(limit, 500))
    cursor = decode_history_cursor(before)
    if task.get("native"):
        events, metrics = await native_timeline_events(task)
        if cursor:
            if cursor.get("kind") != "native" or not isinstance(cursor.get("ts"), str):
                raise HTTPException(400, "History cursor does not match this task")
            boundary = (cursor["ts"], str(cursor.get("id") or ""))
            events = [event for event in events if history_event_key(event) < boundary]
        has_more = len(events) > limit
        items = events[-limit:]
        next_cursor = ""
        if has_more and items:
            stamp, event_id = history_event_key(items[0])
            next_cursor = encode_history_cursor({"kind": "native", "ts": stamp, "id": event_id})
        return {"items": items, "next_cursor": next_cursor, "has_more": has_more, "metrics": metrics}

    before_id = cursor.get("id") if cursor else None
    if cursor and (cursor.get("kind") != "db" or not isinstance(before_id, int)):
        raise HTTPException(400, "History cursor does not match this task")
    query = (
        "SELECT * FROM events WHERE task_id=? "
        + ("AND id<? " if before_id is not None else "")
        + "ORDER BY id DESC LIMIT ?"
    )
    args: tuple[Any, ...] = (task_id, before_id, limit + 1) if before_id is not None else (task_id, limit + 1)
    rows = db.all(query, args)
    has_more = len(rows) > limit
    items = list(reversed(rows[:limit]))
    next_cursor = encode_history_cursor({"kind": "db", "id": items[0]["id"]}) if has_more and items else ""
    return {"items": items, "next_cursor": next_cursor, "has_more": has_more, "metrics": []}


@app.get("/api/tasks/{task_id}/events")
async def task_events(task_id: str, session_id: Optional[str] = None, history_limit: int = 600, _: Any = Depends(auth)):
    task = task_or_404(task_id)
    if session_id:
        return db.all("SELECT * FROM events WHERE session_id=? ORDER BY id", (session_id,))
    if task.get("native"):
        events = db.all(
            "SELECT e.* FROM events e JOIN sessions s ON s.id=e.session_id "
            "WHERE s.task_id=? AND e.stream IN ('system','rollout') ORDER BY e.id",
            (task_id,),
        )
        metric_source = db.all(
            "SELECT * FROM (SELECT e.* FROM events e JOIN sessions s ON s.id=e.session_id "
            "WHERE s.task_id=? AND e.stream='app-server' ORDER BY e.id DESC LIMIT 5000) ORDER BY id",
            (task_id,),
        )
        try:
            if history_limit > 0:
                native_events = recent_chat_events(metric_source, history_limit)
                history_hidden = -1
            else:
                native_events = await native_history_events(task)
                history_hidden = 0
            rollout_events = [event for event in events if event.get("stream") == "rollout"]
            metric_events = runtime_metric_events(metric_source)
            rollout_identities = {identity for event in rollout_events if (identity := history_event_identity(event))}
            native_events = [event for event in native_events if history_event_identity(event) not in rollout_identities]
            # Native history owns chat rendering. Recent app-server deltas are
            # retained on a hidden stream solely for persisted runtime metrics.
            events = native_events + [event for event in events if event.get("stream") in {"system", "rollout"}] + metric_events
            if history_hidden:
                events.append({
                    "id": f"history-{task_id}",
                    "session_id": "",
                    "ts": "",
                    "stream": "history",
                    "payload": json.dumps({"type": "historyTruncated", "count": history_hidden}),
                })
        except Exception:
            pass
    else:
        events = db.all("SELECT e.* FROM events e JOIN sessions s ON s.id=e.session_id WHERE s.task_id=? ORDER BY e.id", (task_id,))
    return events


@app.get("/api/tasks/{task_id}/workspace")
async def task_workspace(task_id: str, path: str = "", _: Any = Depends(auth)):
    task = task_or_404(task_id)
    if task.get("ssh_host"):
        return await remote_fs_json(task, "browse", path)
    root, candidate = workspace_path(task, path)
    if not candidate.exists():
        raise HTTPException(404, "Workspace path not found")
    if candidate.is_file():
        if workspace_hidden(candidate):
            raise HTTPException(403, "Sensitive workspace files are not available in browser preview")
        if candidate.stat().st_size > 512_000:
            raise HTTPException(413, "File is too large for the browser preview")
        try:
            raw = candidate.read_bytes()
        except OSError as exc:
            raise HTTPException(400, f"Unable to read workspace file: {exc}")
        editable = b"\0" not in raw
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            content = raw.decode("utf-8", errors="replace")
            editable = False
        return {"root": str(root), "entry": workspace_entry(candidate, root), "content": content, "editable": editable}
    entries = []
    try:
        children = sorted(candidate.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
    except OSError as exc:
        raise HTTPException(400, f"Unable to list workspace: {exc}")
    for child in children[:300]:
        if child.name in {".git", ".venv", "node_modules", "__pycache__"} or workspace_hidden(child):
            continue
        entries.append(workspace_entry(child, root))
    return {"root": str(root), "entry": workspace_entry(candidate, root), "entries": entries}


@app.get("/api/tasks/{task_id}/workspace-picker")
async def task_workspace_picker(task_id: str, path: str = "", _: Any = Depends(auth)):
    task = task_or_404(task_id)
    if task.get("ssh_host"):
        connection = await require_ssh_connection(task["ssh_host"])
        candidate = remote_workspace_path(path or task["workspace"], connection.get("remote_home", ""))
        remote_home = connection.get("remote_home") or task["workspace"]
        boundary = remote_home if candidate == remote_home or candidate.startswith(remote_home.rstrip("/") + "/") else task["workspace"]
        picker_task = {**task, "workspace": boundary}
        result = await remote_fs_json(picker_task, "picker", candidate)
        result["roots"] = [{"name": PurePosixPath(remote_home).name or remote_home, "path": remote_home}]
        if task["workspace"] != remote_home:
            result["roots"].append({"name": PurePosixPath(task["workspace"]).name or task["workspace"], "path": task["workspace"]})
        return result
    boundary, candidate, roots = workspace_picker_path(task, path.strip() or task["workspace"])
    try:
        children = []
        for child in candidate.iterdir():
            if child.name.startswith(".") or not child.is_dir():
                continue
            resolved = child.resolve()
            if resolved != boundary and boundary not in resolved.parents:
                continue
            children.append((child.name, resolved))
        children.sort(key=lambda item: item[0].lower())
    except OSError as exc:
        raise HTTPException(400, f"Unable to list workspace directories: {exc}")
    return {
        "path": str(candidate),
        "parent": str(candidate.parent) if candidate != boundary else None,
        "roots": [{"name": root.name or root.anchor or str(root), "path": str(root)} for root in roots],
        "entries": [{"name": name, "path": str(resolved)} for name, resolved in children[:300]],
    }


@app.put("/api/tasks/{task_id}/workspace/upload")
async def upload_workspace_file(
    task_id: str,
    request: Request,
    path: str = "",
    filename: str = "",
    overwrite: bool = False,
    _: Any = Depends(auth),
):
    task = task_or_404(task_id)
    if task.get("ssh_host"):
        name = workspace_upload_name(filename)
        directory = await remote_fs_json(task, "stat", path)
        if directory.get("entry", {}).get("kind") != "directory":
            raise HTTPException(400, "Uploads require a directory destination")
        relative_target = (PurePosixPath(path) / name).as_posix()
        result = await remote_write_stream(task, relative_target, request, overwrite)
        await broadcast_task(task_id, {"type": "workspace_changed", "action": "uploaded", "entry": result["entry"], "parent": path})
        return result
    root, directory = workspace_path(task, path)
    if not directory.exists():
        raise HTTPException(404, "Upload directory not found")
    if not directory.is_dir():
        raise HTTPException(400, "Uploads require a directory destination")
    name = workspace_upload_name(filename)
    relative_target = (Path(path) / name).as_posix()
    workspace_path(task, relative_target)
    target = directory / name
    if target.exists() and target.is_dir():
        raise HTTPException(409, "A directory already uses this name")
    temporary = directory / f".{name}.upload-{uuid.uuid4().hex}"
    total = 0
    try:
        with temporary.open("xb") as handle:
            async for chunk in request.stream():
                total += len(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        async with workspace_upload_lock:
            if target.exists() and not overwrite:
                raise HTTPException(409, "A file with this name already exists")
            os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    entry = workspace_entry(target.resolve(), root)
    await broadcast_task(task_id, {"type": "workspace_changed", "action": "uploaded", "entry": entry, "parent": path})
    return {"ok": True, "entry": entry, "bytes": total}


@app.put("/api/tasks/{task_id}/workspace/file")
async def update_workspace_file(task_id: str, payload: WorkspaceFileUpdate, path: str, _: Any = Depends(auth)):
    task = task_or_404(task_id)
    if task.get("ssh_host"):
        preview = await remote_fs_json(task, "browse", path)
        if preview.get("entry", {}).get("kind") != "file" or not preview.get("editable"):
            raise HTTPException(400, "Only UTF-8 text files can be edited in the browser")

        class ContentRequest:
            async def stream(self):
                yield payload.content.encode("utf-8")

        result = await remote_write_stream(task, path, ContentRequest(), True)
        parent = PurePosixPath(path).parent.as_posix()
        await broadcast_task(task_id, {"type": "workspace_changed", "action": "edited", "entry": result["entry"], "parent": "" if parent == "." else parent})
        return {"ok": True, "entry": result["entry"]}
    root, candidate = workspace_path(task, path)
    if not candidate.exists():
        raise HTTPException(404, "Workspace file not found")
    if not candidate.is_file():
        raise HTTPException(400, "Only files can be edited")
    if workspace_hidden(candidate):
        raise HTTPException(403, "Sensitive workspace files are not available for browser editing")
    if "\0" in payload.content:
        raise HTTPException(400, "Binary files cannot be edited in the browser")
    try:
        with candidate.open("rb") as handle:
            sample = handle.read(8192)
        if b"\0" in sample:
            raise HTTPException(400, "Binary files cannot be edited in the browser")
        codecs.getincrementaldecoder("utf-8")().decode(sample, final=False)
    except UnicodeDecodeError:
        raise HTTPException(400, "Only UTF-8 text files can be edited in the browser")
    except OSError as exc:
        raise HTTPException(400, f"Unable to read workspace file: {exc}")

    temporary = candidate.parent / f".{candidate.name}.edit-{uuid.uuid4().hex}"
    try:
        with temporary.open("x", encoding="utf-8", newline="") as handle:
            handle.write(payload.content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, candidate.stat().st_mode & 0o7777)
        async with workspace_upload_lock:
            if not candidate.exists() or not candidate.is_file():
                raise HTTPException(409, "Workspace file changed while it was being edited")
            os.replace(temporary, candidate)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    entry = workspace_entry(candidate.resolve(), root)
    parent = Path(entry["path"]).parent.as_posix()
    await broadcast_task(task_id, {"type": "workspace_changed", "action": "edited", "entry": entry, "parent": "" if parent == "." else parent})
    return {"ok": True, "entry": entry}


@app.get("/api/tasks/{task_id}/workspace/download")
async def download_workspace_file(task_id: str, path: str, _: Any = Depends(auth)):
    task = task_or_404(task_id)
    if task.get("ssh_host"):
        metadata = await remote_fs_json(task, "stat", path)
        if metadata.get("entry", {}).get("kind") != "file":
            raise HTTPException(400, "Only files can be downloaded")
        connection = await require_ssh_connection(task["ssh_host"])
        python_bin = connection.get("python_bin")
        if not python_bin:
            raise HTTPException(503, f"Python 3 was not found on SSH host {task['ssh_host']}")
        read_script = "from pathlib import Path; import shutil,sys; r=Path(sys.argv[1]).resolve(); p=(r/sys.argv[2]).resolve(); (p==r or r in p.parents) or sys.exit(4); shutil.copyfileobj(p.open('rb'),sys.stdout.buffer,1024*1024)"
        command = shlex.join([python_bin, "-c", read_script, task["workspace"], path])
        process = await asyncio.create_subprocess_exec(
            *ssh_options(task["ssh_host"], batch=True), ssh_destination(task["ssh_host"]), command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdout and process.stderr

        async def stream_remote_file():
            try:
                while chunk := await process.stdout.read(1024 * 1024):
                    yield chunk
                code = await process.wait()
                if code != 0:
                    await process.stderr.read()
            finally:
                if process.returncode is None:
                    process.terminate()
                    await process.wait()

        filename = metadata["entry"]["name"]
        media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        mode = "inline" if media_type.startswith(("image/", "audio/", "video/")) or media_type == "application/pdf" else "attachment"
        disposition = f"{mode}; filename*=UTF-8''{urllib.parse.quote(filename)}"
        return StreamingResponse(stream_remote_file(), media_type=media_type, headers={"Content-Disposition": disposition})
    _root, candidate = workspace_path(task, path)
    if not candidate.exists():
        raise HTTPException(404, "Workspace file not found")
    if not candidate.is_file():
        raise HTTPException(400, "Only files can be downloaded")
    if workspace_hidden(candidate):
        raise HTTPException(403, "Sensitive workspace files are not available for browser download")
    media_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
    disposition = "inline" if media_type.startswith(("image/", "audio/", "video/")) or media_type == "application/pdf" else "attachment"
    return FileResponse(candidate, media_type=media_type, content_disposition_type=disposition, filename=candidate.name)


def image_thumbnail(data: bytes, size: int) -> bytes:
    try:
        with Image.open(io.BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source)
            image.thumbnail((size, size), Image.Resampling.LANCZOS)
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGBA" if "transparency" in image.info else "RGB")
            output = io.BytesIO()
            image.save(output, "WEBP", quality=82, method=4)
            return output.getvalue()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise HTTPException(415, "Workspace file is not a supported image") from exc


@app.get("/api/tasks/{task_id}/workspace/thumbnail")
async def thumbnail_workspace_file(task_id: str, path: str, size: int = 640, _: Any = Depends(auth)):
    task = task_or_404(task_id)
    size = max(96, min(size, 1280))
    source_signature = ""
    if task.get("ssh_host"):
        metadata = await remote_fs_json(task, "stat", path)
        entry = metadata.get("entry") or {}
        if entry.get("kind") != "file":
            raise HTTPException(400, "Only image files have thumbnails")
        source_signature = f"{task['ssh_host']}:{task['workspace']}:{path}:{entry.get('size')}:{entry.get('mtime')}"
        cache_key = hashlib.sha256(f"{source_signature}:{size}".encode()).hexdigest()
        cache_dir = DATA_DIR / "thumbnails"
        cache_file = cache_dir / f"{cache_key}.webp"
        if not cache_file.is_file():
            connection = await require_ssh_connection(task["ssh_host"])
            python_bin = connection.get("python_bin")
            if not python_bin:
                raise HTTPException(503, f"Python 3 was not found on SSH host {task['ssh_host']}")
            read_script = "from pathlib import Path; import sys; r=Path(sys.argv[1]).resolve(); p=(r/sys.argv[2]).resolve(); (p==r or r in p.parents) or sys.exit(4); sys.stdout.buffer.write(p.read_bytes())"
            command = shlex.join([python_bin, "-c", read_script, task["workspace"], path])
            process = await asyncio.create_subprocess_exec(
                *ssh_options(task["ssh_host"], batch=True), ssh_destination(task["ssh_host"]), command,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            data, error = await process.communicate()
            if process.returncode:
                raise HTTPException(400, error.decode(errors="replace") or "Unable to read remote image")
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file.write_bytes(image_thumbnail(data, size))
    else:
        _root, candidate = workspace_path(task, path)
        if not candidate.is_file():
            raise HTTPException(404, "Workspace image not found")
        if workspace_hidden(candidate):
            raise HTTPException(403, "Sensitive workspace files are not available in browser preview")
        stat = candidate.stat()
        source_signature = f"{candidate}:{stat.st_mtime_ns}:{stat.st_size}"
        cache_key = hashlib.sha256(f"{source_signature}:{size}".encode()).hexdigest()
        cache_dir = DATA_DIR / "thumbnails"
        cache_file = cache_dir / f"{cache_key}.webp"
        if not cache_file.is_file():
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file.write_bytes(image_thumbnail(candidate.read_bytes(), size))
    return FileResponse(cache_file, media_type="image/webp", headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/sessions")
async def list_sessions(limit: int = 100, _: Any = Depends(auth)):
    limit = max(1, min(limit, 500))
    return db.all("SELECT s.*, t.name task_name, t.workspace FROM sessions s JOIN tasks t ON t.id=s.task_id ORDER BY s.started_at DESC LIMIT ?", (limit,))


@app.get("/api/memories")
async def list_memories(q: str = "", _: Any = Depends(auth)):
    return {"root": str(CODEX_MEMORY_DIR), "files": memory_rows(q), "generated": generated_memory_rows(q)}


@app.get("/api/memories/generated/{thread_id}")
async def read_generated_memory(thread_id: str, _: Any = Depends(auth)):
    path = CODEX_HOME / "memories_1.sqlite"
    if not path.is_file():
        raise HTTPException(404, "Generated memory database not found")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        row = conn.execute(
            "SELECT thread_id,raw_memory,rollout_summary,rollout_slug,generated_at,usage_count,last_usage,selected_for_phase2 "
            "FROM stage1_outputs WHERE thread_id=?",
            (thread_id,),
        ).fetchone()
        conn.close()
    except sqlite3.Error as exc:
        raise HTTPException(503, f"Generated memory read failed: {exc}")
    if not row:
        raise HTTPException(404, "Generated memory not found")
    return {
        "thread_id": row[0], "raw_memory": row[1], "rollout_summary": row[2], "slug": row[3] or "",
        "generated_at": row[4], "usage_count": row[5] or 0, "last_usage": row[6], "selected_for_phase2": bool(row[7]),
    }


@app.post("/api/memories/reset")
async def reset_generated_memories(payload: MemoryResetIn, _: Any = Depends(auth)):
    if not payload.confirm:
        raise HTTPException(400, "Explicit confirmation required")
    client = await appserver_for(None)
    await client.request("memory/reset", None)
    return {"ok": True}


@app.get("/api/memories/{name:path}")
async def read_memory(name: str, _: Any = Depends(auth)):
    path = memory_file(name)
    if not path.is_file():
        raise HTTPException(404, "Memory file not found")
    return {"name": path.relative_to(CODEX_MEMORY_DIR).as_posix(), "content": path.read_text(encoding="utf-8", errors="replace")}


@app.put("/api/memories/{name:path}")
async def write_memory(name: str, payload: MemoryIn, _: Any = Depends(auth)):
    path = memory_file(name)
    if payload.name != name:
        raise HTTPException(400, "Memory path mismatch")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload.content, encoding="utf-8")
    return {"name": name, "size": path.stat().st_size, "updated_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()}


@app.delete("/api/memories/{name:path}")
async def delete_memory(name: str, _: Any = Depends(auth)):
    path = memory_file(name)
    if not path.is_file():
        raise HTTPException(404, "Memory file not found")
    path.unlink()
    return {"ok": True}


async def launch(
    task_id: str,
    mode: str = "start",
    message: str = "",
    message_id: str = "",
    attempted_provider_ids: Optional[set[str]] = None,
) -> dict:
    task = task_or_404(task_id)
    if task.get("ssh_host"):
        await require_ssh_connection(task["ssh_host"], codex=True)
    else:
        require_codex()
    if external_turns.get(task_id):
        persist_external_task_status(task_id, dashboard_active=False)
        raise HTTPException(409, "Task is already running in a terminal Codex client")
    if task_id in running or task["status"] == "running":
        raise HTTPException(409, "Task is already running")
    providers = provider_rows()
    attempted_provider_ids = attempted_provider_ids or set()
    selected = task.get("provider_id")
    ordered = ([p for p in providers if p["id"] == selected] + [p for p in providers if p["id"] != selected]) if selected else providers
    ordered = [provider for provider in ordered if provider["id"] not in attempted_provider_ids]
    if not ordered:
        ordered = [None]
    attempt = int(task["retry_count"]) + 1
    session_id = str(uuid.uuid4())
    provider = ordered[0]
    run_mode = requested_run_mode(task, mode, message_id)
    if (USE_APP_SERVER or task.get("ssh_host")) and mode in {"start", "resume", "message"}:
        # Reserve the task before any IPC awaits, so a second browser request
        # queues behind this owner instead of opening another thread/reader.
        db.execute(
            "UPDATE tasks SET status='running',retry_count=?,execution_source='dashboard',execution_turn_id='',run_mode=?,last_error='',updated_at=? WHERE id=?",
            (attempt, run_mode, now(), task_id),
        )
        result = await launch_appserver(task, provider, mode, message, message_id, attempted_provider_ids)
        await broadcast_overview(task_id, {"type": "session", "status": "running"})
        return result
    resume_id = ""
    if mode in {"resume", "message", "auto-retry"}:
        resume_id = latest_codex_session(task)
    cmd = command_for(task, provider, resume_id, message)
    db.execute("INSERT INTO sessions (id,task_id,status,attempt,provider_id,command,started_at,codex_session_id) VALUES (?,?,?,?,?,?,?,?)", (session_id, task_id, "running", attempt, provider["id"] if provider else None, shlex.join(cmd), now(), resume_id))
    db.execute(
        "UPDATE tasks SET status='running',retry_count=?,active_session_id=?,execution_source='dashboard',execution_turn_id='',run_mode=?,last_error='',updated_at=? WHERE id=?",
        (attempt, session_id, run_mode, now(), task_id),
    )
    asyncio.create_task(supervise(task, session_id, ordered, cmd, resume_id, message_id, message))
    await broadcast_overview(task_id, {"type": "session", "status": "running"})
    return task_or_404(task_id) | {"session_id": session_id, "command": shlex.join(cmd), "mode": mode, "message_id": message_id}


async def supervise(task: dict, session_id: str, providers: list[Optional[dict]], initial_cmd: list[str], resume_id: str = "", message_id: str = "", prompt_override: str = "") -> None:
    task_id = task["id"]
    last_error = ""
    for index, provider in enumerate(providers):
        cmd = initial_cmd if index == 0 else command_for(task, provider, resume_id, prompt_override)
        db.execute(
            "UPDATE sessions SET provider_id=?,command=? WHERE id=?",
            (provider["id"] if provider else None, shlex.join(cmd), session_id),
        )
        env = os.environ.copy()
        if provider and provider.get("base_url"):
            env["OPENAI_BASE_URL"] = provider["base_url"]
        if key_value := provider_api_key(provider):
            env["OPENAI_API_KEY"] = key_value
        try:
            spawn_cmd = cmd
            cwd: Optional[str] = task["workspace"]
            if task.get("ssh_host"):
                connection = await require_ssh_connection(task["ssh_host"], codex=True)
                remote_cmd = [connection["codex_bin"], *cmd[1:]]
                spawn_cmd = [*ssh_options(task["ssh_host"], batch=True), ssh_destination(task["ssh_host"]), shlex.join(remote_cmd)]
                cwd = None
                env.pop("OPENAI_API_KEY", None)
                env.pop("OPENAI_BASE_URL", None)
            process = await asyncio.create_subprocess_exec(*spawn_cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env)
            async with running_lock:
                running[task_id] = process
            if message_id:
                db.execute("UPDATE task_messages SET status='running', session_id=?, error='' WHERE id=?", (session_id, message_id))
                await broadcast_task(task_id, {"type": "message", "message_id": message_id, "status": "running", "session_id": session_id})
            assert process.stdout
            async for raw in process.stdout:
                line = raw.decode(errors="replace").rstrip("\n")
                last_error = line[-2000:]
                parsed = safe_json(line)
                if isinstance(parsed, dict):
                    candidate = parsed.get("thread_id") or parsed.get("session_id") or parsed.get("conversation_id")
                    if candidate:
                        aligned_workspace = align_session_workspace(task_id, str(candidate))
                        if aligned_workspace:
                            task["workspace"] = str(aligned_workspace)
                        db.execute("UPDATE sessions SET codex_session_id=? WHERE id=?", (str(candidate), session_id))
                        canonical_task_id = canonicalize_task_thread_id(task_id, str(candidate))
                        if canonical_task_id != task_id:
                            task_id = canonical_task_id
                            task["id"] = canonical_task_id
                        session_id = canonicalize_session_thread_id(task_id, session_id, str(candidate))
                        persist_native_thread_model(str(candidate), task.get("model") or "")
                db.execute("INSERT INTO events (session_id,ts,stream,payload) VALUES (?,?,?,?)", (session_id, now(), "stdout", json.dumps(parsed, ensure_ascii=False)))
                await broadcast_task(task_id, {"type": "event", "session_id": session_id, "stream": "stdout", "payload": parsed, "ts": now()})
            code = await process.wait()
            async with running_lock:
                running.pop(task_id, None)
            current = task_or_404(task_id)
            if current["status"] == "stopped":
                db.execute("UPDATE sessions SET status='stopped', finished_at=?, exit_code=?, summary=? WHERE id=?", (now(), code, "Stopped by user", session_id))
                if message_id:
                    db.execute("UPDATE task_messages SET status='failed', finished_at=?, error=? WHERE id=?", (now(), "Stopped by user", message_id))
                    await broadcast_task(task_id, {"type": "message", "message_id": message_id, "status": "failed", "error": "Stopped by user", "session_id": session_id})
                await broadcast_task(task_id, {"type": "session", "session_id": session_id, "status": "stopped"})
                await drain_task_messages(task_id)
                return
            if code == 0:
                record_provider_outcome(provider, True, "Codex process completed")
                db.execute("UPDATE sessions SET status='succeeded', finished_at=?, exit_code=?, summary=? WHERE id=?", (now(), code, "Codex completed", session_id))
                db.execute(
                    "UPDATE tasks SET status='succeeded',execution_source='',execution_turn_id='',updated_at=?,last_error='' WHERE id=?",
                    (now(), task_id),
                )
                if message_id:
                    db.execute("UPDATE task_messages SET status='sent', finished_at=?, session_id=?, error='' WHERE id=?", (now(), session_id, message_id))
                    await broadcast_task(task_id, {"type": "message", "message_id": message_id, "status": "sent", "session_id": session_id})
                await broadcast_task(task_id, {"type": "session", "session_id": session_id, "status": "succeeded"})
                await drain_task_messages(task_id)
                return
            last_error = last_error or f"codex exited with code {code}"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = str(exc)
        record_provider_outcome(provider, False, last_error)
        if index + 1 < len(providers):
            db.execute("INSERT INTO events (session_id,ts,stream,payload) VALUES (?,?,?,?)", (session_id, now(), "system", json.dumps({"type": "provider_failover", "from": provider and provider["name"], "reason": last_error}, ensure_ascii=False)))
            continue
        break
    async with running_lock:
        running.pop(task_id, None)
    latest = task_or_404(task_id)
    retries = int(latest["retry_count"])
    if latest.get("retry_forever") or retries <= int(latest["max_retries"]):
        db.execute("UPDATE sessions SET status='retrying', finished_at=?, exit_code=?, summary=? WHERE id=?", (now(), 1, last_error, session_id))
        db.execute(
            "UPDATE tasks SET status='retrying',execution_source='dashboard',last_error=?,updated_at=? WHERE id=?",
            (last_error, now(), task_id),
        )
        if message_id:
            db.execute("UPDATE task_messages SET status='failed', finished_at=?, error=?, session_id=? WHERE id=?", (now(), last_error, session_id, message_id))
            await broadcast_task(task_id, {"type": "message", "message_id": message_id, "status": "failed", "error": last_error, "session_id": session_id})
        await broadcast_task(task_id, {"type": "session", "session_id": session_id, "status": "retrying", "error": last_error})
        await asyncio.sleep(min(30, 2 ** min(retries, 4)))
        try:
            await launch(task_id, "resume" if latest.get("goal") else "auto-retry")
        except Exception:
            pass
    else:
        db.execute("UPDATE sessions SET status='failed', finished_at=?, exit_code=?, summary=? WHERE id=?", (now(), 1, last_error, session_id))
        db.execute(
            "UPDATE tasks SET status='failed',execution_source='',execution_turn_id='',last_error=?,updated_at=? WHERE id=?",
            (last_error, now(), task_id),
        )
        if message_id:
            db.execute("UPDATE task_messages SET status='failed', finished_at=?, error=?, session_id=? WHERE id=?", (now(), last_error, session_id, message_id))
            await broadcast_task(task_id, {"type": "message", "message_id": message_id, "status": "failed", "error": last_error, "session_id": session_id})
        await broadcast_task(task_id, {"type": "session", "session_id": session_id, "status": "failed", "error": last_error})
        await drain_task_messages(task_id)


@app.post("/api/tasks/{task_id}/start")
async def start_task(task_id: str, _: Any = Depends(auth)):
    task = task_or_404(task_id)
    if external_turns.get(task_id):
        persist_external_task_status(task_id, dashboard_active=False)
        return task_or_404(task_id) | {"shared": True, "message": "Task is already running in a terminal Codex client"}
    if task_id in running or task_id in appserver_turn_tasks or task["status"] == "running":
        raise HTTPException(409, "Task is already running")
    db.execute("UPDATE tasks SET retry_count=0, status='queued', updated_at=? WHERE id=?", (now(), task_id))
    return await launch(task_id, "resume" if task.get("native") else "start")


@app.post("/api/tasks/{task_id}/resume")
async def resume_task(task_id: str, _: Any = Depends(auth)):
    task = task_or_404(task_id)
    if external_turns.get(task_id):
        if task.get("goal"):
            db.execute("UPDATE tasks SET run_mode='goal_resume',last_error='',updated_at=? WHERE id=?", (now(), task_id))
        persist_external_task_status(task_id, dashboard_active=False)
        result = task_or_404(task_id)
        await broadcast_task(task_id, {"type": "task_status", "task": result, "source": {"kind": "goal_resume", "surface": "terminal"}})
        await broadcast_overview(task_id, {"kind": "goal_resume", "surface": "terminal"})
        return result | {"shared": True, "message": "Goal resume adopted the active terminal Codex turn"}
    if task_id in running or task_id in appserver_turn_tasks or task["status"] == "running":
        # Rejoin the server-owned turn instead of starting a competing CLI
        # process. All browser tabs can continue through /messages.
        return task | {"shared": True, "message": "Task is already owned by the dashboard session"}
    if task.get("active_session_id"):
        return await launch(task_id, "resume")
    return await launch(task_id, "resume")


@app.get("/api/tasks/{task_id}/messages")
async def list_task_messages(task_id: str, _: Any = Depends(auth)):
    task = task_or_404(task_id)
    task_id = task["id"]
    return db.all("SELECT * FROM task_messages WHERE task_id=? ORDER BY created_at, id", (task_id,))


@app.delete("/api/tasks/{task_id}/messages")
async def clear_task_messages(task_id: str, _: Any = Depends(auth)):
    """Remove all waiting messages without touching an active Codex turn."""
    task_or_404(task_id)
    rows = db.all("SELECT id FROM task_messages WHERE task_id=? AND status='queued' ORDER BY created_at, id", (task_id,))
    if not rows:
        return {"ok": True, "message_ids": []}
    db.execute("DELETE FROM task_messages WHERE task_id=? AND status='queued'", (task_id,))
    for row in rows:
        await broadcast_task(task_id, {"type": "message_removed", "message_id": row["id"]})
    return {"ok": True, "message_ids": [row["id"] for row in rows]}


@app.post("/api/tasks/{task_id}/messages")
async def post_task_message(task_id: str, payload: TaskMessageIn, _: Any = Depends(auth)):
    return await enqueue_task_message(task_id, payload.message, payload.client_message_id, payload.delivery)


@app.patch("/api/tasks/{task_id}/messages/{message_id}")
async def patch_task_message(task_id: str, message_id: str, payload: TaskMessagePatch, _: Any = Depends(auth)):
    task_or_404(task_id)
    row = db.one("SELECT * FROM task_messages WHERE id=? AND task_id=?", (message_id, task_id))
    if not row:
        raise HTTPException(404, "Message not found")
    if row["status"] != "queued":
        raise HTTPException(409, "Only queued messages can be edited")
    db.execute("UPDATE task_messages SET body=?, error='' WHERE id=? AND status='queued'", (payload.message, message_id))
    result = db.one("SELECT * FROM task_messages WHERE id=?", (message_id,))
    await broadcast_task(task_id, {"type": "message", "message_id": message_id, "status": "queued", "body": payload.message, "created_at": row["created_at"]})
    return result


@app.delete("/api/tasks/{task_id}/messages/{message_id}")
async def delete_task_message(task_id: str, message_id: str, _: Any = Depends(auth)):
    task_or_404(task_id)
    row = db.one("SELECT * FROM task_messages WHERE id=? AND task_id=?", (message_id, task_id))
    if not row:
        raise HTTPException(404, "Message not found")
    if row["status"] != "queued":
        raise HTTPException(409, "Only queued messages can be deleted")
    db.execute("DELETE FROM task_messages WHERE id=? AND task_id=? AND status='queued'", (message_id, task_id))
    await broadcast_task(task_id, {"type": "message_removed", "message_id": message_id})
    return {"ok": True, "message_id": message_id}


@app.post("/api/tasks/{task_id}/messages/{message_id}/dispatch")
async def dispatch_task_message(task_id: str, message_id: str, _: Any = Depends(auth)):
    """Execute one queued message now, steering a live turn when possible."""
    task_or_404(task_id)
    row = db.one("SELECT * FROM task_messages WHERE id=? AND task_id=?", (message_id, task_id))
    if not row:
        raise HTTPException(404, "Message not found")
    if row["status"] != "queued":
        return row

    lock = task_message_locks.setdefault(task_id, asyncio.Lock())
    async with lock:
        row = db.one("SELECT * FROM task_messages WHERE id=? AND task_id=?", (message_id, task_id))
        if not row:
            raise HTTPException(404, "Message not found")
        if row["status"] != "queued":
            return row
        current = task_or_404(task_id)
        if current["status"] == "running" or task_id in running or task_id in appserver_turn_tasks:
            return await steer_task_message(
                task_id,
                TaskMessageIn(message=row["body"], client_message_id=message_id, delivery="auto"),
            )

        stamp = now()
        db.execute("UPDATE task_messages SET status='dispatching', started_at=?, error='' WHERE id=?", (stamp, message_id))
        await broadcast_task(task_id, {"type": "message", "message_id": message_id, "status": "dispatching", "body": row["body"], "started_at": stamp})
        try:
            result = await launch(task_id, "message", row["body"], message_id)
            if result.get("session_id"):
                db.execute("UPDATE task_messages SET session_id=? WHERE id=?", (result["session_id"], message_id))
            return db.one("SELECT * FROM task_messages WHERE id=?", (message_id,)) or row
        except HTTPException:
            db.execute("UPDATE task_messages SET status='queued', started_at=NULL, error='' WHERE id=?", (message_id,))
            await broadcast_task(task_id, {"type": "message", "message_id": message_id, "status": "queued", "body": row["body"]})
            raise
        except Exception as exc:
            error = str(exc)
            db.execute("UPDATE task_messages SET status='failed', finished_at=?, error=? WHERE id=?", (now(), error, message_id))
            await broadcast_task(task_id, {"type": "message", "message_id": message_id, "status": "failed", "body": row["body"], "error": error})
            raise HTTPException(500, error) from exc


@app.post("/api/tasks/{task_id}/steer")
async def steer_task_message(task_id: str, payload: TaskMessageIn, _: Any = Depends(auth)):
    """Insert text into the currently active Codex turn without starting a second turn."""
    task_or_404(task_id)
    message_id = payload.client_message_id or str(uuid.uuid4())
    existing = db.one("SELECT * FROM task_messages WHERE id=?", (message_id,))
    if existing and existing["status"] != "queued":
        return existing
    binding = app_thread_bindings.get(task_id)
    turn_id = appserver_turn_ids.get(task_id)
    turn_task = appserver_turn_tasks.get(task_id)
    client = app_servers.get(binding[0]) if binding else None
    thread_id = binding[1] if binding else ""
    session_id = binding[2] if binding else ""
    if not (binding and client and thread_id and turn_id and turn_task and not turn_task.done()):
        raise HTTPException(409, "当前没有可插入的 Codex turn")

    stamp = now()
    message_body = existing["body"] if existing else payload.message
    if existing:
        db.execute(
            "UPDATE task_messages SET status='steering',started_at=?,finished_at=NULL,session_id=?,error='' WHERE id=?",
            (stamp, session_id, message_id),
        )
    else:
        db.execute(
            "INSERT INTO task_messages (id,task_id,body,status,created_at,started_at,session_id) VALUES (?,?,?,?,?,?,?)",
            (message_id, task_id, message_body, "steering", stamp, stamp, session_id),
        )
    db.execute("UPDATE tasks SET updated_at=? WHERE id=?", (stamp, task_id))
    await broadcast_task(task_id, {"type": "message", "message_id": message_id, "status": "steering", "body": message_body, "created_at": stamp, "session_id": session_id})
    try:
        await client.request(
            "turn/steer",
            {
                "threadId": thread_id,
                "expectedTurnId": turn_id,
                "input": appserver_turn_inputs(task_or_404(task_id), message_body),
                "clientUserMessageId": message_id,
            },
        )
    except Exception as exc:
        error = str(exc)
        # The turn may finish between the browser's status check and this RPC.
        # Preserve the user's input by atomically falling back to the durable
        # queue instead of making the client retry the same id as a failed row.
        db.execute(
            "UPDATE task_messages SET status='queued', started_at=NULL, finished_at=NULL, session_id=NULL, error=? WHERE id=?",
            (error, message_id),
        )
        result = db.one("SELECT * FROM task_messages WHERE id=?", (message_id,)) or {"id": message_id, "status": "queued"}
        await broadcast_task(task_id, {"type": "message", "message_id": message_id, "status": "queued", "body": message_body, "error": error, "created_at": stamp})
        schedule_task_drain(task_id)
        return result
    db.execute("UPDATE task_messages SET status='steered', finished_at=? WHERE id=?", (now(), message_id))
    result = db.one("SELECT * FROM task_messages WHERE id=?", (message_id,)) or {"id": message_id, "status": "steered"}
    await broadcast_task(task_id, {"type": "message", "message_id": message_id, "status": "steered", "body": message_body, "session_id": session_id})
    return result


@app.websocket("/ws/overview")
async def overview_socket(websocket: WebSocket):
    session = websocket_auth_session(websocket)
    if not session:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    overview_clients.add(websocket)
    overview_client_users[websocket] = session["username"]
    try:
        await websocket.send_json({"type": "overview_snapshot", "tasks": all_task_summaries(), "profile": profile_snapshot(session["username"])})
        while True:
            incoming = await websocket.receive_json()
            if incoming.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        overview_clients.discard(websocket)
        overview_client_users.pop(websocket, None)


@app.websocket("/ws/tasks/{task_id}")
async def task_socket(websocket: WebSocket, task_id: str):
    if not websocket_auth_session(websocket):
        await websocket.close(code=4401)
        return
    if not db.one("SELECT id FROM tasks WHERE id=?", (task_id,)):
        await websocket.close(code=4404)
        return
    await websocket.accept()
    task_clients.setdefault(task_id, set()).add(websocket)
    try:
        await websocket.send_json({
            "type": "snapshot",
            "task": task_or_404(task_id),
            "messages": db.all("SELECT * FROM task_messages WHERE task_id=? ORDER BY created_at, id", (task_id,)),
            "pending_requests": pending_requests_for_task(task_id),
        })
        while True:
            incoming = await websocket.receive_json()
            if incoming.get("type") == "message" and str(incoming.get("message", "")).strip():
                message = await enqueue_task_message(
                    task_id,
                    str(incoming["message"]).strip(),
                    incoming.get("client_message_id"),
                    "queue" if incoming.get("delivery") == "queue" else "auto",
                )
                await websocket.send_json({"type": "ack", "message": message})
            elif incoming.get("type") == "steer" and str(incoming.get("message", "")).strip():
                message = await steer_task_message(task_id, TaskMessageIn(message=str(incoming["message"]).strip(), client_message_id=incoming.get("client_message_id")))
                await websocket.send_json({"type": "ack", "message": message})
            elif incoming.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        task_clients.get(task_id, set()).discard(websocket)


@app.post("/api/tasks/{task_id}/retry")
async def retry_task(task_id: str, _: Any = Depends(auth)):
    task = task_or_404(task_id)
    if task_id in running or task_id in appserver_turn_tasks or task["status"] == "running":
        raise HTTPException(409, "Task is already running")
    db.execute("UPDATE tasks SET retry_count=0, status='queued', updated_at=? WHERE id=?", (now(), task_id))
    return await launch(task_id, "resume" if task.get("native") else "manual-retry")


@app.post("/api/tasks/{task_id}/approvals/{request_id}")
async def resolve_task_approval(task_id: str, request_id: str, payload: ApprovalResolveIn, _: Any = Depends(auth)):
    task_or_404(task_id)
    request = pending_appserver_requests.get(request_id)
    if not request or request["task_id"] != task_id:
        raise HTTPException(404, "Approval request is no longer pending")
    future = request["future"]
    if future.done():
        raise HTTPException(409, "Approval request was already resolved")
    future.set_result(approval_result(request, payload))
    return {"ok": True, "request_id": request_id, "decision": payload.decision}


@app.post("/api/tasks/{task_id}/stop")
async def stop_task(task_id: str, _: Any = Depends(auth)):
    return await stop_task_run(task_id)


async def stop_task_run(task_id: str) -> dict:
    task = task_or_404(task_id)
    for request in list(pending_appserver_requests.values()):
        if request["task_id"] == task_id and not request["future"].done():
            request["future"].set_result(approval_result(request, ApprovalResolveIn(decision="cancel")))
    tracked_external = list((external_turn_sets.get(task_id) or {}).values())
    async with running_lock:
        process = running.get(task_id)
    turn_task = appserver_turn_tasks.get(task_id)
    if not process and not turn_task:
        external = external_turns.get(task_id)
        if external:
            path = str(external.get("path") or "")
            if path:
                await asyncio.to_thread(interrupt_rollout_writers, path)
            await settle_inactive_external_turn(task_id, "Stopped by user", external)
            return task_or_404(task_id)
        if task.get("status") in {"running", "queued", "retrying"}:
            stamp = now()
            db.execute(
                "UPDATE sessions SET status='stopped',finished_at=COALESCE(finished_at,?),exit_code=COALESCE(exit_code,130),summary='Stopped by user' "
                "WHERE task_id=? AND status IN ('running','retrying')",
                (stamp, task_id),
            )
            db.execute(
                "UPDATE tasks SET status='stopped',active_session_id=NULL,execution_source='',execution_turn_id='',run_mode='',last_error='Stopped by user',updated_at=? WHERE id=?",
                (stamp, task_id),
            )
            result = task_or_404(task_id)
            await broadcast_task(task_id, {"type": "task_status", "task": result, "source": {"kind": "stop"}})
            await broadcast_overview(task_id, {"kind": "stop"})
            return result
        raise HTTPException(409, "Task is not running")
    db.execute(
        "UPDATE tasks SET status='stopped',execution_source='',execution_turn_id='',run_mode='',last_error='Stopped by user',updated_at=? WHERE id=?",
        (now(), task_id),
    )
    # ``running`` stores the shared app-server process as an owner marker for
    # app-server turns. Never terminate that process when stopping one task;
    # other tasks may be using the same transport.
    if process and not turn_task:
        process.terminate()
    if turn_task and not turn_task.done():
        thread_id = app_thread_bindings.get(task_id, ("", "", ""))[1]
        client_key = app_thread_bindings.get(task_id, ("", "", ""))[0]
        client = app_servers.get(client_key)
        turn_id = appserver_turn_ids.get(task_id)
        if client and thread_id and turn_id:
            try:
                await client.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
            except Exception:
                pass
        turn_task.cancel()
        await asyncio.gather(turn_task, return_exceptions=True)
    if tracked_external:
        for path in {str(turn.get("path") or "") for turn in tracked_external} - {""}:
            await asyncio.to_thread(interrupt_rollout_writers, path)
        clear_external_turns(task_id)
        stamp = now()
        for external in tracked_external:
            if external.get("session_id"):
                db.execute(
                    "UPDATE sessions SET status='stopped',finished_at=COALESCE(finished_at,?),exit_code=COALESCE(exit_code,130),summary='Stopped by user' WHERE id=?",
                    (stamp, external["session_id"]),
                )
    result = task_or_404(task_id)
    await broadcast_overview(task_id, {"type": "session", "status": "stopped"})
    return result


CODEX_OPERATIONS = {
    "exec", "review", "apply", "resume", "archive", "unarchive", "delete", "fork", "rename", "compact",
    "memory-enable", "memory-disable", "doctor", "features", "mcp", "mcp-server", "plugin", "login", "logout",
    "update", "cloud", "remote-control", "app-server", "exec-server", "sandbox", "completion", "debug", "help",
}
THREAD_RPC_OPERATIONS = {"archive", "unarchive", "delete", "fork", "rename", "compact", "memory-enable", "memory-disable"}


async def run_thread_operation(task: dict, payload: OperationIn) -> dict:
    thread_id = latest_codex_session(task)
    operation = payload.operation
    if payload.operation == "delete":
        db.execute("UPDATE tasks SET trashed=1,trashed_at=?,status='trashed',active_session_id=NULL,execution_source='',execution_turn_id='',updated_at=? WHERE id=?", (now(), now(), task["id"]))
        await broadcast_overview_removed(task["id"])
        return {"ok": True, "trashed": True, "thread_id": thread_id or ""}
    if operation in {"memory-enable", "memory-disable"}:
        mode = "enabled" if operation == "memory-enable" else "disabled"
        # Memory mode is a persistent thread preference. A live turn keeps
        # its existing context, while the new mode applies to the next turn;
        # do not reject the browser action with a misleading running error.
        if task_turn_active(task["id"], task) or not thread_id:
            db.execute("UPDATE tasks SET memory_mode=?,updated_at=? WHERE id=?", (mode, now(), task["id"]))
            result = {
                "ok": True,
                "operation": operation,
                "thread_id": thread_id or "",
                "memory_mode": mode,
                "native_applied": False,
                "deferred": bool(thread_id),
            }
            await broadcast_task(task["id"], {"type": "task_status", "task": task_or_404(task["id"]), "source": {"kind": "thread", "operation": operation, "deferred": bool(thread_id)}})
            return result
    if not thread_id:
        raise HTTPException(409, "Task has no Codex thread to operate on")
    provider = db.one("SELECT * FROM providers WHERE id=?", (task.get("provider_id"),)) if task.get("provider_id") else None
    client = await appserver_for(provider, task)
    # These operations address the thread record itself and work for archived
    # threads whose rollout is intentionally not loaded in app-server memory.
    if operation in {"archive", "unarchive"}:
        method = {"archive": "thread/archive", "unarchive": "thread/unarchive"}[operation]
        result = await client.request(method, {"threadId": thread_id})
        archived = operation == "archive"
        db.execute(
            "UPDATE tasks SET archived=?,status=?,updated_at=? WHERE id=?",
            (int(archived), "archived" if archived else "available", now(), task["id"]),
        )
        await broadcast_task(task["id"], {"type": "task_status", "task": task_or_404(task["id"]), "source": {"kind": "thread", "operation": operation}})
        return {"ok": True, "operation": operation, "thread_id": thread_id, "result": result}
    if operation in {"memory-enable", "memory-disable"}:
        mode = "enabled" if operation == "memory-enable" else "disabled"
        was_archived = bool(task.get("archived"))
        if was_archived:
            # Codex does not load archived rollouts for memory mutations. The
            # archive state is restored in the finally block after the setting
            # is applied, so this remains transparent to the user.
            await client.request("thread/unarchive", {"threadId": thread_id})
        try:
            result = await client.request("thread/memoryMode/set", {"threadId": thread_id, "mode": mode})
        finally:
            if was_archived:
                await client.request("thread/archive", {"threadId": thread_id})
        db.execute("UPDATE tasks SET memory_mode=?,updated_at=? WHERE id=?", (mode, now(), task["id"]))
        await broadcast_task(task["id"], {"type": "task_status", "task": task_or_404(task["id"]), "source": {"kind": "thread", "operation": operation}})
        return {"ok": True, "operation": operation, "thread_id": thread_id, "memory_mode": mode, "result": result}
    await ensure_thread_loaded(task, client, provider)
    if operation == "rename":
        name = (payload.prompt or " ".join(payload.args)).strip()
        if not name:
            raise HTTPException(400, "rename requires a name in operation prompt or arguments")
        result = await client.request("thread/name/set", {"threadId": thread_id, "name": name[:160]})
        db.execute("UPDATE tasks SET name=?,updated_at=? WHERE id=?", (name[:160], now(), task["id"]))
        return {"ok": True, "operation": operation, "thread_id": thread_id, "result": result}
    if operation == "compact":
        result = await client.request("thread/compact/start", {"threadId": thread_id})
        return {"ok": True, "operation": operation, "thread_id": thread_id, "result": result}
    approval = "never" if task["yolo"] else "on-request"
    sandbox = "danger-full-access" if task["yolo"] else "workspace-write"
    params: dict[str, Any] = {
        "threadId": thread_id,
        "cwd": task["workspace"],
        "approvalPolicy": approval,
        "sandbox": sandbox,
    }
    if task.get("model"):
        params["model"] = task["model"]
    if provider and provider.get("model_provider"):
        params["modelProvider"] = provider["model_provider"]
    result = await client.request("thread/fork", params)
    fork_id = str((result.get("thread") or {}).get("id") or "")
    if not fork_id:
        raise HTTPException(502, "Codex did not return the forked thread id")
    if task.get("ssh_host"):
        fork_task_id, stamp = str(uuid.uuid4()), now()
        db.execute(
            "INSERT INTO tasks (id,name,prompt,goal,workspace,status,yolo,max_retries,retry_forever,retry_explicit,provider_id,model,context,codex_session_id,goal_status,created_at,updated_at,native,reasoning_effort,service_tier,personality,collaboration_mode,permission_profile,ssh_host) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fork_task_id, f"{task['name']} 副本"[:160], task.get("prompt", ""), task.get("goal", ""), task["workspace"], "available",
             int(task.get("yolo", 1)), int(task.get("max_retries", 3)), int(task.get("retry_forever", 0)), int(task.get("retry_explicit", 0)),
             task.get("provider_id"), task.get("model", ""), task.get("context", ""), fork_id, task.get("goal_status", "none"), stamp, stamp, 0,
             task.get("reasoning_effort", ""), task.get("service_tier", ""), task.get("personality", ""), task.get("collaboration_mode", "default"),
             task.get("permission_profile", ""), task["ssh_host"]),
        )
        db.execute(
            "INSERT INTO sessions (id,task_id,status,attempt,provider_id,command,started_at,finished_at,exit_code,summary,codex_session_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), fork_task_id, "imported", 0, task.get("provider_id"), f"ssh {task['ssh_host']} codex thread/fork", stamp, stamp, 0, "Forked remote Codex thread", fork_id),
        )
        forked_task = db.one("SELECT id,name,status FROM tasks WHERE id=?", (fork_task_id,))
        await broadcast_overview(fork_task_id, {"type": "created", "forked": True})
    else:
        await sync_native_threads()
        forked_task = db.one("SELECT id,name,status FROM tasks WHERE codex_session_id=?", (fork_id,))
    return {"ok": True, "operation": operation, "thread_id": thread_id, "fork_thread_id": fork_id, "task": forked_task}


def create_command_task(source: dict, prompt: str, status: str = "queued") -> dict:
    task_id, stamp = str(uuid.uuid4()), now()
    clean_prompt = prompt.strip() or "开始一个新的 Codex 会话"
    name = clean_prompt.splitlines()[0][:80] or "新 Codex 会话"
    workspace = source["workspace"] if source.get("ssh_host") else str(create_session_workspace(task_id))
    db.execute(
        "INSERT INTO tasks (id,name,prompt,goal,workspace,status,yolo,max_retries,retry_forever,provider_id,model,context,codex_session_id,goal_status,created_at,updated_at,native,reasoning_effort,service_tier,personality,collaboration_mode,permission_profile,ssh_host) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (task_id, name, clean_prompt, "", workspace, status, 1, source.get("max_retries", 3), 0,
         source.get("provider_id"), source.get("model", ""), source.get("context", ""), "", "none", stamp, stamp, 0,
         source.get("reasoning_effort", ""), source.get("service_tier", ""), source.get("personality", ""),
         source.get("collaboration_mode", "default"), source.get("permission_profile", ""), source.get("ssh_host", "")),
    )
    return task_or_404(task_id)


def git_diff_summary(task: dict) -> str:
    commands = [
        ["git", "-C", task["workspace"], "status", "--short"],
        ["git", "-C", task["workspace"], "diff", "--stat", "--"],
        ["git", "-C", task["workspace"], "diff", "--no-ext-diff", "--", "."],
    ]
    sections = []
    for index, command in enumerate(commands):
        try:
            completed = ssh_capture(task["ssh_host"], shlex.join(command), 12) if task.get("ssh_host") else subprocess.run(command, capture_output=True, text=True, timeout=8, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"无法读取 Git 变更：{exc}"
        output = (completed.stdout or completed.stderr).strip()
        if completed.returncode and index == 0:
            return output or "当前工作区不是 Git 仓库。"
        if output:
            label = ("状态", "统计", "Diff")[index]
            sections.append(f"**{label}**\n```\n{output[:12000]}\n```")
    return "\n\n".join(sections) or "工作区没有未提交变更。"


def command_help(name: str = "") -> str:
    if name:
        canonical = SLASH_ALIASES.get(name.lstrip("/").lower(), name.lstrip("/").lower())
        command = SLASH_COMMAND_BY_NAME.get(canonical)
        if not command:
            return f"没有找到 `/{name.lstrip('/')}`。输入 `/help` 查看全部命令。"
        aliases = ", ".join(f"`/{alias}`" for alias in command.get("aliases", []))
        suffix = f"\n别名：{aliases}" if aliases else ""
        return f"`/{command['name']} {command.get('args', '')}`\n\n{command['description']}{suffix}"
    categories = {"goal": "Goal", "thread": "线程", "session": "会话设置", "context": "上下文", "codex": "Codex", "browser": "浏览器", "account": "账号"}
    sections = []
    for category, title in categories.items():
        rows = [f"`/{item['name']} {item.get('args', '')}` - {item['description']}" for item in SLASH_COMMANDS if item["category"] == category]
        if rows:
            sections.append(f"**{title}**\n" + "\n".join(f"- {row}" for row in rows))
    return "\n\n".join(sections)


def result_message(command: str, message: str, **extra: Any) -> dict:
    return {"ok": True, "command": command, "message": message, **extra}


def normalize_model_command_value(value: str) -> str:
    text = str(value or "").strip()
    if text.lower() in {"", "default", "provider-default", "provider_default", "__provider_default__"}:
        return ""
    return text


async def execute_slash_command(task: dict, command: str, arg_text: str, args: list[str], confirmed: bool = False) -> dict:
    """Execute one browser slash command without creating a model turn."""
    if command == "help":
        return result_message(command, command_help(args[0] if args else ""))

    if command == "goal":
        thread_id = latest_codex_session(task)
        client = None
        if thread_id:
            client, _ = await appserver_for_task(task)
            await ensure_thread_loaded(task, client)
        first = args[0].lower() if args else ""
        statuses = {"active", "paused", "blocked", "usageLimited", "budgetLimited", "complete", "pause", "resume"}
        if not args or first in {"show", "get"} or (first == "status" and len(args) == 1):
            goal = {"objective": task.get("goal", ""), "status": task.get("goal_status", "none"), "tokensUsed": task.get("goal_tokens_used", 0)}
            if client and thread_id:
                try:
                    goal = (await client.request("thread/goal/get", {"threadId": thread_id})).get("goal") or goal
                except Exception:
                    pass
            if not goal.get("objective"):
                return result_message(command, "当前线程没有设置 Goal。\n\n用法：`/goal <objective>`")
            return result_message(command, f"**Goal**\n{goal.get('objective')}\n\n状态：`{goal.get('status', 'active')}` · 已用 tokens：{goal.get('tokensUsed', 0)}", goal=goal)
        if first == "clear":
            if client and thread_id:
                await client.request("thread/goal/clear", {"threadId": thread_id})
            db.execute("UPDATE tasks SET goal='',goal_status='none',goal_tokens_used=0,retry_forever=0,retry_explicit=0,updated_at=? WHERE id=?", (now(), task["id"]))
            return result_message(command, "Goal 已清除。")
        if first == "budget":
            if len(args) != 2 or not args[1].isdigit():
                raise HTTPException(400, "用法：/goal budget <tokens>")
            if not task.get("goal"):
                raise HTTPException(400, "当前线程还没有 Goal")
            objective, status = task["goal"], task.get("goal_status") or "active"
            if client and thread_id:
                await client.request("thread/goal/set", {"threadId": thread_id, "objective": objective, "status": status, "tokenBudget": int(args[1])})
            return result_message(command, f"Goal token budget 已设置为 `{args[1]}`。")
        if first == "status":
            if len(args) != 2:
                raise HTTPException(400, "用法：/goal status <active|paused|blocked|complete>")
            first = args[1].lower()
        status_aliases = {"usagelimited": "usageLimited", "budgetlimited": "budgetLimited"}
        if first in statuses or first in status_aliases:
            status = {"pause": "paused", "resume": "active"}.get(first, status_aliases.get(first, first))
            objective = task.get("goal", "")
            if not objective:
                raise HTTPException(400, "当前线程还没有 Goal")
            if client and thread_id:
                goal = (await client.request("thread/goal/set", {"threadId": thread_id, "objective": objective, "status": status})).get("goal") or {}
                status = goal.get("status", status)
            db.execute("UPDATE tasks SET goal_status=?,updated_at=? WHERE id=?", (status, now(), task["id"]))
            return result_message(command, f"Goal 状态已切换为 `{status}`。")
        objective = arg_text.strip()
        if not objective:
            raise HTTPException(400, "用法：/goal <objective>")
        status = "active"
        if client and thread_id:
            goal = (await client.request("thread/goal/set", {"threadId": thread_id, "objective": objective, "status": status})).get("goal") or {}
            status = goal.get("status", status)
        db.execute("UPDATE tasks SET goal=?,goal_status=?,goal_tokens_used=0,updated_at=? WHERE id=?", (objective, status, now(), task["id"]))
        return result_message(command, f"Goal 已设置：\n\n{objective}\n\n状态：`{status}`。", goal={"objective": objective, "status": status})

    if command == "status":
        goal = {"objective": task.get("goal", ""), "status": task.get("goal_status", "none"), "tokensUsed": task.get("goal_tokens_used", 0)}
        thread_id = latest_codex_session(task)
        account = None
        if USE_APP_SERVER or task.get("ssh_host"):
            client, _ = await appserver_for_task(task)
            if thread_id:
                try:
                    goal = (await client.request("thread/goal/get", {"threadId": thread_id})).get("goal") or goal
                except Exception:
                    pass
            try:
                account = (await client.request("account/read", {"refreshToken": False})).get("account")
            except Exception:
                pass
        account_text = f" · 账号：{account.get('type', '已登录')}" if account else ""
        return result_message(command, f"**会话状态**\n状态：`{task.get('status')}` · {'YOLO' if task.get('yolo') else '受控'}{account_text}\n模型：`{task.get('model') or '默认'}` · effort：`{task.get('reasoning_effort') or '默认'}`\n模式：`{task.get('collaboration_mode') or 'default'}` · fast：`{'on' if task.get('service_tier') else 'off'}`\nThread：`{thread_id or '尚未创建'}`\nGoal：`{goal.get('status', 'none')}` · tokens：{goal.get('tokensUsed', 0)}", task=task, goal=goal, account=account)

    if command == "model":
        client, _ = await appserver_for_task(task)
        if not args:
            response = await client.request("model/list", {"includeHidden": False, "limit": 100})
            models = response.get("data") or []
            rows = [f"- `{item.get('id') or item.get('model')}` {item.get('displayName', '')} · 默认 effort `{item.get('defaultReasoningEffort', '')}`" for item in models]
            return result_message(command, "**可用模型**\n" + ("\n".join(rows) or "Codex 没有返回可用模型。"), models=models)
        effort_values = {"minimal", "low", "medium", "high", "xhigh", "ultra"}
        default_values = {"", "default", "provider-default", "provider_default", "__provider_default__"}
        named: dict[str, str] = {}
        positional: list[str] = []
        for arg in args:
            key, sep, value = arg.partition("=")
            if sep and key.lower() in {"model", "effort", "reasoning_effort"}:
                named["model" if key.lower() == "model" else "effort"] = value
            else:
                positional.append(arg)
        model_value = named.get("model")
        effort_value = named.get("effort")
        if model_value is None and positional:
            first = positional[0].strip()
            if first.lower() not in effort_values:
                model_value = first
            else:
                effort_value = effort_value or first.lower()
        if effort_value is None:
            effort_value = next((value.lower() for value in positional[1:] if value.lower() in effort_values), None)
        updates: dict[str, str] = {}
        if model_value is not None:
            updates["model"] = normalize_model_command_value(model_value)
        if effort_value is not None:
            updates["reasoning_effort"] = "" if effort_value.lower() in default_values else effort_value.lower()
        if not updates:
            raise HTTPException(400, "用法：/model [model=<id>|default] [effort=<minimal|low|medium|high|xhigh|ultra>|default]")
        sets = ",".join(f"{key}=?" for key in updates)
        db.execute(f"UPDATE tasks SET {sets},updated_at=? WHERE id=?", tuple(updates.values()) + (now(), task["id"]))
        if "model" in updates:
            persist_native_thread_model(task.get("codex_session_id") or task["id"], str(updates.get("model") or ""))
        updated_task = task_or_404(task["id"])
        model_text = updated_task.get("model") or "默认"
        effort_text = updated_task.get("reasoning_effort") or "默认"
        return result_message(command, f"下一条 turn 使用模型 `{model_text}`，effort `{effort_text}`。", task=updated_task)

    if command == "fast":
        enabled = not bool(task.get("service_tier")) if not args else args[0].lower() in {"on", "true", "1", "fast", "priority"}
        tier = "fast" if enabled else ""
        if enabled and (USE_APP_SERVER or task.get("ssh_host")):
            try:
                client, _ = await appserver_for_task(task)
                response = await client.request("model/list", {"includeHidden": False, "limit": 100})
                model = next((m for m in response.get("data") or [] if (m.get("id") or m.get("model")) == (task.get("model") or "")), None)
                tier = next((item.get("id") for item in (model or {}).get("serviceTiers", []) if "fast" in item.get("id", "").lower() or "priority" in item.get("id", "").lower()), tier)
            except Exception:
                pass
        db.execute("UPDATE tasks SET service_tier=?,updated_at=? WHERE id=?", (tier, now(), task["id"]))
        return result_message(command, f"Fast mode 已{'开启' if enabled else '关闭'}；下一条 turn 生效。")

    if command == "permissions":
        if not args:
            profiles = []
            if USE_APP_SERVER or task.get("ssh_host"):
                client, _ = await appserver_for_task(task)
                try:
                    profiles = (await client.request("permissionProfile/list", {"cwd": task["workspace"], "limit": 100})).get("data") or []
                except Exception:
                    pass
            lines = [f"当前：{'YOLO' if task.get('yolo') else (task.get('permission_profile') or '受控')}" ]
            if profiles:
                lines.append("\n**可用 profile**\n" + "\n".join(f"- `{p.get('id')}` · {p.get('description') or ''}" for p in profiles))
            return result_message(command, "\n".join(lines), profiles=profiles)
        selected = args[0].lower()
        if selected in {"yolo", "never", "danger", "danger-full-access"}:
            db.execute("UPDATE tasks SET yolo=1,permission_profile='',updated_at=? WHERE id=?", (now(), task["id"]))
            return result_message(command, "YOLO 已开启（不请求审批，danger-full-access）。")
        if selected in {"controlled", "on-request", "workspace-write", "off", "safe"}:
            db.execute("UPDATE tasks SET yolo=0,permission_profile='',updated_at=? WHERE id=?", (now(), task["id"]))
            return result_message(command, "已切换为受控模式（workspace-write）。")
        db.execute("UPDATE tasks SET yolo=0,permission_profile=?,updated_at=? WHERE id=?", (args[0], now(), task["id"]))
        return result_message(command, f"权限 profile 已设置为 `{args[0]}`；下一条 turn 生效。")

    if command == "plan":
        enabled = not (task.get("collaboration_mode") == "plan") if not args else args[0].lower() in {"on", "true", "1", "plan"}
        mode = "plan" if enabled else "default"
        db.execute("UPDATE tasks SET collaboration_mode=?,updated_at=? WHERE id=?", (mode, now(), task["id"]))
        return result_message(command, f"Plan mode 已{'开启' if enabled else '关闭'}；下一条 turn 使用 `{mode}`。")

    if command == "personality":
        value = (args[0].lower() if args else (task.get("personality") or "none"))
        if not args:
            return result_message(command, f"当前沟通风格：`{value}`。可选：none、friendly、pragmatic。")
        if value not in {"none", "friendly", "pragmatic"}:
            raise HTTPException(400, "personality 只能是 none、friendly 或 pragmatic")
        db.execute("UPDATE tasks SET personality=?,updated_at=? WHERE id=?", (value, now(), task["id"]))
        return result_message(command, f"沟通风格已设置为 `{value}`；下一条 turn 生效。")

    if command in {"memories"}:
        if not args or args[0].lower() == "status":
            return result_message(command, f"当前线程记忆：`{task.get('memory_mode', 'enabled')}`。", ui_action="open_panel", panel="memories")
        if args[0].lower() not in {"on", "off", "enable", "disable"}:
            raise HTTPException(400, "用法：/memories [on|off|status]")
        operation = "memory-enable" if args[0].lower() in {"on", "enable"} else "memory-disable"
        result = await run_thread_operation(task, OperationIn(operation=operation)) if latest_codex_session(task) else {"memory_mode": "enabled" if operation.endswith("enable") else "disabled"}
        if not latest_codex_session(task):
            db.execute("UPDATE tasks SET memory_mode=?,updated_at=? WHERE id=?", (result["memory_mode"], now(), task["id"]))
        return result_message(command, f"线程记忆已{'启用' if operation.endswith('enable') else '停用'}。", **result)

    if command == "skills":
        client, _ = await appserver_for_task(task)
        try:
            result = await client.request("skills/list", {"cwds": [task["workspace"]], "forceReload": bool(args and args[0] == "reload")})
            skills = result.get("data") or []
            text = "**Codex Skills**\n" + ("\n".join(f"- `{s.get('name', '')}` · {s.get('description') or ''}" for s in skills) or "没有发现 Codex Skill。")
        except Exception as exc:
            text = f"Codex Skills 查询失败：{exc}"
            skills = []
        return result_message(command, text, skills=skills, ui_action="open_panel", panel="skills")

    if command == "hooks":
        client, _ = await appserver_for_task(task)
        result = await client.request("hooks/list", {"cwds": [task["workspace"]]})
        entries = result.get("data") or []
        hooks = [hook for entry in entries for hook in entry.get("hooks", [])]
        return result_message(command, "**Hooks**\n" + ("\n".join(f"- `{h.get('eventName')}` · {h.get('command') or h.get('handlerType', '')} · {'启用' if h.get('enabled') else '停用'}" for h in hooks) or "当前工作区没有 Hooks。"), hooks=hooks)

    if command == "import":
        client, _ = await appserver_for_task(task)
        result = await client.request("externalAgentConfig/detect", {"cwds": [task["workspace"]], "includeHome": True, "maxSessions": 50})
        items = result.get("items") or []
        return result_message(command, "**可导入项目**\n" + ("\n".join(f"- `{item.get('itemType')}` · {item.get('description', '')}" for item in items) or "没有检测到可导入的外部 Agent 配置。"), items=items)

    if command == "review":
        instructions = arg_text or "Review the current uncommitted changes. Report concrete bugs, regressions, and missing tests first."
        body = f"请对当前工作区做代码审查。{instructions}\n优先报告问题，最后给出简短总结。"
        message = await enqueue_task_message(task["id"], body)
        return result_message(command, "代码审查已加入当前线程队列。", message=message)

    if command == "rename":
        name = arg_text.strip()
        if not name:
            raise HTTPException(400, "用法：/rename <name>")
        if task_is_running(task):
            raise HTTPException(409, "线程运行中不能重命名")
        result = await run_thread_operation(task, OperationIn(operation="rename", prompt=name)) if latest_codex_session(task) else {}
        db.execute("UPDATE tasks SET name=?,updated_at=? WHERE id=?", (name[:160], now(), task["id"]))
        return result_message(command, f"会话已重命名为 **{name[:160]}**。", **result)

    if command in {"archive", "unarchive", "delete"}:
        if command in {"archive", "delete"} and not confirmed:
            return result_message(command, f"`/{command}` 是破坏性操作，请再次发送并确认。", requires_confirmation=True)
        if task_is_running(task):
            raise HTTPException(409, "线程运行中不能执行该操作")
        if not latest_codex_session(task):
            if command == "delete":
                db.execute("UPDATE tasks SET trashed=1,trashed_at=?,status='trashed',updated_at=? WHERE id=?", (now(), now(), task["id"]))
                await broadcast_overview_removed(task["id"])
            else:
                archived = command == "archive"
                db.execute("UPDATE tasks SET archived=?,status=?,updated_at=? WHERE id=?", (int(archived), "archived" if archived else "available", now(), task["id"]))
                await broadcast_task(task["id"], {"type": "task_status", "task": task_or_404(task["id"]), "source": {"kind": "thread", "operation": command}})
            return result_message(command, f"会话已{'移到回收站' if command == 'delete' else ('归档' if command == 'archive' else '取消归档')}。", trashed=command == "delete", ui_action="clear_selection" if command == "delete" else "refresh")
        result = await run_thread_operation(task, OperationIn(operation=command))
        return result_message(command, f"会话已{'移到回收站' if command == 'delete' else ('归档' if command == 'archive' else '取消归档')}。", **result, ui_action="clear_selection" if command == "delete" else "refresh")

    if command in {"fork", "side"}:
        if task_is_running(task):
            raise HTTPException(409, "线程运行中不能 fork")
        if not latest_codex_session(task):
            raise HTTPException(409, "当前会话还没有 Codex thread")
        result = await run_thread_operation(task, OperationIn(operation="fork"))
        fork_task = result.get("task")
        if command == "side" and arg_text and fork_task:
            await enqueue_task_message(fork_task["id"], arg_text)
        return result_message(command, f"已创建{'侧边' if command == 'side' else '派生'}会话。", **result, ui_action="select_task", task_id=(fork_task or {}).get("id"))

    if command == "new" or command == "clear":
        prompt = arg_text if command == "new" else ""
        if not prompt:
            created = create_command_task(task, "", "available")
            await broadcast_overview(created["id"], {"type": "created", "source": f"/{command}"})
            return result_message(command, "已新建默认 YOLO 会话。", ui_action="select_task", task_id=created["id"], task=created)
        created = create_command_task(task, prompt)
        await launch(created["id"], "start")
        return result_message(command, "新会话已创建并以 YOLO 模式开始运行。", ui_action="select_task", task_id=created["id"], task=task_or_404(created["id"]))

    if command == "resume":
        if task_is_running(task):
            return result_message(command, "当前线程已经由 dashboard 共享连接托管，所有浏览器标签页都可以继续交互。", shared=True)
        if arg_text:
            message = await enqueue_task_message(task["id"], arg_text)
            return result_message(command, "继续消息已加入队列。", message=message)
        return result_message(command, "当前网页已经连接到这个线程；直接发送下一条消息即可继续。", ui_action="focus_composer")

    if command == "init":
        message = await enqueue_task_message(task["id"], "请在当前工作区创建或更新 AGENTS.md，写入适用于本项目的开发、测试和安全指令。完成后说明文件路径和内容摘要。")
        return result_message(command, "AGENTS.md 初始化请求已加入当前线程队列。", message=message)

    if command == "compact":
        if not latest_codex_session(task):
            return result_message(command, "当前线程还没有 Codex thread，不需要压缩。")
        if task_is_running(task):
            raise HTTPException(409, "线程运行中不能手动压缩")
        result = await run_thread_operation(task, OperationIn(operation="compact"))
        return result_message(command, "上下文压缩已启动。", **result)

    if command in {"agent", "subagents"}:
        if not latest_codex_session(task):
            return result_message(command, "当前线程还没有 Codex thread。")
        client, provider = await appserver_for_task(task)
        try:
            await ensure_thread_loaded(task, client, provider)
        except RuntimeError as exc:
            return result_message(command, f"当前 thread 尚未加载：{exc}")
        result = await client.request("thread/items/list", {"threadId": latest_codex_session(task), "limit": 200, "sortDirection": "desc"})
        items = [item for item in result.get("data", []) if any(key in json.dumps(item, ensure_ascii=False).lower() for key in ("agent", "collab", "subagent"))]
        return result_message(command, "**Agent 活动**\n" + ("\n".join(f"- `{item.get('type', 'item')}` · {str(item.get('text') or item.get('name') or '')[:180]}" for item in items) or "当前线程没有可切换的 Agent 活动。"), items=items)

    if command == "diff":
        return result_message(command, git_diff_summary(task))

    if command == "mention":
        if not arg_text:
            return result_message(command, "输入 `/mention path/to/file` 选择工作区文件。", ui_action="open_workspace")
        if task.get("ssh_host"):
            preview = await remote_fs_json(task, "browse", arg_text)
            if preview.get("entry", {}).get("kind") != "file":
                raise HTTPException(404, "工作区文件不存在")
            if int(preview["entry"].get("size") or 0) > 256_000:
                raise HTTPException(413, "文件过大，不能作为 composer 附件")
            content = preview.get("content", "")
            relative = preview["entry"]["path"]
        else:
            root, candidate = workspace_path(task, arg_text)
            if not candidate.exists() or not candidate.is_file():
                raise HTTPException(404, "工作区文件不存在")
            if workspace_hidden(candidate):
                raise HTTPException(403, "敏感文件不能加入浏览器上下文")
            if candidate.stat().st_size > 256_000:
                raise HTTPException(413, "文件过大，不能作为 composer 附件")
            content = candidate.read_text(encoding="utf-8", errors="replace")
            relative = candidate.relative_to(root).as_posix()
        return result_message(command, f"已将 `{relative}` 加入下一条消息。", ui_action="attach_text", attachment={"name": relative, "content": content})

    if command == "mcp":
        client, provider = await appserver_for_task(task)
        if latest_codex_session(task):
            try:
                await ensure_thread_loaded(task, client, provider)
            except RuntimeError:
                pass
        try:
            result = await client.request("mcpServerStatus/list", {"threadId": latest_codex_session(task) or None, "detail": "full", "limit": 100})
        except RuntimeError:
            # A native thread imported from another app-server process may not
            # be loaded in this shared client yet; MCP inventory is global.
            result = await client.request("mcpServerStatus/list", {"threadId": None, "detail": "full", "limit": 100})
        servers = result.get("data") or []
        return result_message(command, "**MCP servers**\n" + ("\n".join(f"- `{server.get('name', server.get('id', 'server'))}` · {server.get('status', 'unknown')}" for server in servers) or "没有配置 MCP server。"), servers=servers)

    if command == "plugins":
        # Do not invoke the remote catalog from a browser request: the native
        # plugin/list RPC may start an unauthenticated background git fetch.
        plugin_root = CODEX_HOME / "plugins"
        local_plugins = [{"name": path.name, "path": str(path)} for path in sorted(plugin_root.iterdir()) if path.is_dir()] if plugin_root.is_dir() else []
        return result_message(command, "**本机 Codex 插件**\n" + ("\n".join(f"- `{item['name']}`" for item in local_plugins) or "没有发现本机插件。远程插件目录需要在 Codex 内完成登录后再打开。"), marketplaces=[{"name": "local", "plugins": local_plugins}])

    if command == "logout":
        if not confirmed:
            return result_message(command, "退出 Codex 会清除当前本机认证，请再次发送并确认。", requires_confirmation=True)
        client, _ = await appserver_for_task(task)
        result = await client.request("account/logout", None)
        return result_message(command, "Codex 已退出登录。", result=result)

    if command == "ps":
        if not latest_codex_session(task):
            return result_message(command, "当前线程还没有 Codex thread。")
        client, provider = await appserver_for_task(task)
        try:
            await ensure_thread_loaded(task, client, provider)
        except RuntimeError as exc:
            return result_message(command, f"当前 Codex app-server 尚未加载该 thread，无法读取后台终端：{exc}")
        try:
            result = await client.request("thread/backgroundTerminals/list", {"threadId": latest_codex_session(task), "limit": 100})
        except RuntimeError as exc:
            return result_message(command, f"当前 Codex app-server 尚未加载该 thread，无法读取后台终端：{exc}")
        terminals = result.get("data") or []
        return result_message(command, "**后台终端**\n" + ("\n".join(f"- `{item.get('processId')}` · `{item.get('command', '')}`" for item in terminals) or "没有后台终端。"), terminals=terminals)

    if command == "stop":
        if task_is_running(task):
            return result_message(command, "正在停止当前 Codex turn。", stop=await stop_task_run(task["id"]))
        if latest_codex_session(task):
            client, _ = await appserver_for_task(task)
            result = await client.request("thread/backgroundTerminals/clean", {"threadId": latest_codex_session(task)})
            return result_message(command, "后台终端已停止。", result=result)
        return result_message(command, "当前没有运行中的 turn 或后台终端。")

    if command == "experimental":
        client, _ = await appserver_for_task(task)
        if not args:
            result = await client.request("experimentalFeature/list", {"limit": 200})
            features = result.get("data") or []
            return result_message(command, "**实验功能**\n" + "\n".join(f"- `{f.get('name')}` · {'on' if f.get('enabled') else 'off'} · {f.get('stage', '')}" for f in features), features=features)
        if len(args) < 2 or args[1].lower() not in {"on", "off", "true", "false"}:
            raise HTTPException(400, "用法：/experimental <feature> <on|off>")
        enabled = args[1].lower() in {"on", "true"}
        result = await client.request("experimentalFeature/enablement/set", {"enablement": {args[0]: enabled}})
        return result_message(command, f"实验功能 `{args[0]}` 已{'开启' if enabled else '关闭'}。", result=result)

    if command == "approve":
        return result_message(command, "当前线程没有待处理的自动审查拒绝；如果 Codex 发出 Guardian 请求，请在该活动卡片中批准后重试。")

    if command in {"ide", "statusline"}:
        return result_message(command, "已打开浏览器工作区检查器。", ui_action="open_inspector")
    if command == "copy":
        return result_message(command, "已请求复制最近一条 Codex 回复。", ui_action="copy_last")
    if command == "raw":
        return result_message(command, "已切换原始活动显示。", ui_action="toggle_raw", value=(args[0].lower() if args else "toggle"))
    if command == "theme":
        theme = args[0].lower() if args else "toggle"
        if theme not in {"dark", "light", "toggle"}:
            raise HTTPException(400, "theme 只能是 dark、light 或 toggle")
        return result_message(command, f"浏览器主题：`{theme}`。", ui_action="theme", value=theme)
    if command == "title":
        value = args[0].lower() if args else "toggle"
        return result_message(command, "已切换浏览器标题。", ui_action="title", value=value)
    if command == "keymap":
        return result_message(command, "**浏览器快捷键**\n- `Enter` 发送；Codex 运行时插入当前 turn\n- `Alt+Enter` 将消息排到下一轮\n- `Shift+Enter` 换行\n- 空输入框时 `↑/↓` 浏览历史输入\n- `Tab` 补全命令\n- `Esc` 取消队列编辑或停止当前运行\n- `Ctrl/Cmd+K` 聚焦 composer\n- `Ctrl/Cmd+N` 新建 YOLO 会话。", ui_action="show_keymap")
    if command == "vim":
        return result_message(command, "浏览器 composer 支持普通输入；Vim 标记已切换，但不会改变浏览器原生编辑行为。", ui_action="vim", value=(args[0].lower() if args else "toggle"))
    if command == "feedback":
        return result_message(command, "浏览器不会自动上传反馈或源码；请通过项目 issue/反馈渠道提交。")
    if command == "exit":
        return result_message(command, "已断开当前网页的实时连接；线程仍由 dashboard 保持，可在其他设备继续交互。", ui_action="disconnect")

    raise HTTPException(400, f"浏览器暂不支持 /{command}")


@app.post("/api/tasks/{task_id}/commands")
async def post_slash_command(task_id: str, payload: SlashCommandIn, _: Any = Depends(auth)):
    task = task_or_404(task_id)
    if payload.client_message_id:
        existing = db.all(
            "SELECT payload FROM events e JOIN sessions s ON s.id=e.session_id WHERE s.task_id=? AND e.stream='system' ORDER BY e.id DESC LIMIT 200",
            (task_id,),
        )
        for row in existing:
            try:
                previous = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            if previous.get("type") == "commandResult" and previous.get("client_message_id") == payload.client_message_id:
                return {"ok": bool(previous.get("ok", True)), "command": previous.get("command", ""), "message": previous.get("text", ""), "client_message_id": payload.client_message_id}
    try:
        command, arg_text, args = parse_slash_command(payload.command)
    except HTTPException as exc:
        await record_command_event(task, "slashCommand", payload.command.strip(), payload.command.strip(), False, payload.client_message_id)
        await record_command_event(task, "commandResult", str(exc.detail), payload.command.strip(), False, payload.client_message_id)
        return {"ok": False, "command": payload.command.strip(), "message": str(exc.detail)}
    await record_command_event(task, "slashCommand", payload.command.strip(), payload.command.strip(), True, payload.client_message_id)
    try:
        result = await execute_slash_command(task, command, arg_text, args, payload.confirmed)
    except HTTPException as exc:
        message = str(exc.detail)
        if db.one("SELECT id FROM tasks WHERE id=?", (task_id,)):
            await record_command_event(task, "commandResult", message, payload.command.strip(), False, payload.client_message_id)
        return {"ok": False, "command": command, "message": message}
    except Exception as exc:
        message = f"/{command} 执行失败：{exc}"
        if db.one("SELECT id FROM tasks WHERE id=?", (task_id,)):
            await record_command_event(task, "commandResult", message, payload.command.strip(), False, payload.client_message_id)
        return {"ok": False, "command": command, "message": message}
    if db.one("SELECT id FROM tasks WHERE id=?", (task_id,)):
        await record_command_event(task, "commandResult", result.get("message", "命令已完成"), payload.command.strip(), bool(result.get("ok", True)), payload.client_message_id)
    return result


@app.post("/api/tasks/{task_id}/operation")
async def codex_operation(task_id: str, payload: OperationIn, _: Any = Depends(auth)):
    task = task_or_404(task_id)
    if payload.operation not in CODEX_OPERATIONS:
        raise HTTPException(400, f"Unsupported Codex operation: {payload.operation}")
    # Moving a task to the recycle bin is a dashboard operation. It must work
    # even when Codex is unavailable or the task never created a native thread.
    if payload.operation == "delete":
        if task_id in running or task_id in appserver_turn_tasks or task["status"] in {"running", "retrying", "queued"}:
            await stop_task_run(task_id)
            task = task_or_404(task_id)
        return await run_thread_operation(task, payload)
    if payload.operation in {"memory-enable", "memory-disable"}:
        return await run_thread_operation(task, payload)
    if task_id in running or task_id in appserver_turn_tasks or task["status"] == "running":
        raise HTTPException(409, "Task is already running")
    if task.get("ssh_host"):
        await require_ssh_connection(task["ssh_host"], codex=True)
    else:
        require_codex()
    if (USE_APP_SERVER or task.get("ssh_host")) and payload.operation in THREAD_RPC_OPERATIONS:
        return await run_thread_operation(task, payload)
    # Global workspace and safety flags are placed before the subcommand.
    if (USE_APP_SERVER or task.get("ssh_host")) and payload.operation in {"exec", "resume"}:
        provider = next(iter(provider_rows()), None)
        db.execute(
            "UPDATE tasks SET status='running',retry_count=retry_count+1,execution_source='dashboard',execution_turn_id='',run_mode='operation',updated_at=? WHERE id=?",
            (now(), task_id),
        )
        return await launch_appserver(task, provider, "resume" if payload.operation == "resume" else "start", payload.prompt or "", "")
    cmd = [CODEX_BIN, "-C", task["workspace"]]
    if task["yolo"]:
        cmd += ["--ask-for-approval", "never", "--dangerously-bypass-approvals-and-sandbox"]
    cmd += [payload.operation, *payload.args]
    if payload.prompt:
        cmd.append(payload.prompt)
    session_id, stamp = str(uuid.uuid4()), now()
    attempt = int(task["retry_count"]) + 1
    db.execute("INSERT INTO sessions (id,task_id,status,attempt,command,started_at) VALUES (?,?,?,?,?,?)", (session_id, task_id, "running", attempt, shlex.join(cmd), stamp))
    db.execute(
        "UPDATE tasks SET status='running',active_session_id=?,execution_source='dashboard',execution_turn_id='',run_mode='operation',updated_at=? WHERE id=?",
        (session_id, stamp, task_id),
    )
    asyncio.create_task(supervise_operation(task, session_id, cmd))
    return {"session_id": session_id, "command": shlex.join(cmd)}


async def supervise_operation(task: dict, session_id: str, cmd: list[str]) -> None:
    task_id = task["id"]
    try:
        spawn_cmd = cmd
        cwd: Optional[str] = task["workspace"]
        if task.get("ssh_host"):
            connection = await require_ssh_connection(task["ssh_host"], codex=True)
            spawn_cmd = [*ssh_options(task["ssh_host"], batch=True), ssh_destination(task["ssh_host"]), shlex.join([connection["codex_bin"], *cmd[1:]])]
            cwd = None
        process = await asyncio.create_subprocess_exec(*spawn_cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=os.environ.copy())
        async with running_lock:
            running[task_id] = process
        assert process.stdout
        async for raw in process.stdout:
            line = raw.decode(errors="replace").rstrip("\n")
            db.execute("INSERT INTO events (session_id,ts,stream,payload) VALUES (?,?,?,?)", (session_id, now(), "stdout", json.dumps(safe_json(line), ensure_ascii=False)))
        code = await process.wait()
        db.execute("UPDATE sessions SET status=?, finished_at=?, exit_code=?, summary=? WHERE id=?", ("succeeded" if code == 0 else "failed", now(), code, "Codex operation completed" if code == 0 else f"exit code {code}", session_id))
        db.execute(
            "UPDATE tasks SET status=?,execution_source='',execution_turn_id='',last_error=?,updated_at=? WHERE id=?",
            ("succeeded" if code == 0 else "failed", "" if code == 0 else f"exit code {code}", now(), task_id),
        )
    except Exception as exc:
        db.execute("UPDATE sessions SET status='failed', finished_at=?, exit_code=?, summary=? WHERE id=?", (now(), 1, str(exc), session_id))
        db.execute(
            "UPDATE tasks SET status='failed',execution_source='',execution_turn_id='',last_error=?,updated_at=? WHERE id=?",
            (str(exc), now(), task_id),
        )
    finally:
        async with running_lock:
            running.pop(task_id, None)
        await broadcast_overview(task_id, {"type": "operation"})
        schedule_task_drain(task_id)


@app.get("/api/skills")
async def list_skills(_: Any = Depends(auth)):
    rows = db.all("SELECT * FROM skills ORDER BY name")
    for r in rows:
        r["enabled"] = bool(r["enabled"])
        r.update({"installed": False, "source": "Dashboard", "editable": True, "deletable": True, "path": ""})
    return installed_skill_rows() + rows


@app.post("/api/skills")
async def create_skill(payload: SkillIn, _: Any = Depends(auth)):
    name = skill_slug(payload.name)
    if any(row["name"] == name for row in installed_skill_rows()):
        raise HTTPException(409, "An installed Skill already uses this name")
    path = (CODEX_HOME / "skills" / name / "SKILL.md").resolve()
    root = (CODEX_HOME / "skills").resolve()
    if root not in path.parents:
        raise HTTPException(400, "Invalid Skill path")
    if path.exists():
        raise HTTPException(409, "Skill already exists")
    write_skill_document(path, name, payload.description, payload.content)
    return next(row for row in installed_skill_rows() if row["path"] == str(path))


@app.put("/api/skills/{skill_id}")
async def update_skill(skill_id: str, payload: SkillIn, _: Any = Depends(auth)):
    legacy = db.one("SELECT id FROM skills WHERE id=?", (skill_id,))
    if legacy:
        db.execute("UPDATE skills SET name=?,description=?,content=?,enabled=?,updated_at=? WHERE id=?", (payload.name, payload.description, payload.content, int(payload.enabled), now(), skill_id))
        return db.one("SELECT * FROM skills WHERE id=?", (skill_id,))
    row = installed_skill_or_404(skill_id)
    if not row["editable"]:
        raise HTTPException(403, "This installed Skill is read-only")
    name = skill_slug(payload.name)
    path = Path(row["path"]).resolve()
    directory = path.parent
    destination = directory.parent / name
    renamed = destination != directory
    if renamed and destination.exists():
        raise HTTPException(409, "Another installed Skill already uses this name")
    if renamed:
        directory.rename(destination)
        path = destination / "SKILL.md"
    try:
        write_skill_document(path, name, payload.description, payload.content, preserve=True)
    except Exception:
        if renamed and destination.exists() and not directory.exists():
            destination.rename(directory)
        raise
    return next(row for row in installed_skill_rows() if row["path"] == str(path))


@app.delete("/api/skills/{skill_id}")
async def delete_skill(skill_id: str, _: Any = Depends(auth)):
    if db.one("SELECT id FROM skills WHERE id=?", (skill_id,)):
        db.execute("DELETE FROM skills WHERE id=?", (skill_id,))
        return {"ok": True}
    row = installed_skill_or_404(skill_id)
    if not row.get("deletable"):
        raise HTTPException(403, "This installed Skill cannot be deleted from the dashboard")
    root = Path(row["root"]).resolve()
    directory = Path(row["path"]).resolve().parent
    if directory == root or root not in directory.parents:
        raise HTTPException(400, "Invalid installed Skill directory")
    shutil.rmtree(directory)
    return {"ok": True}


@app.get("/api/providers")
async def list_providers(_: Any = Depends(auth)):
    return provider_public_rows()


@app.post("/api/providers/check-all")
async def check_all_providers(_: Any = Depends(auth)):
    providers = db.all("SELECT * FROM providers ORDER BY priority,name")
    results = await asyncio.gather(*(asyncio.to_thread(provider_probe, provider) for provider in providers))
    for provider, result in zip(providers, results):
        store_provider_probe(provider["id"], result)
    return provider_public_rows()


@app.post("/api/providers/{provider_id}/check")
async def check_provider(provider_id: str, _: Any = Depends(auth)):
    provider = db.one("SELECT * FROM providers WHERE id=?", (provider_id,))
    if not provider:
        raise HTTPException(404, "Provider not found")
    result = await asyncio.to_thread(provider_probe, provider)
    store_provider_probe(provider_id, result)
    return next(row for row in provider_public_rows() if row["id"] == provider_id)


@app.post("/api/providers/sync-native")
async def sync_providers(_: Any = Depends(auth)):
    result = sync_native_providers()
    for row in db.all("SELECT id FROM providers WHERE native=1"):
        await invalidate_appserver(row["id"])
    await invalidate_appserver("default")
    return result


@app.post("/api/providers/verify")
async def verify_provider(payload: ProviderVerifyIn, _: Any = Depends(auth)):
    """Probe an unsaved provider and return its selectable models."""
    base_url = normalize_provider_url(payload.base_url)
    result = await asyncio.to_thread(
        provider_probe,
        {"base_url": base_url, "api_key": payload.api_key.get_secret_value() if payload.api_key else ""},
    )
    ok = result["status"] in {"healthy", "reachable", "configured"}
    return {"ok": ok, "health_status": result["status"], "detail": result["detail"], "latency_ms": result["latency_ms"], "models": result.get("models", [])}


@app.post("/api/providers")
async def create_provider(payload: ProviderIn, _: Any = Depends(auth)):
    duplicate = provider_duplicate(payload.base_url)
    if duplicate:
        raise HTTPException(409, f"Provider 连接已存在：{duplicate.get('name') or duplicate.get('base_url')}")
    provider_id, stamp = str(uuid.uuid4()), now()
    api_key = payload.api_key.get_secret_value() if payload.api_key else ""
    base_url = normalize_provider_url(payload.base_url)
    name = payload.name.strip() or provider_name_from_url(base_url)
    model_provider = payload.model_provider.strip() or provider_model_provider_from_url(base_url)
    db.execute(
        "INSERT INTO providers (id,name,kind,model,profile,api_key,base_url,enabled,priority,created_at,updated_at,model_provider,native) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)",
        (provider_id, name, payload.kind, payload.model, payload.profile, api_key, base_url, int(payload.enabled), payload.priority, stamp, stamp, model_provider),
    )
    provider = db.one("SELECT * FROM providers WHERE id=?", (provider_id,))
    result = await asyncio.to_thread(provider_probe, provider)
    if result["status"] not in {"healthy", "reachable", "configured"}:
        db.execute("DELETE FROM providers WHERE id=?", (provider_id,))
        raise HTTPException(400, f"Provider 验证失败：{result['detail']}")
    store_provider_probe(provider_id, result)
    return next(row for row in provider_public_rows() if row["id"] == provider_id)


@app.put("/api/providers/{provider_id}")
async def update_provider(provider_id: str, payload: ProviderIn, _: Any = Depends(auth)):
    current = db.one("SELECT * FROM providers WHERE id=?", (provider_id,))
    if not current: raise HTTPException(404, "Provider not found")
    duplicate = provider_duplicate(payload.base_url, exclude_id=provider_id)
    if duplicate:
        raise HTTPException(409, f"Provider 连接已存在：{duplicate.get('name') or duplicate.get('base_url')}")
    await invalidate_appserver(provider_id)
    supplied_key = payload.api_key.get_secret_value() if payload.api_key else ""
    api_key = "" if payload.clear_api_key else (supplied_key or current.get("api_key") or "")
    base_url = normalize_provider_url(payload.base_url)
    name = payload.name.strip() or provider_name_from_url(base_url)
    model_provider = payload.model_provider.strip() or current.get("model_provider") or provider_model_provider_from_url(base_url)
    db.execute("UPDATE providers SET name=?,kind=?,model=?,model_provider=?,profile=?,api_key=?,base_url=?,enabled=?,priority=?,health_status='unchecked',health_detail='',health_checked_at=NULL,health_latency_ms=NULL,updated_at=? WHERE id=?", (name, payload.kind, payload.model, model_provider, payload.profile, api_key, base_url, int(payload.enabled), payload.priority, now(), provider_id))
    provider = db.one("SELECT * FROM providers WHERE id=?", (provider_id,))
    result = await asyncio.to_thread(provider_probe, provider)
    store_provider_probe(provider_id, result)
    return next(row for row in provider_public_rows() if row["id"] == provider_id)


@app.delete("/api/providers/{provider_id}")
async def delete_provider(provider_id: str, _: Any = Depends(auth)):
    await invalidate_appserver(provider_id)
    db.execute("DELETE FROM providers WHERE id=?", (provider_id,)); return {"ok": True}


@app.get("/{path:path}")
async def static_files(path: str):
    candidate = (STATIC / path).resolve()
    if candidate.is_file() and STATIC.resolve() in candidate.parents:
        return FileResponse(candidate, headers={"Cache-Control": "no-store"})
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=DASHBOARD_HOST, port=DASHBOARD_PORT)
