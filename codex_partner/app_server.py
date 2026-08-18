"""Shared JSON-RPC transport for one Codex app-server process."""

import asyncio
import json
from typing import Any, Awaitable, Callable, Optional

from .platform_support import prepare_subprocess_command


NotificationHandler = Callable[[str, dict], Awaitable[None]]


class AppServerClient:
    """Own one app-server process and fan out protocol messages to the runtime."""

    def __init__(
        self,
        env: dict[str, str],
        key: str,
        *,
        command: Optional[list[str]] = None,
        local: bool = True,
        require_codex: Optional[Callable[[], None]] = None,
        notification_handler: Optional[NotificationHandler] = None,
        server_request_handler: Optional[NotificationHandler] = None,
        thread_bindings: Optional[dict[str, tuple[str, str, str]]] = None,
        turn_waiters: Optional[dict[str, asyncio.Future]] = None,
        client_name: str = "codex-partner",
        client_version: str = "0.0.0",
    ):
        self.env, self.key = env, key
        self.command = command or ["codex", "app-server", "--stdio"]
        self.local = local
        self.require_codex = require_codex
        self.notification_handler = notification_handler
        self.server_request_handler = server_request_handler
        # Preserve the runtime's empty-but-shared mappings; ``or {}`` would
        # silently replace them and break app-server exit recovery.
        self.thread_bindings = thread_bindings if thread_bindings is not None else {}
        self.turn_waiters = turn_waiters if turn_waiters is not None else {}
        self.client_name = client_name
        self.client_version = client_version
        self.process: Optional[asyncio.subprocess.Process] = None
        self.reader_task: Optional[asyncio.Task] = None
        self.write_lock = asyncio.Lock()
        self.pending: dict[int, asyncio.Future] = {}
        self.next_id = 1

    async def start(self) -> None:
        if self.local and self.require_codex:
            self.require_codex()
        if self.process and self.process.returncode is None and self.reader_task and not self.reader_task.done():
            return
        if self.process and self.process.returncode is None:
            try:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), 1)
            except Exception:
                pass
        command = prepare_subprocess_command(self.command)
        self.process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=self.env,
            limit=64 * 1024 * 1024,
        )
        self.reader_task = asyncio.create_task(self._reader())
        await self.request(
            "initialize",
            {
                "clientInfo": {"name": self.client_name, "version": self.client_version},
                "capabilities": {"experimentalApi": True},
            },
        )
        await self.notify("initialized")

    async def _reader(self) -> None:
        assert self.process and self.process.stdout
        try:
            async for raw in self._message_lines():
                try:
                    message = json.loads(raw.decode(errors="replace"))
                except json.JSONDecodeError:
                    continue
                request_id = message.get("id")
                if request_id is not None and request_id in self.pending:
                    future = self.pending.pop(request_id)
                    if not future.done():
                        if "error" in message:
                            future.set_exception(RuntimeError(json.dumps(message["error"], ensure_ascii=False)))
                        else:
                            future.set_result(message.get("result"))
                elif self.notification_handler:
                    try:
                        if "id" in message and "method" in message and "params" in message:
                            if self.server_request_handler:
                                await self.server_request_handler(self.key, message)
                        else:
                            await self.notification_handler(self.key, message)
                    except Exception:
                        # One malformed notification must not strand every browser task.
                        continue
        except asyncio.CancelledError:
            return
        finally:
            error = RuntimeError("Codex app-server exited")
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(error)
            self.pending.clear()
            for task_id, (key, thread_id, _session_id) in list(self.thread_bindings.items()):
                if key != self.key:
                    continue
                waiter = self.turn_waiters.pop(thread_id, None)
                if waiter and not waiter.done():
                    waiter.set_exception(error)

    async def _message_lines(self):
        """Read newline-delimited RPC without StreamReader's line-size ceiling."""
        assert self.process and self.process.stdout
        buffered = bytearray()
        while chunk := await self.process.stdout.read(1024 * 1024):
            buffered.extend(chunk)
            while (newline := buffered.find(b"\n")) >= 0:
                raw = bytes(buffered[:newline]).rstrip(b"\r")
                del buffered[: newline + 1]
                if raw:
                    yield raw
        raw = bytes(buffered).strip()
        if raw:
            yield raw

    async def notify(self, method: str, params: Any = None) -> None:
        message = {"method": method}
        if params is not None:
            message["params"] = params
        async with self.write_lock:
            if not self.process or not self.process.stdin:
                raise RuntimeError("Codex app-server is not running")
            self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
            await self.process.stdin.drain()

    async def request(self, method: str, params: Any) -> Any:
        async with self.write_lock:
            if not self.process or not self.process.stdin:
                raise RuntimeError("Codex app-server is not running")
            request_id = self.next_id
            self.next_id += 1
            future = asyncio.get_running_loop().create_future()
            self.pending[request_id] = future
            self.process.stdin.write((json.dumps({"id": request_id, "method": method, "params": params}, ensure_ascii=False) + "\n").encode())
            await self.process.stdin.drain()
        return await asyncio.wait_for(future, timeout=120)

    async def close(self) -> None:
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 2)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.reader_task and not self.reader_task.done():
            self.reader_task.cancel()
        if self.reader_task:
            await asyncio.gather(self.reader_task, return_exceptions=True)
