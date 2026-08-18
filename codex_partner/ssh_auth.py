"""SSH-backed browser authentication for Codex Partner."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

try:
    import pexpect
except ImportError:  # Native Windows uses local-only auth by default.
    pexpect = None


USERNAME_PATTERN = re.compile(r"^[a-z_][a-z0-9_.-]{0,63}$", re.IGNORECASE)


def valid_ssh_username(username: str) -> bool:
    """Reject values that cannot be a plain OpenSSH user name."""

    return bool(USERNAME_PATTERN.fullmatch(username.strip()))


def verify_ssh_password(
    username: str,
    password: str,
    *,
    host: str,
    port: int,
    known_hosts: Path,
    timeout: int = 12,
) -> tuple[bool, str]:
    """Verify credentials with OpenSSH without retaining the password."""

    username = username.strip()
    if not valid_ssh_username(username) or not password:
        return False, "Invalid SSH username or password"
    if pexpect is None:
        return False, "SSH password authentication is unavailable on this platform"

    known_hosts.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    args = [
        "ssh",
        "-F",
        "/dev/null",
        "-o",
        "BatchMode=no",
        "-o",
        "PreferredAuthentications=password,keyboard-interactive",
        "-o",
        "PubkeyAuthentication=no",
        "-o",
        "NumberOfPasswordPrompts=1",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-o",
        f"ConnectTimeout={min(max(timeout, 3), 30)}",
        "-p",
        str(port),
        f"{username}@{host}",
        "true",
    ]
    child = pexpect.spawn(
        args[0],
        args[1:],
        encoding="utf-8",
        timeout=timeout,
        env={**os.environ, "LC_ALL": "C"},
    )
    password_sent = False
    output: list[str] = []
    try:
        while True:
            matched = child.expect(
                [
                    r"(?i)(?:password|passphrase).*:",
                    r"(?i)are you sure you want to continue connecting",
                    pexpect.EOF,
                    pexpect.TIMEOUT,
                ]
            )
            output.append(child.before or "")
            if matched == 0:
                if password_sent:
                    return False, "SSH authentication failed"
                child.sendline(password)
                password_sent = True
                continue
            if matched == 1:
                child.sendline("yes")
                continue
            if matched == 3:
                return False, "SSH authentication timed out"
            break
        child.close()
        if child.exitstatus == 0:
            return True, ""
        detail = " ".join(" ".join(output).split()).lower()
        if "connection refused" in detail or "no route to host" in detail:
            return False, "Local SSH service is unavailable"
        return False, "SSH authentication failed"
    finally:
        if child.isalive():
            child.close(force=True)


class LoginThrottle:
    """Small in-memory limiter for repeated SSH password failures."""

    def __init__(self, attempts: int = 5, window_seconds: int = 300):
        self.attempts = attempts
        self.window_seconds = window_seconds
        self.failures: dict[str, list[float]] = {}

    def allowed(self, key: str) -> bool:
        cutoff = time.monotonic() - self.window_seconds
        recent = [stamp for stamp in self.failures.get(key, []) if stamp >= cutoff]
        self.failures[key] = recent
        return len(recent) < self.attempts

    def fail(self, key: str) -> None:
        self.failures.setdefault(key, []).append(time.monotonic())

    def clear(self, key: str) -> None:
        self.failures.pop(key, None)
