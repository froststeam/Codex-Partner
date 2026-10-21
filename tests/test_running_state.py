import asyncio
import ast
import base64
import importlib
import inspect
import io
import json
import os
import re
import sqlite3
import sys
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from codex_partner.interaction_state import derive_interaction_state, goal_should_run, goal_status_after_failure


class RunningStateTests(unittest.TestCase):
    def test_interaction_state_matrix(self):
        cases = [
            # goal, status, retry, session, action, enabled, retry-enabled, clear, should-run
            ("", "none", False, "available", "set", True, False, False, False),
            ("goal", "paused", False, "available", "start", True, True, True, False),
            ("goal", "active", False, "running", "pause", True, True, False, True),
            ("goal", "active", True, "running", "pause", False, True, False, True),
            ("goal", "blocked", False, "failed", "continue", True, True, True, False),
            ("goal", "blocked", True, "retrying", "recovering", False, False, False, True),
            ("goal", "paused", True, "retrying", "recovering", False, False, False, True),
            ("goal", "complete", False, "available", "restart", True, False, True, False),
            ("goal", "active", False, "queued", "starting", False, False, False, True),
            ("goal", "active", False, "stopped", "start", True, True, True, False),
            ("goal", "active", False, "available", "starting", False, False, False, True),
            ("goal", "blocked", True, "available", "recovering", False, False, False, True),
        ]
        for goal, goal_status, retry, session, action, enabled, retry_enabled, clear, should_run in cases:
            with self.subTest(goal_status=goal_status, retry=retry, session=session):
                task = {
                    "goal": goal, "goal_status": goal_status,
                    "retry_forever": retry, "status": session,
                    "run_mode": "goal_resume" if session == "queued" else "",
                }
                state = derive_interaction_state(task)
                self.assertEqual(action, state["goalAction"])
                self.assertEqual(enabled, state["goalActionEnabled"])
                self.assertEqual(retry_enabled, state["retryEnabled"])
                self.assertEqual(clear, state["goalClearEnabled"])
                self.assertEqual(should_run, goal_should_run(task))

    def test_interaction_state_exhaustive_invariants(self):
        goal_statuses = ("none", "planned", "active", "paused", "blocked", "complete")
        session_statuses = ("available", "running", "queued", "retrying", "stopped", "failed", "succeeded")
        for goal_status in goal_statuses:
            for session_status in session_statuses:
                for retry in (False, True):
                    goal = "" if goal_status == "none" else "goal"
                    task = {"goal": goal, "goal_status": goal_status, "status": session_status, "retry_forever": retry}
                    with self.subTest(goal_status=goal_status, session_status=session_status, retry=retry):
                        state = derive_interaction_state(task)
                        self.assertEqual(session_status in {"running", "queued", "retrying"}, state["sessionBusy"])
                        self.assertEqual("pause" if state["sessionBusy"] else "send", state["composerAction"])
                        if not goal:
                            self.assertEqual("set", state["goalAction"])
                            self.assertFalse(state["retryEnabled"])
                            self.assertFalse(state["goalClearEnabled"])
                        if state["goalStatus"] == "complete":
                            self.assertEqual("restart", state["goalAction"])
                            self.assertFalse(state["retryEnabled"])
                            self.assertFalse(state["goalShouldRun"])
                        if state["goalStatus"] == "active" and state["sessionBusy"]:
                            self.assertIn(state["goalAction"], {"pause", "starting"})
                            self.assertFalse(state["goalClearEnabled"])
                        if state["goalAction"] in {"starting", "recovering", "pausing"}:
                            self.assertFalse(state["goalActionEnabled"])
                            self.assertFalse(state["retryEnabled"])

    def test_composer_action_matrix_is_independent_of_goal_status(self):
        for goal_status in ("none", "paused", "active", "blocked", "complete"):
            goal = "" if goal_status == "none" else "goal"
            for session_status in ("available", "stopped", "failed", "succeeded"):
                state = derive_interaction_state({"goal": goal, "goal_status": goal_status, "status": session_status})
                self.assertEqual("send", state["composerAction"])
            for session_status in ("running", "queued", "retrying"):
                state = derive_interaction_state({"goal": goal, "goal_status": goal_status, "status": session_status})
                self.assertEqual("pause", state["composerAction"])

    def test_startup_drain_includes_active_and_retry_recoverable_goals(self):
        ids = []
        cases = (
            ("startup-active-goal", "active", False, True),
            ("startup-paused-retry", "paused", True, True),
            ("startup-blocked-retry", "blocked", True, True),
            ("startup-paused-off", "paused", False, False),
            ("startup-complete", "complete", True, False),
            ("startup-legacy-stopped-active", "active", False, False),
        )
        try:
            for task_id, status, retry, _expected in cases:
                ids.append(task_id)
                self.make_task(task_id, "stopped" if task_id == "startup-legacy-stopped-active" else "available")
                self.app.db.execute(
                    "UPDATE tasks SET goal='goal',goal_status=?,retry_forever=? WHERE id=?",
                    (status, int(retry), task_id),
                )
            startup = self.app.startup_drain_task_ids()
            for task_id, _status, _retry, expected in cases:
                self.assertEqual(expected, task_id in startup)
        finally:
            for task_id in ids:
                self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_legacy_stopped_active_states_are_reconciled(self):
        user_stop = "legacy-user-stopped-active"
        failed_stop = "legacy-failed-stopped-active"
        complete_retry = "legacy-complete-retry"
        terminal_binding = "legacy-terminal-binding"
        for task_id in (user_stop, failed_stop, complete_retry, terminal_binding):
            self.make_task(task_id, "stopped")
        self.app.db.execute("UPDATE tasks SET goal='goal',goal_status='active',retry_forever=1 WHERE id=?", (user_stop,))
        self.app.db.execute(
            "UPDATE tasks SET goal='goal',goal_status='active',retry_forever=0,last_error='Codex process ended without a completion event' WHERE id=?",
            (failed_stop,),
        )
        self.app.db.execute("UPDATE tasks SET goal='goal',goal_status='complete',retry_forever=1 WHERE id=?", (complete_retry,))
        self.app.db.execute(
            "UPDATE tasks SET status='available',active_session_id='old-session',execution_turn_id='old-turn',run_mode='goal_resume',last_error='Stopped by user' WHERE id=?",
            (terminal_binding,),
        )
        try:
            self.app.reconcile_legacy_interaction_states()
            user = self.app.task_or_404(user_stop)
            failed = self.app.task_or_404(failed_stop)
            complete = self.app.task_or_404(complete_retry)
            normalized = self.app.task_or_404(terminal_binding)
            self.assertEqual(("stopped", "paused", False), (user["status"], user["goal_status"], user["retry_forever"]))
            self.assertEqual(("failed", "blocked"), (failed["status"], failed["goal_status"]))
            self.assertFalse(complete["retry_forever"])
            self.assertIsNone(normalized["active_session_id"])
            self.assertEqual("", normalized["execution_turn_id"])
            self.assertEqual("", normalized["run_mode"])
            self.assertEqual("", normalized["last_error"])
        finally:
            for task_id in (user_stop, failed_stop, complete_retry, terminal_binding):
                self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_active_goal_continues_normally_but_errors_require_auto_retry(self):
        task = {"goal": "goal", "goal_status": "active", "retry_forever": False}
        self.assertTrue(self.app.goal_auto_resume_enabled(task))
        self.assertFalse(self.app.task_retry_allowed(task))
        task["retry_forever"] = True
        self.assertTrue(self.app.task_retry_allowed(task))
        self.assertEqual("blocked", goal_status_after_failure(task))
        task["retry_forever"] = False
        self.assertEqual("blocked", goal_status_after_failure(task))
        self.assertEqual("paused", goal_status_after_failure(task, user_stopped=True))

    def test_complete_and_missing_goal_cannot_enable_auto_retry(self):
        for task_id, goal, goal_status in (
            ("retry-without-goal", "", "none"),
            ("retry-complete-goal", "done", "complete"),
        ):
            self.make_task(task_id, "available")
            self.app.db.execute(
                "UPDATE tasks SET goal=?,goal_status=? WHERE id=?",
                (goal, goal_status, task_id),
            )
            try:
                with self.assertRaises(self.app.HTTPException) as raised:
                    asyncio.run(self.app.patch_task(task_id, self.app.TaskPatch(retry_forever=True), None))
                self.assertEqual(409, raised.exception.status_code)
                self.assertFalse(self.app.task_or_404(task_id)["retry_forever"])
            finally:
                self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_stopped_session_accepts_an_ordinary_message_without_reactivating_goal(self):
        task_id = "stopped-paused-goal-message"
        self.make_task(task_id, "stopped")
        self.app.db.execute(
            "UPDATE tasks SET goal='paused goal',goal_status='paused',retry_forever=0 WHERE id=?",
            (task_id,),
        )
        try:
            with mock.patch.object(self.app, "schedule_task_drain") as schedule:
                message = asyncio.run(self.app.enqueue_task_message(task_id, "ordinary message"))
            task = self.app.task_or_404(task_id)
            self.assertEqual("queued", message["status"])
            self.assertEqual("available", task["status"])
            self.assertEqual("paused", task["goal_status"])
            schedule.assert_called_once_with(task_id)
        finally:
            self.app.db.execute("DELETE FROM task_messages WHERE task_id=?", (task_id,))
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_message_queue_normalizes_a_pre_thread_task_alias(self):
        task_id = "canonical-message-task"
        alias = "temporary-message-alias"
        self.make_task(task_id, "available")
        self.app.task_id_aliases[alias] = task_id
        try:
            with mock.patch.object(self.app, "schedule_task_drain") as schedule:
                message = asyncio.run(self.app.enqueue_task_message(alias, "second message"))
            row = self.app.db.one("SELECT task_id,status FROM task_messages WHERE id=?", (message["id"],))
            self.assertEqual(task_id, row["task_id"])
            self.assertEqual("queued", row["status"])
            schedule.assert_called_once_with(task_id)
        finally:
            self.app.task_id_aliases.pop(alias, None)
            self.app.db.execute("DELETE FROM task_messages WHERE task_id=?", (task_id,))
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_task_payload_exposes_canonical_interaction_controls(self):
        task_id = "interaction-payload"
        self.make_task(task_id, "available")
        self.app.db.execute(
            "UPDATE tasks SET goal='paused goal',goal_status='paused',retry_forever=0 WHERE id=?",
            (task_id,),
        )
        try:
            task = self.app.task_or_404(task_id)
            self.assertEqual("start", task["interaction"]["goalAction"])
            self.assertEqual("send", task["interaction"]["composerAction"])
            self.assertTrue(task["interaction"]["goalClearEnabled"])
        finally:
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_native_history_timeout_returns_overlay_fallback_instead_of_500(self):
        task_id = "native-history-timeout"
        self.make_task(task_id, "available")
        self.app.db.execute(
            "UPDATE tasks SET native=1,codex_session_id=? WHERE id=?",
            (task_id, task_id),
        )

        class SlowClient:
            async def request(self, _method, _params):
                raise asyncio.TimeoutError()

        try:
            with mock.patch.object(self.app, "appserver_for", new=mock.AsyncMock(return_value=SlowClient())):
                rows = asyncio.run(self.app.native_history_events(self.app.task_or_404(task_id)))
            self.assertEqual([], rows)
        finally:
            self.app.native_history_cache.pop(task_id, None)
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        os.environ["CODEX_DASHBOARD_DATA"] = str(root / "data")
        os.environ["CODEX_HOME"] = str(root / "codex-home")
        fake_codex = "fake_codex.cmd" if os.name == "nt" else "fake_codex"
        os.environ["CODEX_BIN"] = str(Path(__file__).parent / fake_codex)
        os.environ["CODEX_DASHBOARD_AUTH"] = "ssh"
        os.environ["CODEX_DASHBOARD_EXTERNAL_TURN_GRACE"] = "1"
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        cls.app = importlib.import_module("app")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def make_task(self, task_id: str, status: str = "running", active_session_id=None):
        stamp = self.app.now()
        self.app.db.execute(
            "INSERT INTO tasks (id,name,prompt,workspace,status,yolo,created_at,updated_at,native,codex_session_id,active_session_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, task_id, "prompt", self.temp.name, status, 1, stamp, stamp, 1, task_id, active_session_id),
        )

    def test_python_sources_parse_with_python_310_grammar(self):
        root = Path(__file__).resolve().parents[1]
        sources = [root / "app.py", *(root / "codex_partner").rglob("*.py"), *(root / "tests").rglob("*.py")]
        for source in sources:
            with self.subTest(source=source.relative_to(root)):
                ast.parse(source.read_text(encoding="utf-8"), filename=str(source), feature_version=(3, 10))

    def test_persistent_appserver_cleanup_preserves_active_units(self):
        from codex_partner import persistent_appserver

        active_key = "provider:task:active"
        active_unit = persistent_appserver.unit_name(active_key)
        stale_unit = persistent_appserver.unit_name("provider:task:stale")
        calls = []

        async def fake_run(*command):
            calls.append(command)
            if "list-units" in command:
                return 0, f"{active_unit} loaded active running active\n{stale_unit} loaded active running stale\n"
            return 0, ""

        with mock.patch.object(persistent_appserver, "supported", return_value=True), \
             mock.patch.object(persistent_appserver, "_run", side_effect=fake_run):
            stopped = asyncio.run(persistent_appserver.stop_inactive_services({active_key}))

        self.assertEqual(1, stopped)
        self.assertIn(("systemctl", "--user", "stop", "--no-block", stale_unit), calls)
        self.assertNotIn(("systemctl", "--user", "stop", "--no-block", active_unit), calls)

    def test_persistent_appserver_requires_a_booted_systemd_user_manager(self):
        from codex_partner import persistent_appserver

        root = Path(self.temp.name)
        booted = root / "run-systemd-system"
        booted.mkdir(exist_ok=True)
        bus = root / "bus"

        with mock.patch.object(persistent_appserver.shutil, "which", return_value="/usr/bin/systemctl"):
            # systemctl/systemd-run exist, but PID 1 is not systemd: unsupported.
            with mock.patch.object(persistent_appserver, "SYSTEMD_BOOT_DIR", root / "missing"):
                self.assertFalse(persistent_appserver.supported())
            # Booted, but no session bus to talk to a user manager: still unsupported.
            with mock.patch.object(persistent_appserver, "SYSTEMD_BOOT_DIR", booted), \
                 mock.patch.object(persistent_appserver, "session_bus_path", return_value=bus):
                self.assertFalse(persistent_appserver.supported())
            # A live bus socket is the final requirement.
            bus.touch()
            with mock.patch.object(persistent_appserver, "SYSTEMD_BOOT_DIR", booted), \
                 mock.patch.object(persistent_appserver, "session_bus_path", return_value=bus):
                self.assertTrue(persistent_appserver.supported())
        with mock.patch.object(persistent_appserver.shutil, "which", return_value=None):
            self.assertFalse(persistent_appserver.supported())

    def test_appserver_startup_error_ignores_turn_failures(self):
        bus_error = ("Unable to start persistent Codex app-server: Failed to connect to bus: "
                     "$DBUS_SESSION_BUS_ADDRESS and $XDG_RUNTIME_DIR not defined")
        self.assertTrue(self.app.appserver_startup_error(bus_error))
        self.assertTrue(self.app.appserver_startup_error("Persistent Codex app-server did not open its control endpoint"))
        self.assertFalse(self.app.appserver_startup_error("stream disconnected before completion: [ArrayParam]"))
        self.assertFalse(self.app.appserver_startup_error(""))

    def test_appserver_for_falls_back_to_a_stdio_child_without_a_user_manager(self):
        task = {"id": "no-user-manager", "workspace": self.temp.name, "ssh_host": ""}
        started = []

        class FakeClient:
            daemon_pid = 0

            def __init__(self, env, key, command=None, **kwargs):
                self.key = key
                self.command = command

            async def start(self):
                started.append(self.command)

            async def close(self):
                return None

        async def run():
            with mock.patch.object(self.app, "ensure_persistent_appserver",
                                   new=mock.AsyncMock(side_effect=RuntimeError("Failed to connect to bus"))), \
                 mock.patch.object(self.app, "AppServerClient", FakeClient), \
                 mock.patch.object(self.app, "remove_stale_task_appservers", new=mock.AsyncMock()):
                return await self.app.appserver_for(None, task)

        client = asyncio.run(run())
        try:
            self.assertEqual([[self.app.CODEX_BIN, "app-server", "--stdio"]], started)
            self.assertEqual([self.app.CODEX_BIN, "app-server", "--stdio"], client.command)
        finally:
            self.app.app_servers.pop(self.app.appserver_key(None, task), None)

    def test_supervise_turn_stops_retrying_when_the_appserver_cannot_start(self):
        task_id = "no-user-manager-supervisor"
        session_id = "no-user-manager-supervisor-session"
        self.make_task(task_id, "running")
        self.insert_session(task_id, session_id)
        self.app.db.execute(
            "UPDATE tasks SET retry_forever=1,goal='keep going',goal_status='active',retry_count=17785 WHERE id=?",
            (task_id,),
        )
        task = self.app.task_or_404(task_id)
        error = RuntimeError("Unable to start persistent Codex app-server: Failed to connect to bus: "
                             "$DBUS_SESSION_BUS_ADDRESS and $XDG_RUNTIME_DIR not defined")
        with mock.patch.object(self.app, "appserver_for", new=mock.AsyncMock(side_effect=error)), \
             mock.patch.object(self.app, "refresh_external_primary", return_value=None), \
             mock.patch.object(self.app, "launch_after_turn_cleanup", new=mock.AsyncMock()) as retry:
            asyncio.run(self.app.supervise_appserver_turn(task, None, "message", "", "hello", "", session_id, set()))
        retry.assert_not_awaited()
        row = self.app.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        self.assertEqual("failed", row["status"])
        self.assertIn("Failed to connect to bus", row["last_error"])
        self.app.db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_persistent_appserver_restarts_an_active_stale_endpoint(self):
        from codex_partner import persistent_appserver

        calls = []
        readiness = iter((False, False, True))

        async def fake_run(*command):
            calls.append(command)
            return 0, ""

        async def fake_ready(_port):
            return next(readiness)

        async def run():
            with mock.patch.object(persistent_appserver, "supported", return_value=True), \
                 mock.patch.object(persistent_appserver, "_run", side_effect=fake_run), \
                 mock.patch.object(persistent_appserver, "_endpoint_ready", side_effect=fake_ready):
                return await persistent_appserver.ensure_service(
                    "provider:stale-endpoint",
                    "/usr/bin/codex",
                    {"HOME": self.temp.name, "PATH": "/usr/bin"},
                    Path(self.temp.name),
                )

        endpoint = asyncio.run(run())
        unit = persistent_appserver.unit_name("provider:stale-endpoint")
        self.assertIsNotNone(endpoint)
        self.assertIn(("systemctl", "--user", "restart", unit), calls)
        self.assertFalse(any("systemd-run" in command for command in calls))

    def test_database_reads_do_not_wait_for_global_writer_lock(self):
        completed = threading.Event()
        result = []

        def read():
            result.append(self.app.db.one("SELECT 1 AS value"))
            completed.set()

        with self.app.db.lock:
            thread = threading.Thread(target=read)
            thread.start()
            self.assertTrue(completed.wait(0.5), "WAL read was blocked by the Python writer lock")
        thread.join(timeout=1)
        self.assertEqual({"value": 1}, result[0])

    def test_timeline_has_task_id_ordering_index(self):
        indexes = self.app.db.all("PRAGMA index_list(events)")
        self.assertIn("idx_events_task_id", {index["name"] for index in indexes})
        plan = self.app.db.all(
            "EXPLAIN QUERY PLAN SELECT * FROM events WHERE task_id=? ORDER BY id DESC LIMIT ?",
            ("thread", 120),
        )
        self.assertTrue(any("idx_events_task_id" in row["detail"] for row in plan), plan)

    def test_first_page_does_not_wait_for_global_session_history(self):
        core = (Path(__file__).resolve().parents[1] / "static" / "core.js").read_text(encoding="utf-8")
        refresh = core[core.index("async function refresh(") : core.index("async function refreshSelectedConversation")]
        self.assertNotIn('api("/sessions")', refresh)
        self.assertIn("state.sessions = []", refresh)

    def test_terminal_switch_keeps_each_task_socket_and_output_buffer(self):
        conversation = (Path(__file__).resolve().parents[1] / "static" / "conversation.js").read_text(encoding="utf-8")
        self.assertIn("const terminalSockets = new Map()", conversation)
        self.assertIn("const terminalBuffers = new Map()", conversation)
        self.assertIn("terminalSockets.set(taskId, nextSocket)", conversation)
        self.assertIn("rememberTerminalOutput(taskId, data.data)", conversation)
        self.assertIn("restoreTerminalBuffer(taskId)", conversation)
        switch_block = conversation[conversation.index("function connectTerminal") : conversation.index("function openTerminal")]
        self.assertNotIn("terminalSocket.close()", switch_block)

    def test_composer_shortcuts_are_moved_out_of_the_topbar(self):
        core = (Path(__file__).resolve().parents[1] / "static" / "core.js").read_text(encoding="utf-8")
        self.assertIn("placeComposerShortcuts()", core)
        self.assertIn('slice(6).forEach(item => item.remove())', core)
        self.assertIn('"#message-input": "composerPlaceholderWithShortcuts"', core)
        conversation = (Path(__file__).resolve().parents[1] / "static" / "conversation.js").read_text(encoding="utf-8")
        self.assertIn('state.editingQueuedId ? uiLabel("editQueued") : t("composerPlaceholderWithShortcuts")', conversation)

    def test_long_pasted_text_is_forced_to_a_workspace_attachment(self):
        app_js = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("const LONG_PASTE_FILE_THRESHOLD = 6000", app_js)
        self.assertIn('event.clipboardData?.getData("text/plain")', app_js)
        self.assertIn('new File([text], `pasted-text-${stamp}.txt`', app_js)
        self.assertIn('attachComposerFiles([file], { forceUploadText: true, suppressReadyToast: true })', app_js)
        self.assertIn('textFile && !forceUploadText', app_js)
        self.assertIn("else insertComposerText(text)", app_js)

    def test_browser_user_message_anchors_tools_below_the_triggering_message(self):
        worker = (Path(__file__).resolve().parents[1] / "static" / "chat-worker.js").read_text(encoding="utf-8")
        self.assertIn("const browserUserKeys = new Set", worker)
        self.assertIn("browserUserKeys.has(key)", worker)
        self.assertIn("browserUserBodies.get(body)", worker)
        self.assertIn("matchedBrowserBodies.set(body", worker)

        script = f'''
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const context = {{ self: {{ postMessage() {{}} }} }};
vm.createContext(context);
vm.runInContext(fs.readFileSync({json.dumps(str(Path(__file__).resolve().parents[1] / "static/chat-worker.js"))}, "utf8"), context);
const body = "trigger the tool";
const merged = context.mergeEvents([
  {{ id: "tool", ts: "2026-08-28T09:10:00Z", stream: "app-server", payload: JSON.stringify({{ type: "commandExecution", command: "pwd" }}) }},
  {{ id: "late-native-user", ts: "2026-08-28T09:11:00Z", stream: "app-server", payload: JSON.stringify({{ type: "userMessage", text: body, client_message_id: "client-1" }}) }},
], [
  {{ id: "client-1", body, status: "running", created_at: "2026-08-28T09:08:49Z" }},
]);
process.stdout.write(JSON.stringify(merged.map(event => ({{ stream: event.stream, ts: event.ts, type: context.valueOf(event.payload).type }}))));
'''
        result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True, encoding="utf-8")
        merged = json.loads(result.stdout)
        self.assertEqual("user", merged[0]["stream"])
        self.assertEqual("browserMessage", merged[0]["type"])
        self.assertEqual("commandExecution", merged[1]["type"])

    def test_long_paths_have_dedicated_full_value_copy_controls(self):
        conversation = (Path(__file__).resolve().parents[1] / "static" / "conversation.js").read_text(encoding="utf-8")
        worker = (Path(__file__).resolve().parents[1] / "static" / "chat-worker.js").read_text(encoding="utf-8")
        styles = (Path(__file__).resolve().parents[1] / "static" / "styles.css").read_text(encoding="utf-8")
        self.assertIn('data-copy-path="${esc(item.cwd)}"', conversation)
        self.assertIn("function decorateCopyablePaths", conversation)
        self.assertIn("function looksLikeCopyablePath", conversation)
        self.assertIn('event.target.closest("[data-copy-path]")', conversation)
        self.assertIn('data-copy-path="${safePath}"', worker)
        self.assertIn(".inline-path", styles)
        self.assertIn(".path-copy-button", styles)
        self.assertIn('data-copy-workspace="${esc(task.workspace)}"', conversation)
        self.assertIn("workspace-path-value", conversation)
        self.assertIn('.message-content a[href]', conversation)
        self.assertIn("inline-path-link", conversation)
        app_js = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("button.dataset.copyWorkspace", app_js)
        self.assertIn("工作目录已复制", app_js)
        html = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="media-viewer-copy-path"', html)
        self.assertIn('$("#media-viewer-copy-path").dataset.copyPath = path', conversation)
        self.assertIn("已复制完整路径", conversation)

    def test_ctrl_b_focus_mode_collapses_both_sidebars(self):
        core = (Path(__file__).resolve().parents[1] / "static" / "core.js").read_text(encoding="utf-8")
        app_js = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")
        styles = (Path(__file__).resolve().parents[1] / "static" / "styles.css").read_text(encoding="utf-8")
        self.assertIn('localStorage.getItem("codex-partner-focus-mode") === "1"', core)
        self.assertIn("function setFocusMode", core)
        self.assertIn("function toggleFocusMode", core)
        self.assertIn("focusRestoreLayout", core)
        self.assertIn("sidebarOpen: sessionSidebarLayoutIsOpen()", core)
        self.assertIn("setSessionSidebarOpen(restore.sidebarOpen !== false, false)", core)
        self.assertIn("state.inspectorClosed = !restore.inspectorOpen", core)
        self.assertIn("return state.focusMode ? false : sessionSidebarLayoutIsOpen()", core)
        self.assertIn('event.key.toLowerCase() === "b"', app_js)
        self.assertIn("toggleFocusMode()", app_js)
        self.assertIn(".workspace-app.focus-mode > .session-sidebar", styles)
        self.assertIn(".workspace-app.focus-mode > .inspector", styles)
        self.assertIn(".workspace-app.focus-mode .message { width: min(1280px, 100%)", styles)
        self.assertIn(".workspace-app.focus-mode .message-body", styles)
        self.assertIn(".workspace-app.focus-mode .activity-group { max-width: 1280px", styles)
        self.assertIn(".workspace-app.focus-mode .composer-dock", styles)

    def test_global_session_history_query_does_not_block_event_loop(self):
        def slow_query(*_args):
            time.sleep(0.2)
            return []

        async def exercise():
            started = time.perf_counter()
            with mock.patch.object(self.app.db, "all", side_effect=slow_query):
                query = asyncio.create_task(self.app.list_sessions(100, None))
                await asyncio.sleep(0.02)
                self.assertLess(time.perf_counter() - started, 0.1)
                self.assertFalse(query.done())
                self.assertEqual([], await query)

        asyncio.run(exercise())

    def test_global_sessions_do_not_scan_event_history_for_deduplication(self):
        source = Path(self.app.__file__).read_text(encoding="utf-8")
        start = source.index('@app.get("/api/sessions")')
        block = source[start : start + 5000]
        self.assertNotIn("FROM events", block)
        self.assertIn("appserver_turn_tasks.items()", block)

    def test_appserver_start_failure_cleans_up_without_client(self):
        task_id = "appserver-start-failure"
        session_id = "appserver-start-failure-session"
        self.make_task(task_id)
        task = self.app.task_or_404(task_id)
        self.app.db.execute(
            "INSERT INTO sessions (id,task_id,status,attempt,command,started_at) VALUES (?,?,?,?,?,?)",
            (session_id, task_id, "running", 1, "codex app-server", self.app.now()),
        )
        self.app.db.execute(
            "UPDATE tasks SET active_session_id=?,run_mode='message',execution_source='dashboard' WHERE id=?",
            (session_id, task_id),
        )

        async def run():
            with mock.patch.object(self.app, "appserver_for", new=mock.AsyncMock(side_effect=RuntimeError("start failed"))), \
                 mock.patch.object(self.app, "provider_rows", return_value=[]), \
                 mock.patch.object(self.app, "schedule_task_drain") as schedule:
                await self.app.supervise_appserver_turn(task, None, "resume", "message", "", "", session_id, set())
                schedule.assert_called_once_with(task_id)

        asyncio.run(run())
        failed = self.app.task_or_404(task_id)
        self.assertEqual("failed", failed["status"])
        self.assertEqual("start failed", failed["last_error"])
        self.assertFalse(failed["active_session_id"])
        self.assertEqual("", failed["run_mode"])

    def test_success_path_releases_all_execution_bindings(self):
        source = Path(self.app.__file__).read_text(encoding="utf-8")
        self.assertIn(
            "status=?,active_session_id=NULL,execution_source='',execution_turn_id='',run_mode='',last_error=''",
            source,
        )

    def test_idle_session_restart_replaces_codex_service_and_preserves_thread(self):
        task_id = "manual-restart-idle"
        self.make_task(task_id, "available")

        async def run():
            with mock.patch.object(self.app, "refresh_live_external_turn", new=mock.AsyncMock(return_value=False)), \
                 mock.patch.object(self.app, "dashboard_owns_task", return_value=False), \
                 mock.patch.object(self.app, "stop_persistent_appserver", new=mock.AsyncMock()) as stop_service, \
                 mock.patch.object(self.app, "appserver_for", new=mock.AsyncMock(return_value=SimpleNamespace())) as start_service:
                result = await self.app.restart_task_codex(task_id, None)
            self.assertTrue(result["restarted"])
            self.assertFalse(result["resumed"])
            self.assertEqual(task_id, result["thread_id"])
            stop_service.assert_awaited_once()
            start_service.assert_awaited_once()

        asyncio.run(run())

    def test_running_goal_restart_resumes_same_goal_after_process_replacement(self):
        task_id = "manual-restart-running-goal"
        self.make_task(task_id, "running")
        self.app.db.execute(
            "UPDATE tasks SET goal=?,goal_status='active',run_mode='goal_resume' WHERE id=?",
            ("finish the goal", task_id),
        )

        async def fake_stop(actual_task_id, pause_goal=True):
            self.assertEqual(task_id, actual_task_id)
            self.assertFalse(pause_goal)
            self.app.db.execute(
                "UPDATE tasks SET status='stopped',active_session_id=NULL,run_mode='' WHERE id=?",
                (task_id,),
            )
            return self.app.task_or_404(task_id)

        async def fake_launch(actual_task_id, mode="start", message="", **_kwargs):
            self.assertEqual(task_id, actual_task_id)
            self.assertEqual("goal-resume", mode)
            self.assertEqual("", message)
            self.app.db.execute(
                "UPDATE tasks SET status='running',run_mode='goal_resume' WHERE id=?",
                (task_id,),
            )
            return self.app.task_or_404(task_id)

        async def run():
            with mock.patch.object(self.app, "refresh_live_external_turn", new=mock.AsyncMock(return_value=False)), \
                 mock.patch.object(self.app, "dashboard_owns_task", return_value=True), \
                 mock.patch.object(self.app, "stop_task_run", new=mock.AsyncMock(side_effect=fake_stop)) as stop_run, \
                 mock.patch.object(self.app, "stop_persistent_appserver", new=mock.AsyncMock()), \
                 mock.patch.object(self.app, "appserver_for", new=mock.AsyncMock(return_value=SimpleNamespace())), \
                 mock.patch.object(self.app, "launch", new=mock.AsyncMock(side_effect=fake_launch)) as launch:
                result = await self.app.restart_task_codex(task_id, None)
            self.assertTrue(result["resumed"])
            self.assertEqual(task_id, result["thread_id"])
            stop_run.assert_awaited_once_with(task_id, pause_goal=False)
            launch.assert_awaited_once()

        asyncio.run(run())

    def test_restart_uses_persisted_running_state_during_owner_binding_race(self):
        task_id = "manual-restart-owner-binding-race"
        self.make_task(task_id, "running")

        async def fake_stop(actual_task_id, pause_goal=True):
            self.assertEqual(task_id, actual_task_id)
            self.app.db.execute(
                "UPDATE tasks SET status='stopped',active_session_id=NULL,run_mode='' WHERE id=?",
                (task_id,),
            )
            return self.app.task_or_404(task_id)

        async def fake_launch(actual_task_id, mode="start", message="", **_kwargs):
            self.assertEqual("resume", mode)
            self.assertIn("Continue from where", message)
            self.app.db.execute("UPDATE tasks SET status='running' WHERE id=?", (actual_task_id,))
            return self.app.task_or_404(actual_task_id)

        async def run():
            with mock.patch.object(self.app, "refresh_live_external_turn", new=mock.AsyncMock(return_value=False)), \
                 mock.patch.object(self.app, "dashboard_owns_task", return_value=False), \
                 mock.patch.object(self.app, "stop_task_run", new=mock.AsyncMock(side_effect=fake_stop)), \
                 mock.patch.object(self.app, "stop_persistent_appserver", new=mock.AsyncMock()), \
                 mock.patch.object(self.app, "appserver_for", new=mock.AsyncMock(return_value=SimpleNamespace())), \
                 mock.patch.object(self.app, "launch", new=mock.AsyncMock(side_effect=fake_launch)):
                result = await self.app.restart_task_codex(task_id, None)
            self.assertTrue(result["resumed"])

        asyncio.run(run())

    def test_restart_normalizes_a_pre_thread_task_alias_before_owner_lookup(self):
        task_id = "manual-restart-canonical"
        alias = "manual-restart-alias"
        self.make_task(task_id, "available")
        self.app.task_id_aliases[alias] = task_id

        async def run():
            with mock.patch.object(self.app, "refresh_live_external_turn", new=mock.AsyncMock(return_value=False)) as refresh, \
                 mock.patch.object(self.app, "dashboard_owns_task", return_value=False), \
                 mock.patch.object(self.app, "stop_persistent_appserver", new=mock.AsyncMock()), \
                 mock.patch.object(self.app, "appserver_for", new=mock.AsyncMock(return_value=SimpleNamespace())):
                result = await self.app.restart_task_codex(alias, None)
            self.assertEqual(task_id, result["id"])
            refresh.assert_awaited_once_with(task_id)

        try:
            asyncio.run(run())
        finally:
            self.app.task_id_aliases.pop(alias, None)

    def test_partner_update_settings_are_private_and_persistent(self):
        from codex_partner.self_update import load_state, save_state

        path = Path(self.temp.name) / "partner-update-test.json"
        state = load_state(path)
        self.assertTrue(state["auto_update"])
        state["auto_update"] = False
        save_state(path, state)
        self.assertFalse(load_state(path)["auto_update"])
        if os.name != "nt":
            self.assertEqual(0o600, path.stat().st_mode & 0o777)

    def test_partner_update_uses_official_pypi_and_exact_version(self):
        updater = (Path(__file__).resolve().parents[1] / "codex_partner" / "self_update.py").read_text(encoding="utf-8")
        app_source = Path(self.app.__file__).read_text(encoding="utf-8")
        self.assertIn('"https://pypi.org/simple"', updater)
        self.assertIn('package=f"codex-partner=={latest}"', app_source)
        self.assertIn("AUTO_UPDATE_INTERVAL_MINUTES = 15", updater)

    def test_partner_update_without_new_version_does_not_restart(self):
        state_path = Path(self.temp.name) / "partner-update-no-change.json"
        async def run():
            with mock.patch.object(self.app, "PARTNER_UPDATE_STATE_PATH", state_path), \
                 mock.patch.object(self.app, "installed_partner_version", return_value="0.0.10"), \
                 mock.patch.object(self.app, "latest_pypi_version", return_value="0.0.10"), \
                 mock.patch.object(self.app, "pip_upgrade_partner", new=mock.AsyncMock()) as pip_upgrade, \
                 mock.patch.object(self.app.asyncio, "create_task") as create_task:
                response = await self.app.perform_partner_update()
            self.assertFalse(response["changed"])
            self.assertFalse(response["restart_scheduled"])
            pip_upgrade.assert_not_awaited()
            create_task.assert_not_called()
            self.assertEqual("current", json.loads(state_path.read_text())["last_result"])

        asyncio.run(run())

    def test_partner_update_installs_without_restarting_until_user_confirms(self):
        state_path = Path(self.temp.name) / "partner-update-pending-restart.json"
        upgrade = {
            "ok": True,
            "previous_version": "0.0.10",
            "installed_version": "0.0.11",
            "changed": True,
            "command": [sys.executable, "-m", "pip"],
            "output": "installed",
        }

        async def run():
            with mock.patch.object(self.app, "PARTNER_UPDATE_STATE_PATH", state_path), \
                 mock.patch.object(self.app, "APP_VERSION", "0.0.10"), \
                 mock.patch.object(self.app, "installed_partner_version", return_value="0.0.10"), \
                 mock.patch.object(self.app, "latest_pypi_version", return_value="0.0.11"), \
                 mock.patch.object(self.app, "pip_upgrade_partner", new=mock.AsyncMock(return_value=upgrade)) as pip_upgrade, \
                 mock.patch.object(self.app.asyncio, "create_task") as create_task:
                response = await self.app.perform_partner_update()
            self.assertTrue(response["changed"])
            self.assertTrue(response["restart_required"])
            self.assertFalse(response["restart_scheduled"])
            create_task.assert_not_called()
            self.assertTrue(json.loads(state_path.read_text())["restart_required"])
            pip_upgrade.assert_awaited_once_with(package="codex-partner==0.0.11")

        asyncio.run(run())

    def test_partner_restart_requires_confirmation_and_schedules_restart(self):
        state_path = Path(self.temp.name) / "partner-update-confirm-restart.json"
        state_path.write_text(json.dumps({"restart_required": True, "auto_update": True}))

        async def run():
            payload = self.app.PartnerRestartIn(confirm=True)
            scheduled = SimpleNamespace()
            with mock.patch.object(self.app, "PARTNER_UPDATE_STATE_PATH", state_path), \
                 mock.patch.object(self.app, "installed_partner_version", return_value="0.0.11"), \
                 mock.patch.object(self.app.asyncio, "create_task", return_value=scheduled) as create_task:
                response = await self.app.restart_partner_after_update(payload, None)
            self.assertTrue(response["restart_scheduled"])
            create_task.assert_called_once()
            create_task.call_args.args[0].close()

        asyncio.run(run())

    def test_native_history_accepts_iso_timestamps(self):
        stamp = self.app.native_stamp("2026-08-16T01:27:10.093564Z", "fallback")
        self.assertEqual("2026-08-16T01:27:10.093564+00:00", stamp)

    def test_rollout_tool_activity_exposes_real_command_and_redacts_secrets(self):
        record = {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "id": "tool-command",
                "call_id": "call-command",
                "name": "exec_command",
                "arguments": json.dumps({"cmd": "curl -H 'Authorization: Bearer secret-value' https://example.test"}),
            },
        }
        payload = self.app.rollout_browser_payload(record)
        self.assertEqual("commandExecution", payload["type"])
        self.assertIn("curl -H", payload["text"])
        self.assertIn("Bearer ***", payload["text"])
        self.assertNotIn("secret-value", payload["text"])
        self.assertIn("Bearer ***", payload["command"])
        self.assertNotIn("secret-value", payload["command"])
        self.assertEqual("call-command", payload["item_id"])
        self.assertEqual("started", payload["status"])

        output_payload = self.app.rollout_browser_payload({
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call-command",
                "output": json.dumps({"output": "line one\nline two", "metadata": {"exit_code": 1}}),
            },
        })
        self.assertEqual("toolOutput", output_payload["type"])
        self.assertEqual("failed", output_payload["status"])
        self.assertEqual(1, output_payload["exit_code"])
        self.assertEqual("line one\nline two", output_payload["output"])

        wrapped = [
            {"type": "input_text", "text": "Script completed\nOutput:\n"},
            {"type": "input_text", "text": json.dumps({"output": "actual stdout", "metadata": {"exit_code": 0}})},
        ]
        wrapped_output, wrapped_exit = self.app.decoded_tool_output(wrapped)
        self.assertEqual("actual stdout", wrapped_output)
        self.assertEqual(0, wrapped_exit)
        repr_output, repr_exit = self.app.decoded_tool_output(repr(wrapped))
        self.assertEqual("actual stdout", repr_output)
        self.assertEqual(0, repr_exit)

    def test_native_tool_output_enriches_matching_rollout_activity(self):
        rollout = [{
            "id": 1,
            "stream": "rollout",
            "payload": json.dumps({"type": "commandExecution", "turn_id": "turn-1", "item_id": "call-1", "command": "pwd"}),
        }]
        native = [{
            "id": "native-1",
            "stream": "native",
            "payload": json.dumps({"type": "commandExecution", "turn_id": "turn-1", "item_id": "call-1", "command": "pwd", "output": "/tmp", "exit_code": 0, "status": "completed"}),
        }]
        merged = self.app.merge_native_with_rollout(native, rollout)
        self.assertEqual(1, len(merged))
        payload = json.loads(merged[0]["payload"])
        self.assertEqual("/tmp", payload["output"])
        self.assertEqual(0, payload["exit_code"])
        self.assertEqual("completed", payload["status"])

    def test_native_timeline_keeps_normalized_appserver_activity(self):
        task_id = "native-appserver-activity"
        self.make_task(task_id, "available")
        self.app.db.execute("UPDATE tasks SET native=1,codex_session_id=? WHERE id=?", (task_id, task_id))
        session_id = "native-appserver-session"
        self.app.db.execute(
            "INSERT INTO sessions (id,task_id,status,attempt,provider_id,command,started_at,codex_session_id) VALUES (?,?,?,?,?,?,?,?)",
            (session_id, task_id, "imported", 0, None, "imported", self.app.now(), task_id),
        )
        for index, payload in enumerate((
            {"type": "agent_delta", "delta": "partial", "turn_id": "turn-1"},
            {"type": "codex", "method": "item/commandExecution/outputDelta", "params": {"turnId": "turn-1"}},
            {"type": "commandExecution", "command": "pwd", "item_id": "call-1", "turn_id": "turn-1", "status": "completed"},
            {"type": "mcpToolCall", "tool": "search", "item_id": "call-2", "turn_id": "turn-1", "status": "completed"},
        )):
            self.app.db.execute(
                "INSERT INTO events (session_id,task_id,ts,stream,payload) VALUES (?,?,?,?,?)",
                (session_id, task_id, f"2026-08-19T08:00:0{index}+00:00", "app-server", json.dumps(payload)),
            )

        async def exercise():
            with mock.patch.object(self.app, "native_history_events", new=mock.AsyncMock(return_value=[])):
                events, _metrics = await self.app.native_timeline_events(self.app.task_or_404(task_id))
            return [json.loads(event["payload"])["type"] for event in events]

        try:
            self.assertEqual(["commandExecution", "mcpToolCall"], asyncio.run(exercise()))
        finally:
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_native_fast_timeline_uses_persisted_semantic_events(self):
        task_id = "native-fast-timeline"
        self.make_task(task_id, "available")
        self.app.db.execute("UPDATE tasks SET native=1,codex_session_id=? WHERE id=?", (task_id, task_id))
        session_id = "native-fast-session"
        self.app.db.execute(
            "INSERT INTO sessions (id,task_id,status,attempt,provider_id,command,started_at,codex_session_id) VALUES (?,?,?,?,?,?,?,?)",
            (session_id, task_id, "imported", 0, None, "imported", self.app.now(), task_id),
        )
        for index, payload in enumerate((
            {"type": "agent_delta", "delta": "noise"},
            {"type": "codex", "method": "item/commandExecution/outputDelta"},
            {"type": "commandExecution", "command": "pwd"},
            {"type": "agentMessage", "text": "done"},
        )):
            self.app.db.execute(
                "INSERT INTO events (session_id,task_id,ts,stream,payload) VALUES (?,?,?,?,?)",
                (session_id, task_id, f"2026-08-19T08:01:0{index}+00:00", "app-server", json.dumps(payload)),
            )
        try:
            result = asyncio.run(self.app.task_timeline(task_id, limit=120, fast=True, _=None))
            self.assertTrue(result["fast"])
            self.assertEqual(["commandExecution", "agentMessage"], [json.loads(row["payload"])["type"] for row in result["items"]])
            cursor = self.app.decode_history_cursor(result["next_cursor"])
            self.assertEqual("native", cursor["kind"])
            self.assertEqual(result["items"][0]["ts"], cursor["ts"])
            older = {**result["items"][0], "id": "older", "ts": "2026-08-18T08:00:00+00:00"}
            with mock.patch.object(self.app, "native_timeline_events", new=mock.AsyncMock(return_value=([older, *result["items"]], []))):
                previous = asyncio.run(self.app.task_timeline(task_id, before=result["next_cursor"], _=None))
            self.assertEqual([older], previous["items"])
        finally:
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_rollout_plan_steps_extracts_wrapped_update_plan(self):
        payload = {
            "type": "custom_tool_call",
            "input": 'const p = await tools.update_plan({plan:[{step:"Inspect events","status":"completed"},{"step":"Filter noise",status:"in_progress"}]}); text(p);',
        }
        self.assertEqual([
            {"step": "Inspect events", "status": "completed"},
            {"step": "Filter noise", "status": "in_progress"},
        ], self.app.rollout_plan_steps(payload))
        browser = self.app.rollout_browser_payload({"type": "response_item", "payload": payload})
        self.assertEqual("planUpdate", browser["type"])
        self.assertEqual(2, len(browser["plan"]))

        patch_payload = self.app.rollout_browser_payload({
            "type": "response_item",
            "payload": {"type": "custom_tool_call", "name": "exec", "input": "text(await tools.apply_patch(patch));"},
        })
        self.assertEqual("fileChange", patch_payload["type"])
        self.assertEqual("应用代码修改", patch_payload["text"])

    def test_empty_reasoning_items_are_filtered_at_source_and_in_history(self):
        self.assertIsNone(self.app.rollout_browser_payload({
            "type": "response_item",
            "payload": {"type": "reasoning", "id": "empty", "summary": [], "content": []},
        }))
        populated = self.app.rollout_browser_payload({
            "type": "response_item",
            "payload": {"type": "reasoning", "id": "useful", "summary": [{"type": "summary_text", "text": "Inspecting queue state"}], "content": []},
        })
        self.assertEqual("reasoning", populated["type"])
        self.assertIn("Inspecting queue state", populated["text"])

        task_id = "empty-reasoning-history"
        self.make_task(task_id)
        self.app.db.execute("UPDATE tasks SET native=1,codex_session_id=? WHERE id=?", (task_id, task_id))

        class FakeClient:
            async def request(self, method, params):
                return {"thread": {"turns": [{"id": "turn", "items": [
                    {"type": "reasoning", "id": "empty", "summary": [], "content": []},
                    {"type": "agentMessage", "id": "answer", "text": "visible"},
                ]}]}}

        try:
            self.app.native_history_cache.pop(task_id, None)
            with mock.patch.object(self.app, "appserver_for", new=mock.AsyncMock(return_value=FakeClient())):
                rows = asyncio.run(self.app.native_history_events(self.app.task_or_404(task_id)))
            self.assertEqual(1, len(rows))
            self.assertEqual("visible", json.loads(rows[0]["payload"])["text"])
        finally:
            self.app.native_history_cache.pop(task_id, None)
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_task_or_404_falls_back_when_alias_points_to_missing_task(self):
        task_id = "task-alias-fallback"
        missing_thread_id = "missing-thread-alias"
        self.make_task(task_id)
        self.app.task_id_aliases[task_id] = missing_thread_id
        try:
            task = self.app.task_or_404(task_id)
            self.assertEqual(task_id, task["id"])
            self.assertNotIn(task_id, self.app.task_id_aliases)
        finally:
            self.app.task_id_aliases.pop(task_id, None)
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_reconcile_task_session_ids_migrates_existing_local_task(self):
        task_id = "legacy-dashboard-id"
        thread_id = "thread-session-id"
        session_id = "legacy-session-id"
        with tempfile.TemporaryDirectory(dir=self.temp.name) as directory:
            root = Path(directory) / "codex_partner"
            with mock.patch.object(self.app, "SESSION_WORKSPACE_ROOT", root):
                self.make_task(task_id)
                self.app.db.execute(
                    "UPDATE tasks SET workspace=?,codex_session_id=?,active_session_id=? WHERE id=?",
                    (str(root), thread_id, session_id, task_id),
                )
                self.app.db.execute(
                    "INSERT INTO sessions (id,task_id,status,attempt,command,started_at,codex_session_id) VALUES (?,?,?,?,?,?,?)",
                    (session_id, task_id, "running", 1, "codex app-server", self.app.now(), thread_id),
                )
                self.app.reconcile_task_session_ids()
                task = self.app.task_or_404(thread_id)
                self.assertEqual(thread_id, task["id"])
                self.assertEqual(thread_id, task["codex_session_id"])
                self.assertEqual(str((root / thread_id).resolve()), task["workspace"])
                self.assertTrue((root / thread_id).is_dir())
                session = self.app.db.one("SELECT task_id FROM sessions WHERE id=?", (session_id,))
                self.assertEqual(thread_id, session["task_id"])
        self.app.db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        self.app.db.execute("DELETE FROM tasks WHERE id IN (?,?)", (task_id, thread_id))

    def test_avatar_is_persisted_per_login_and_broadcast_to_matching_devices(self):
        image = self.app.Image.new("RGB", (32, 32), "#ffd83e")
        second_frame = self.app.Image.new("RGB", (32, 32), "#2f9e73")
        source = io.BytesIO()
        image.save(source, "GIF", save_all=True, append_images=[second_frame], duration=[80, 120], loop=0)
        data_url = "data:image/gif;base64," + base64.b64encode(source.getvalue()).decode()

        class Socket:
            def __init__(self):
                self.payloads = []

            async def send_json(self, payload):
                self.payloads.append(payload)

        own_device = Socket()
        other_user = Socket()
        self.app.overview_clients.update({own_device, other_user})
        self.app.overview_client_users[own_device] = "avatar-user"
        self.app.overview_client_users[other_user] = "someone-else"
        try:
            result = asyncio.run(self.app.update_profile_avatar(
                self.app.AvatarIn(data_url=data_url), {"username": "avatar-user"}
            ))
            path = self.app.profile_avatar_path("avatar-user")
            self.assertTrue(path.is_file())
            if os.name != "nt":
                self.assertEqual(0o600, path.stat().st_mode & 0o777)
            with self.app.Image.open(path) as saved:
                self.assertTrue(saved.is_animated)
                self.assertEqual(2, saved.n_frames)
            response = asyncio.run(self.app.get_profile_avatar({"username": "avatar-user"}))
            self.assertEqual("image/gif", response.media_type)
            self.assertTrue(result["avatar_url"].startswith("/api/profile/avatar?v="))
            self.assertEqual("profile_updated", own_device.payloads[-1]["type"])
            self.assertFalse(other_user.payloads)
        finally:
            self.app.overview_clients.difference_update({own_device, other_user})
            self.app.overview_client_users.pop(own_device, None)
            self.app.overview_client_users.pop(other_user, None)

    def test_current_profile_image_is_the_default_user_avatar(self):
        static = Path(__file__).resolve().parents[1] / "static"
        avatar = static / "default-user-avatar.webp"
        core = (static / "core.js").read_text(encoding="utf-8")
        html = (static / "index.html").read_text(encoding="utf-8")
        self.assertTrue(avatar.is_file())
        with self.app.Image.open(avatar) as image:
            self.assertEqual("WEBP", image.format)
            self.assertEqual((282, 320), image.size)
        self.assertIn('const DEFAULT_USER_AVATAR = "/default-user-avatar.webp?v=20260818"', core)
        self.assertIn('profile?.avatar_url || DEFAULT_USER_AVATAR', core)
        self.assertIn('/core.js?v=20260831-focus-wide', html)

    def test_app_server_reads_large_json_messages_in_chunks(self):
        async def collect():
            payload = json.dumps({"id": 7, "result": {"text": "x" * (2 * 1024 * 1024)}}).encode() + b"\n"

            class Stdout:
                def __init__(self):
                    self.chunks = [payload[:700_000], payload[700_000:1_400_000], payload[1_400_000:]]

                async def read(self, _size):
                    return self.chunks.pop(0) if self.chunks else b""

            class Process:
                stdout = Stdout()

            client = self.app.AppServerClient({}, "large-json")
            client.process = Process()
            return [line async for line in client._message_lines()]

        lines = asyncio.run(collect())
        self.assertEqual(1, len(lines))
        self.assertEqual(2 * 1024 * 1024, len(json.loads(lines[0])["result"]["text"]))

    def test_rollout_watcher_imports_live_exec_thread_missing_from_app_server(self):
        thread_id = "01a00000-0000-7000-8000-000000000001"
        rollout = Path(self.app.CODEX_HOME) / "sessions" / "2026" / "08" / "20" / f"rollout-{thread_id}.jsonl"
        rollout.parent.mkdir(parents=True, exist_ok=True)
        records = [
            {"timestamp": "2026-08-20T10:00:00Z", "type": "session_meta", "payload": {
                "id": thread_id, "session_id": thread_id, "timestamp": "2026-08-20T10:00:00Z",
                "cwd": self.temp.name, "model_provider": "mtcode",
            }},
            {"timestamp": "2026-08-20T10:00:01Z", "type": "event_msg", "payload": {
                "type": "task_started", "turn_id": "live-turn", "started_at": "2026-08-20T10:00:01Z",
            }},
            {"timestamp": "2026-08-20T10:00:02Z", "type": "event_msg", "payload": {
                "type": "user_message", "message": "Continue the live native task",
            }},
        ]
        rollout.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        self.app.native_import_attempt_at = 0
        try:
            with mock.patch.object(self.app, "USE_APP_SERVER", True), \
                 mock.patch.object(self.app, "native_rollout_rows", return_value=[{"thread_id": thread_id, "path": str(rollout)}]), \
                 mock.patch.object(self.app, "sync_native_threads", new=mock.AsyncMock(return_value={"imported": 0})), \
                 mock.patch.object(self.app, "rollout_writer_pid_map", return_value={os.path.realpath(rollout): {4242}}), \
                 mock.patch.object(self.app, "native_rollout_writer_pids", return_value={4242}):
                asyncio.run(self.app.refresh_native_rollouts())
            task = self.app.task_or_404(thread_id)
            self.assertEqual("running", task["status"])
            self.assertTrue(task["external_running"])
            self.assertEqual("Continue the live native task", task["prompt"])
        finally:
            self.app.clear_external_turns(thread_id)
            self.app.native_rollout_offsets.pop(str(rollout), None)
            self.app.native_rollout_remainders.pop(str(rollout), None)
            self.app.db.execute("DELETE FROM events WHERE task_id=?", (thread_id,))
            self.app.db.execute("DELETE FROM sessions WHERE task_id=?", (thread_id,))
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (thread_id,))

    def test_unlisted_native_thread_does_not_repeat_full_sync(self):
        thread_id = "internal-thread-not-listed"
        rows = [{"thread_id": thread_id, "path": "/missing/internal-rollout.jsonl"}]
        sync = mock.AsyncMock(return_value={"imported": 0})

        async def refresh_twice():
            await self.app.refresh_native_rollouts()
            # Expire both intervals: unchanged unknown IDs must still not
            # trigger another index sync, but live writer discovery must run.
            self.app.native_import_attempt_at = 0
            self.app.native_index_sync_at = 0
            await self.app.refresh_native_rollouts()

        with mock.patch.object(self.app, "USE_APP_SERVER", True), \
             mock.patch.object(self.app, "native_import_attempt_at", 0), \
             mock.patch.object(self.app, "native_index_sync_at", 0), \
             mock.patch.object(self.app, "native_index_seen_threads", set()), \
             mock.patch.object(self.app, "native_rollout_rows", return_value=rows), \
             mock.patch.object(self.app, "sync_native_threads", sync), \
             mock.patch.object(self.app, "rollout_writer_pid_map", return_value={}) as writers:
            asyncio.run(refresh_twice())
        sync.assert_awaited_once()
        self.assertEqual(2, writers.call_count)

    def test_overview_loads_task_rows_in_one_query(self):
        self.make_task("overview-batch-a", "available")
        self.make_task("overview-batch-b", "available")
        try:
            with mock.patch.object(self.app, "attach_external_task_state", side_effect=lambda row, _: row), \
                 mock.patch.object(self.app.db, "one", wraps=self.app.db.one) as one, \
                 mock.patch.object(self.app.db, "all", wraps=self.app.db.all) as all_rows:
                summaries = self.app.all_task_summaries()
            self.assertTrue({"overview-batch-a", "overview-batch-b"} <= {row["id"] for row in summaries})
            all_rows.assert_called_once()
            one.assert_not_called()
        finally:
            for task_id in ("overview-batch-a", "overview-batch-b"):
                self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_task_list_preview_preserves_full_task_prompt(self):
        task_id = "large-prompt-summary"
        self.make_task(task_id, "available")
        prompt = "完整提示词" * 20000
        self.app.db.execute("UPDATE tasks SET prompt=? WHERE id=?", (prompt, task_id))
        try:
            rows = asyncio.run(self.app.list_tasks())
            summary = next(row for row in rows if row["id"] == task_id)
            self.assertEqual(prompt[:512], summary["prompt"])
            self.assertEqual(prompt[:512], self.app.task_summary(task_id)["prompt"])
            self.assertEqual(prompt, self.app.task_or_404(task_id)["prompt"])
        finally:
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_compact_sidebar_omits_details_but_searches_full_prompt(self):
        task_id = "compact-search-task"
        self.make_task(task_id, "available")
        prompt = "hidden prompt " * 20000 + "literal%_needle"
        goal = "complete goal " * 10000
        self.app.db.execute("UPDATE tasks SET prompt=?,goal=? WHERE id=?", (prompt, goal, task_id))
        try:
            rows = asyncio.run(self.app.list_tasks(compact=True, q="literal%_needle"))
            self.assertEqual([task_id], [row["id"] for row in rows])
            self.assertTrue(rows[0]["_summary"])
            self.assertNotIn("prompt", rows[0])
            self.assertNotIn("goal", rows[0])
            self.assertLess(len(json.dumps(rows)), 2000)
            task = self.app.task_or_404(task_id)
            self.assertEqual(prompt, task["prompt"])
            self.assertEqual(goal, task["goal"])
            self.assertEqual([], asyncio.run(self.app.list_tasks(compact=True, q="absent%_needle")))
        finally:
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_rollout_batch_skips_idle_files_but_checks_active_and_truncated(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = []
            offsets = {}
            mapping = {}
            for name in ("idle", "active", "appended", "truncated", "new"):
                path = Path(directory) / name
                path.write_bytes(b"record\n")
                rows.append({"thread_id": name, "path": str(path)})
                mapping[name] = name
                if name != "new":
                    offsets[str(path)] = 100 if name == "truncated" else 0 if name == "appended" else path.stat().st_size
            changed, live = self.app.changed_rollout_rows(rows, mapping, offsets, {"active"})
            self.assertEqual({"active", "appended", "truncated", "new"}, {row["thread_id"] for row in changed})
            self.assertEqual({row["path"] for row in rows}, live)

    def test_native_sync_preserves_dashboard_model_and_name_overrides(self):
        task_id = f"native-model-{time.time_ns()}"
        thread_id = f"thread-{time.time_ns()}"
        stamp = self.app.now()
        workspace = self.app.create_session_workspace(task_id)
        state_db = Path(self.app.CODEX_HOME) / "state_5.sqlite"
        state_db.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(state_db) as conn:
            conn.execute(
                "CREATE TABLE threads (id TEXT PRIMARY KEY, approval_mode TEXT, sandbox_policy TEXT, model TEXT, model_provider TEXT, archived INTEGER, memory_mode TEXT)"
            )
            conn.execute(
                "INSERT INTO threads (id,approval_mode,sandbox_policy,model,model_provider,archived,memory_mode) VALUES (?,?,?,?,?,?,?)",
                (thread_id, "never", "danger-full-access", "gpt-5.5", "native-provider", 0, "enabled"),
            )
            conn.commit()
        self.app.db.execute(
            "INSERT INTO tasks (id,name,prompt,workspace,status,yolo,provider_id,model,codex_session_id,created_at,updated_at,native,name_revision) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, "Dashboard title", "prompt", str(workspace), "available", 1, "dashboard-provider", "gpt-5.6", thread_id, stamp, stamp, 1, 1),
        )

        class Client:
            async def request(self, method, params):
                if method == "thread/list":
                    return {
                        "data": [{
                            "id": thread_id,
                            "name": "Native thread",
                            "firstUserMessage": "prompt",
                            "cwd": str(workspace),
                            "model": "gpt-5.5",
                            "modelProvider": "native-provider",
                            "createdAt": stamp,
                            "updatedAt": stamp,
                        }],
                        "nextCursor": None,
                    }
                raise AssertionError(method)

        with mock.patch.object(self.app, "USE_APP_SERVER", True), \
             mock.patch.object(self.app, "sync_native_providers", return_value={"available": True}), \
             mock.patch.object(self.app, "appserver_for", new=mock.AsyncMock(return_value=Client())), \
             mock.patch.object(self.app, "native_goal_rows", return_value={}), \
             mock.patch.object(self.app, "native_thread_settings", return_value={}):
            asyncio.run(self.app.sync_native_threads())

        task = self.app.task_or_404(thread_id)
        self.assertFalse(self.app.db.one("SELECT id FROM tasks WHERE id=?", (task_id,)))
        self.assertEqual(thread_id, task["id"])
        self.assertEqual(thread_id, task["codex_session_id"])
        self.assertEqual(thread_id, Path(task["workspace"]).name)
        self.assertEqual("gpt-5.6", task["model"])
        self.assertEqual("dashboard-provider", task["provider_id"])
        self.assertEqual("Dashboard title", task["name"])
        with sqlite3.connect(state_db) as conn:
            row = conn.execute("SELECT model FROM threads WHERE id=?", (thread_id,)).fetchone()
        self.assertEqual("gpt-5.6", row[0])
        session = self.app.db.one("SELECT id,task_id,codex_session_id FROM sessions WHERE task_id=?", (thread_id,))
        self.assertEqual(thread_id, session["id"])
        self.assertEqual(thread_id, session["task_id"])
        self.assertEqual(thread_id, session["codex_session_id"])

    def test_appserver_turn_inputs_preserve_native_modalities_and_workspace_boundary(self):
        workspace = Path(self.temp.name) / "input-workspace"
        workspace.mkdir(exist_ok=True)
        (workspace / "screen.png").write_bytes(b"image")
        (workspace / "notes.pdf").write_bytes(b"pdf")
        (workspace / "voice.ogg").write_bytes(b"audio")
        task = {"workspace": str(workspace), "prompt": "fallback", "context": "", "ssh_host": ""}
        message = (
            "inspect these\n"
            "[[codex-input:localImage:screen.png]]\n"
            "[[codex-input:mention:notes.pdf]]\n"
            "[[codex-input:localAudio:voice.ogg]]\n"
            "[[codex-input:mention:..%2Fsecret.txt]]"
        )
        inputs = self.app.appserver_turn_inputs(task, message)
        self.assertEqual("text", inputs[0]["type"])
        self.assertNotIn("codex-input", inputs[0]["text"])
        self.assertEqual(
            ["localImage", "mention", "localAudio"],
            [item["type"] for item in inputs[1:]],
        )
        self.assertEqual("original", inputs[1]["detail"])
        self.assertEqual("notes.pdf", inputs[2]["name"])
        resolved_workspace = workspace.resolve()
        self.assertTrue(all(resolved_workspace in Path(item["path"]).resolve().parents for item in inputs[1:]))

    def test_goal_resume_does_not_replay_the_original_prompt(self):
        task = {
            "workspace": self.temp.name,
            "prompt": "stale original prompt",
            "context": "",
            "goal": "write a novel",
            "memory_mode": "disabled",
            "ssh_host": "",
        }
        inputs = self.app.appserver_turn_inputs(task, "", include_goal_memory=True)
        self.assertEqual("/goal resume", inputs[0]["text"])
        self.assertNotIn(task["prompt"], inputs[0]["text"])
        self.assertNotIn("stale original prompt", inputs[0]["text"])

    def test_normal_resume_does_not_replay_the_original_prompt(self):
        task = {
            "workspace": self.temp.name,
            "prompt": "stale original prompt",
            "context": "",
            "goal": "",
            "memory_mode": "enabled",
            "ssh_host": "",
        }
        inputs = self.app.appserver_turn_inputs(task, "", continue_existing=True)
        self.assertEqual("Continue from the previous turn.", inputs[0]["text"])
        self.assertNotIn(task["prompt"], inputs[0]["text"])

    def test_cli_goal_resume_is_exact_and_normal_resume_is_neutral(self):
        task = {
            "workspace": self.temp.name,
            "prompt": "stale original prompt",
            "context": "new Goal",
            "goal": "current objective",
            "yolo": False,
            "model": "",
            "reasoning_effort": "",
        }
        goal_cmd = self.app.command_for(task, None, resume_id="thread", include_goal=True)
        normal_cmd = self.app.command_for(task, None, resume_id="thread", include_goal=False)
        self.assertEqual("/goal resume", goal_cmd[-1])
        self.assertEqual("Continue from the previous turn.", normal_cmd[-1])

        yolo_cmd = self.app.command_for({**task, "yolo": True}, None, resume_id="thread", include_goal=False)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", yolo_cmd)
        self.assertNotIn("--ask-for-approval", yolo_cmd)

    def test_cli_yolo_flags_support_new_and_legacy_codex(self):
        modern = mock.Mock(returncode=0, stdout="--dangerously-bypass-approvals-and-sandbox", stderr="")
        legacy = mock.Mock(returncode=0, stdout="--ask-for-approval <never>", stderr="")
        self.app.CODEX_EXEC_YOLO_FLAGS.clear()
        with mock.patch.object(self.app.subprocess, "run", return_value=modern):
            self.assertEqual(
                ["--dangerously-bypass-approvals-and-sandbox"],
                self.app.codex_exec_yolo_flags("modern-codex"),
            )
        self.app.CODEX_EXEC_YOLO_FLAGS.clear()
        with mock.patch.object(self.app.subprocess, "run", return_value=legacy):
            self.assertEqual(
                ["--ask-for-approval", "never"],
                self.app.codex_exec_yolo_flags("legacy-codex"),
            )

    def test_attachment_frontend_uses_structured_appserver_inputs(self):
        static = Path(__file__).resolve().parents[1] / "static"
        app_js = (static / "app.js").read_text(encoding="utf-8")
        conversation = (static / "conversation.js").read_text(encoding="utf-8")
        worker = (static / "chat-worker.js").read_text(encoding="utf-8")
        self.assertIn("[[codex-input:${file.inputType", app_js)
        self.assertIn('modalities.includes("image")', app_js)
        self.assertIn('modalities.includes("audio")', app_js)
        self.assertIn("inputModalities", conversation)
        self.assertIn("localImage|localAudio|mention", worker)
        self.assertIn('mediaKind === "audio"', conversation)

    def test_slash_commands_refresh_selected_conversation(self):
        conversation = (Path(__file__).resolve().parents[1] / "static" / "conversation.js").read_text(encoding="utf-8")
        core = (Path(__file__).resolve().parents[1] / "static" / "core.js").read_text(encoding="utf-8")
        self.assertIn("if (state.selectedId) await refreshSelectedConversation(state.selectedId);", conversation)
        self.assertIn("if (task.id && task.id !== id)", core)
        self.assertIn("localStorage.setItem(\"codex-dashboard-session\", task.id)", core)

    def test_controlled_appserver_approval_waits_for_browser_choice(self):
        task_id, thread_id, server_key = "controlled-approval", "approval-thread", "approval-server"
        self.make_task(task_id)
        self.app.db.execute("UPDATE tasks SET yolo=0 WHERE id=?", (task_id,))

        class Stdin:
            def __init__(self):
                self.data = bytearray()

            def write(self, value):
                self.data.extend(value)

            async def drain(self):
                return None

        class Client:
            def __init__(self):
                self.write_lock = asyncio.Lock()
                self.process = type("Process", (), {"stdin": Stdin()})()

        client = Client()
        self.app.app_servers[server_key] = client
        self.app.app_thread_bindings[task_id] = (server_key, thread_id, "approval-session")
        broadcasts = []

        async def broadcast(_task_id, payload):
            broadcasts.append(payload)
            if payload["type"] == "server_request":
                request = self.app.pending_appserver_requests[payload["request"]["id"]]
                request["future"].set_result({"decision": "acceptForSession"})

        message = {
            "id": 81,
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": thread_id, "turnId": "turn-1", "itemId": "item-1", "command": "touch approved"},
        }
        try:
            with mock.patch.object(self.app, "broadcast_task", new=broadcast):
                asyncio.run(self.app.handle_appserver_server_request(server_key, message))
            response = json.loads(client.process.stdin.data.decode())
            self.assertEqual({"decision": "acceptForSession"}, response["result"])
            self.assertEqual(["server_request", "server_request_resolved"], [item["type"] for item in broadcasts])
            self.assertFalse(self.app.pending_requests_for_task(task_id))
            permissions = self.app.approval_result(
                {"method": "item/permissions/requestApproval", "params": {"permissions": {"network": {"enabled": True}}}},
                self.app.ApprovalResolveIn(decision="acceptForSession"),
            )
            self.assertEqual({"permissions": {"network": {"enabled": True}}, "scope": "session"}, permissions)
            answers = self.app.approval_result(
                {"method": "item/tool/requestUserInput", "params": {"questions": [{"id": "choice"}]}},
                self.app.ApprovalResolveIn(decision="accept", answers={"choice": ["Option A"]}),
            )
            self.assertEqual({"answers": {"choice": {"answers": ["Option A"]}}}, answers)
        finally:
            self.app.app_servers.pop(server_key, None)
            self.app.app_thread_bindings.pop(task_id, None)
            self.app.pending_appserver_requests.clear()

    def test_controlled_approval_ui_is_realtime_and_actionable(self):
        static = Path(__file__).resolve().parents[1] / "static"
        html = (static / "index.html").read_text(encoding="utf-8")
        conversation = (static / "conversation.js").read_text(encoding="utf-8")
        app_js = (static / "app.js").read_text(encoding="utf-8")
        styles = (static / "styles.css").read_text(encoding="utf-8")
        self.assertIn('id="approval-center"', html)
        self.assertIn('"pending_requests": pending_requests_for_task(task_id)', Path(self.app.__file__).read_text(encoding="utf-8"))
        self.assertIn('data-approval-decision="acceptForSession"', conversation)
        self.assertIn('data-approval-decision="decline"', conversation)
        self.assertIn("/approvals/${encodeURIComponent(requestId)}", app_js)
        self.assertIn(".approval-card {", styles)
        self.assertIn('approval-card${questions.length ? " has-questions"', conversation)
        self.assertIn("grid-auto-columns: clamp(78px, 7vw, 92px)", styles)
        self.assertIn("repeat(auto-fit, minmax(min(132px, 100%), 1fr))", styles)

    def test_timeline_uses_cursor_pages_without_overlap(self):
        task_id = f"timeline-{time.time_ns()}"
        self.make_task(task_id, "available")
        self.app.db.execute("UPDATE tasks SET native=0 WHERE id=?", (task_id,))
        session_id = f"session-{task_id}"
        stamp = self.app.now()
        self.app.db.execute(
            "INSERT INTO sessions (id,task_id,status,attempt,command,started_at) VALUES (?,?,?,?,?,?)",
            (session_id, task_id, "succeeded", 1, "codex", stamp),
        )
        for index in range(70):
            self.app.db.execute(
                "INSERT INTO events (session_id,ts,stream,payload) VALUES (?,?,?,?)",
                (session_id, f"2026-08-16T00:00:{index:02d}+00:00", "stdout", json.dumps({"text": str(index)})),
            )
        first = asyncio.run(self.app.task_timeline(task_id, limit=25, _=None))
        second = asyncio.run(self.app.task_timeline(task_id, before=first["next_cursor"], limit=25, _=None))
        self.assertEqual(25, len(first["items"]))
        self.assertEqual(25, len(second["items"]))
        self.assertTrue(first["has_more"])
        self.assertFalse({row["id"] for row in first["items"]} & {row["id"] for row in second["items"]})

    def test_task_websocket_status_is_incremental_patch(self):
        task_id = f"patch-{time.time_ns()}"
        self.make_task(task_id, "running")

        class Socket:
            def __init__(self):
                self.payloads = []

            async def send_json(self, payload):
                self.payloads.append(payload)

        socket = Socket()
        self.app.task_clients[task_id] = {socket}
        asyncio.run(self.app.broadcast_task(task_id, {"type": "task_status", "task": self.app.task_or_404(task_id)}))
        self.assertEqual("task_patch", socket.payloads[0]["type"])
        self.assertNotIn("task", socket.payloads[0])
        self.assertEqual("running", socket.payloads[0]["patch"]["status"])
        self.app.task_clients.pop(task_id, None)

    def test_runtime_metric_events_keep_only_recent_turns(self):
        events = []
        for index in range(5):
            turn_id = f"turn-{index}"
            for event_type in ("userMessage", "agent_delta", "agentMessage"):
                events.append({
                    "stream": "app-server",
                    "payload": json.dumps({"type": event_type, "turn_id": turn_id, "delta": "x"}),
                })
        events.append({"stream": "system", "payload": json.dumps({"type": "commandResult", "turn_id": "turn-4"})})
        result = self.app.runtime_metric_events(events)
        self.assertTrue(result)
        self.assertTrue(all(event["stream"] == "metrics" for event in result))
        turn_ids = {json.loads(event["payload"])["turn_id"] for event in result}
        self.assertEqual({"turn-2", "turn-3", "turn-4"}, turn_ids)

    def test_recent_chat_events_drop_token_deltas(self):
        events = [
            {"payload": json.dumps({"type": "userMessage", "text": "one"})},
            {"payload": json.dumps({"type": "agent_delta", "delta": "partial"})},
            {"payload": json.dumps({"type": "agentMessage", "text": "two"})},
            {"payload": json.dumps({"type": "codex", "method": "noise"})},
        ]
        result = self.app.recent_chat_events(events)
        self.assertEqual(["userMessage", "agentMessage"], [json.loads(event["payload"])["type"] for event in result])

    def test_writer_detection_and_stale_file(self):
        if os.name == "nt":
            self.skipTest("Windows does not expose portable process file-descriptor metadata")
        path = Path(self.temp.name) / "writer.jsonl"
        path.write_text("{}\n", encoding="utf-8")
        with path.open("a", encoding="utf-8"):
            self.assertIn(os.getpid(), self.app.rollout_writer_pids(str(path), refresh=True))
        os.utime(path, (time.time() - 10, time.time() - 10))
        self.assertFalse(self.app.rollout_writer_pids(str(path), refresh=True))
        self.assertFalse(self.app.rollout_is_live(str(path)))

    def test_dashboard_appserver_descendant_is_not_an_external_writer(self):
        client = SimpleNamespace(process=SimpleNamespace(pid=110, returncode=None))
        self.app.app_servers["writer-tree-test"] = client
        try:
            with mock.patch.object(self.app, "rollout_writer_pids", return_value={110, 111, 900}), \
                 mock.patch.object(self.app, "process_tree_pids", return_value={110, 111}):
                writers = self.app.native_rollout_writer_pids("/tmp/dashboard-writer", refresh=True)
            self.assertEqual({900}, writers)
        finally:
            self.app.app_servers.pop("writer-tree-test", None)

    def test_task_appserver_closes_after_task_id_is_canonicalized(self):
        old_id = "provisional-appserver-task"
        new_id = "canonical-appserver-thread"
        client = SimpleNamespace(close=mock.AsyncMock())
        key = f"default:task:{old_id}"
        self.app.app_servers[key] = client
        self.app.app_thread_bindings[new_id] = (key, new_id, "session-id")
        try:
            asyncio.run(self.app.close_task_appserver(None, {"id": new_id}, client))
            self.assertNotIn(key, self.app.app_servers)
            self.assertNotIn(new_id, self.app.app_thread_bindings)
            client.close.assert_awaited_once()
        finally:
            self.app.app_servers.pop(key, None)
            self.app.app_thread_bindings.pop(new_id, None)

    def test_launch_refreshes_terminal_owner_before_reserving_dashboard(self):
        task_id = "launch-terminal-race"
        self.make_task(task_id, "available")
        try:
            with mock.patch.object(self.app, "require_codex"), \
                 mock.patch.object(self.app, "refresh_live_external_turn", new=mock.AsyncMock(return_value=True)), \
                 mock.patch.object(self.app, "launch_appserver", new=mock.AsyncMock()) as launch_appserver:
                with self.assertRaises(self.app.HTTPException) as raised:
                    asyncio.run(self.app.launch(task_id, "message", "wait for terminal", "message-race"))
            self.assertEqual(409, raised.exception.status_code)
            launch_appserver.assert_not_awaited()
            self.assertEqual("available", self.app.task_or_404(task_id)["status"])
        finally:
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_refresh_live_external_turn_closes_observer_polling_gap(self):
        task_id = "refresh-terminal-race"
        turn_id = "terminal-race-turn"
        self.make_task(task_id, "available")
        boundary = {
            "timestamp": self.app.now(),
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": turn_id},
        }
        try:
            with mock.patch.object(self.app, "native_rollout_path", return_value="/tmp/terminal-race.jsonl"), \
                 mock.patch.object(self.app, "inspect_rollout_boundary", return_value=(100, boundary, "running command")), \
                 mock.patch.object(self.app, "native_rollout_writer_pids", return_value={900}):
                detected = asyncio.run(self.app.refresh_live_external_turn(task_id))
            self.assertTrue(detected)
            self.assertEqual("terminal", self.app.task_or_404(task_id)["execution_source"])
            self.assertEqual(turn_id, self.app.task_or_404(task_id)["execution_turn_id"])
            self.assertIn(turn_id, self.app.external_turn_sets[task_id])
        finally:
            self.app.clear_external_turns(task_id)
            self.app.db.execute("DELETE FROM events WHERE task_id=?", (task_id,))
            self.app.db.execute("DELETE FROM sessions WHERE task_id=?", (task_id,))
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_stale_started_turn_becomes_failed(self):
        task_id = "stale-thread"
        self.make_task(task_id)
        path = Path(self.temp.name) / "stale.jsonl"
        record = {"timestamp": self.app.now(), "type": "event_msg", "payload": {"type": "task_started"}}
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        os.utime(path, (time.time() - 10, time.time() - 10))
        asyncio.run(self.app.reconcile_initial_rollout(task_id, task_id, str(path), record, ""))
        self.assertEqual("failed", self.app.task_or_404(task_id)["status"])
        self.assertNotIn(task_id, self.app.external_turns)

    def test_live_started_turn_remains_running(self):
        task_id = "live-thread"
        self.make_task(task_id, "available")
        path = Path(self.temp.name) / "live.jsonl"
        record = {"timestamp": self.app.now(), "type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-1"}}
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        with path.open("a", encoding="utf-8"):
            asyncio.run(self.app.reconcile_initial_rollout(task_id, task_id, str(path), record, "working"))
        self.assertEqual("running", self.app.task_or_404(task_id)["status"])
        self.assertEqual(str(path), self.app.external_turns[task_id]["path"])
        self.assertEqual(self.app.external_turns[task_id]["session_id"], self.app.task_or_404(task_id)["active_session_id"])

    def test_conversation_resume_does_not_activate_goal_on_terminal_turn(self):
        task_id = f"external-goal-{time.time_ns()}"
        self.make_task(task_id, "available")
        self.app.db.execute(
            "UPDATE tasks SET goal='finish it',goal_status='active',retry_forever=1 WHERE id=?",
            (task_id,),
        )
        self.app.register_external_turn(task_id, {
            "thread_id": task_id,
            "turn_id": "terminal-turn",
            "started_at": self.app.now(),
            "session_id": f"external:{task_id}:terminal-turn",
            "path": "/tmp/live-terminal-rollout",
        })
        try:
            result = asyncio.run(self.app.resume_task(task_id, None))
            self.assertTrue(result["shared"])
            self.assertEqual("running", result["status"])
            self.assertEqual("terminal", result["run_mode"])
            self.assertEqual("terminal", result["execution_source"])
            self.app.persist_external_task_status(task_id, dashboard_active=False)
            self.assertEqual("terminal", self.app.task_or_404(task_id)["run_mode"])
        finally:
            self.app.clear_external_turns(task_id)

    def test_adopted_terminal_goal_continues_when_turn_finishes(self):
        task_id = f"external-goal-drain-{time.time_ns()}"
        self.make_task(task_id, "available")
        self.app.db.execute(
            "UPDATE tasks SET goal='finish it',goal_status='active',retry_forever=1,run_mode='goal_resume' WHERE id=?",
            (task_id,),
        )
        with mock.patch.object(self.app, "launch", new=mock.AsyncMock(return_value={"session_id": "next"})) as launch:
            asyncio.run(self.app.drain_task_messages(task_id))
        launch.assert_awaited_once_with(task_id, "goal-resume")
        self.assertEqual("queued", self.app.task_or_404(task_id)["status"])

    def test_paused_or_blocked_goal_auto_resumes_when_retry_is_enabled(self):
        for status in ("paused", "blocked"):
            task_id = f"external-goal-{status}-{time.time_ns()}"
            self.make_task(task_id, "available")
            self.app.db.execute(
                "UPDATE tasks SET goal='finish it',goal_status=?,retry_forever=1,run_mode='goal_resume' WHERE id=?",
                (status, task_id),
            )
            try:
                with mock.patch.object(self.app, "launch", new=mock.AsyncMock(return_value={"session_id": "next"})) as launch:
                    asyncio.run(self.app.drain_task_messages(task_id))
                task = self.app.task_or_404(task_id)
                self.assertEqual(status, task["goal_status"])
                launch.assert_awaited_once_with(task_id, "goal-resume")
                self.assertEqual("queued", task["status"])
                self.assertTrue(self.app.goal_auto_resume_enabled(task))
                self.assertEqual(status, self.app.goal_status_for_resume(task))
            finally:
                self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_paused_or_blocked_goal_stays_stopped_when_retry_forever_is_off(self):
        for status in ("paused", "blocked"):
            task = {"goal": "finish it", "goal_status": status, "retry_forever": 0}
            self.assertFalse(self.app.goal_auto_resume_enabled(task))
            self.assertFalse(self.app.task_retry_allowed(task))
            self.assertEqual(status, self.app.goal_status_for_resume(task))

    def test_retry_policy_ignores_legacy_retry_counters(self):
        disabled = {
            "goal": "finish it",
            "goal_status": "active",
            "retry_forever": 0,
            "retry_count": 0,
            "max_retries": 100,
        }
        enabled = {**disabled, "retry_forever": 1, "retry_count": 999, "max_retries": 0}
        self.assertFalse(self.app.task_retry_allowed(disabled))
        self.assertTrue(self.app.task_retry_allowed(enabled))

    def test_task_api_hides_legacy_retry_counters(self):
        task_id = f"hidden-retry-counters-{time.time_ns()}"
        self.make_task(task_id, "available")
        try:
            detail = asyncio.run(self.app.get_task(task_id, None))
            summary = self.app.task_summary(task_id)
            self.assertNotIn("retry_count", detail)
            self.assertNotIn("max_retries", detail)
            self.assertNotIn("retry_count", summary)
            self.assertNotIn("max_retries", summary)
        finally:
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_external_turn_disappearance_blocks_active_goal_without_auto_retry(self):
        task_id = f"external-goal-stopped-{time.time_ns()}"
        self.make_task(task_id, "running")
        self.app.db.execute(
            "UPDATE tasks SET goal='finish it',goal_status='active',retry_forever=0 WHERE id=?",
            (task_id,),
        )
        external = self.app.register_external_turn(task_id, {
            "thread_id": task_id,
            "turn_id": "terminal-turn",
            "started_at": self.app.now(),
            "session_id": f"external:{task_id}:terminal-turn",
            "path": "/tmp/missing-terminal-rollout",
        })
        try:
            asyncio.run(self.app.settle_inactive_external_turn(
                task_id,
                "Codex process ended without a completion event",
                {**external, "session_id": ""},
            ))
            result = self.app.task_or_404(task_id)
            self.assertEqual("failed", result["status"])
            self.assertEqual("blocked", result["goal_status"])
            self.assertFalse(result["retry_forever"])
        finally:
            self.app.clear_external_turns(task_id)
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_external_turn_disappearance_blocks_and_schedules_recovery_with_auto_retry(self):
        task_id = f"external-goal-retry-{time.time_ns()}"
        self.make_task(task_id, "running")
        self.app.db.execute(
            "UPDATE tasks SET goal='finish it',goal_status='active',retry_forever=1 WHERE id=?",
            (task_id,),
        )
        external = self.app.register_external_turn(task_id, {
            "thread_id": task_id, "turn_id": "terminal-turn", "started_at": self.app.now(),
            "session_id": f"external:{task_id}:terminal-turn", "path": "/tmp/missing-terminal-rollout",
        })
        try:
            with mock.patch.object(self.app, "schedule_task_drain") as schedule:
                asyncio.run(self.app.settle_inactive_external_turn(
                    task_id, "Codex process ended without a completion event", {**external, "session_id": ""},
                ))
            result = self.app.task_or_404(task_id)
            self.assertEqual("failed", result["status"])
            self.assertEqual("blocked", result["goal_status"])
            self.assertTrue(result["retry_forever"])
            self.assertEqual("recovering", result["interaction"]["goalAction"])
            schedule.assert_called_once_with(task_id)
        finally:
            self.app.clear_external_turns(task_id)
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_explicit_external_turn_abort_pauses_goal_and_disables_retry(self):
        task_id = f"external-goal-abort-{time.time_ns()}"
        turn_id = "terminal-abort-turn"
        self.make_task(task_id, "running")
        self.app.db.execute(
            "UPDATE tasks SET goal='finish it',goal_status='active',retry_forever=1 WHERE id=?",
            (task_id,),
        )
        session_id = f"external:{task_id}:{turn_id}"
        self.app.register_external_turn(task_id, {
            "thread_id": task_id, "turn_id": turn_id, "started_at": self.app.now(),
            "session_id": session_id, "path": "/tmp/aborted-terminal-rollout",
        })
        self.app.db.execute(
            "INSERT INTO sessions (id,task_id,status,attempt,provider_id,command,started_at,codex_session_id) VALUES (?,?,?,?,?,?,?,?)",
            (session_id, task_id, "running", 1, None, "terminal", self.app.now(), task_id),
        )
        record = {
            "timestamp": self.app.now(), "type": "event_msg",
            "payload": {"type": "turn_aborted", "turn_id": turn_id, "reason": "interrupted"},
        }
        try:
            with mock.patch.object(self.app, "schedule_task_drain") as schedule:
                asyncio.run(self.app.apply_external_turn_boundary(task_id, task_id, record))
            result = self.app.task_or_404(task_id)
            self.assertEqual("stopped", result["status"])
            self.assertEqual("paused", result["goal_status"])
            self.assertFalse(result["retry_forever"])
            self.assertEqual("start", result["interaction"]["goalAction"])
            schedule.assert_called_once_with(task_id)
        finally:
            self.app.clear_external_turns(task_id)
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_non_user_failures_have_no_automatic_goal_pause_path(self):
        source = Path(self.app.__file__).read_text(encoding="utf-8")
        self.assertNotIn("pause_goal_after_execution_stops(", source)

    def test_compaction_helper_waits_for_completion_notification(self):
        thread_id = "context-window-thread"

        class Client:
            async def request(inner_self, method, params):
                self.assertEqual("thread/compact/start", method)
                self.assertEqual({"threadId": thread_id}, params)
                asyncio.get_running_loop().call_soon(
                    self.app.compaction_waiters[thread_id].set_result,
                    {"threadId": thread_id},
                )
                return {"started": True}

        result = asyncio.run(self.app.compact_codex_thread(Client(), thread_id))
        self.assertEqual({"started": True}, result)
        self.assertNotIn(thread_id, self.app.compaction_waiters)

    def test_compaction_helper_reports_failed_compaction_turn(self):
        thread_id = "failed-compaction-thread"

        class Client:
            async def request(inner_self, method, params):
                asyncio.get_running_loop().call_soon(
                    asyncio.create_task,
                    self.app.handle_appserver_notification("test", {
                        "method": "turn/completed",
                        "params": {
                            "threadId": thread_id,
                            "turn": {
                                "status": "failed",
                                "error": {"codexErrorInfo": "contextWindowExceeded"},
                            },
                        },
                    }),
                )
                return {}

        with self.assertRaisesRegex(RuntimeError, "contextWindowExceeded"):
            asyncio.run(self.app.compact_codex_thread(Client(), thread_id))
        self.assertNotIn(thread_id, self.app.compaction_waiters)

    def test_overfull_remote_compaction_retries_with_local_compactor(self):
        source = Path(self.app.__file__).read_text(encoding="utf-8")
        self.assertIn('"--disable", "remote_compaction_v2", "app-server", "--stdio"', source)
        self.assertIn("not is_context_window_error(exc)", source)
        self.assertIn('"already has an active writer" not in str(candidate)', source)
        self.assertIn("retrying the same client can never observe the lock release", source)
        self.assertIn("Release that writer before starting the local-compactor process", source)

    def test_context_window_failure_compacts_and_continues_active_goal_without_auto_retry(self):
        source = Path(self.app.__file__).read_text(encoding="utf-8")
        self.assertIn("is_context_window_error(error)", source)
        self.assertIn("should_retry = context_recovered or", source)
        self.assertIn('run_mode == "goal_resume"', source)

    def test_context_window_error_accepts_codex_camel_case_payload(self):
        self.assertTrue(self.app.is_context_window_error('{"codexErrorInfo":"contextWindowExceeded"}'))
        self.assertTrue(self.app.is_context_window_error("Codex ran out of room in the model's context window"))
        self.assertTrue(self.app.is_context_window_error("context_window_exceeded"))
        self.assertFalse(self.app.is_context_window_error("network timeout"))

    def test_thread_token_usage_without_turn_id_survives_timeline_projection(self):
        event = {
            "id": 99,
            "task_id": "token-usage-thread",
            "stream": "app-server",
            "ts": self.app.now(),
            "payload": json.dumps({
                "type": "codex",
                "method": "thread/tokenUsage/updated",
                "params": {"tokenUsage": {"last": {"inputTokens": 4200}, "modelContextWindow": 16000}},
            }),
        }
        rows = self.app.runtime_metric_events([event], 3)
        self.assertEqual(1, len(rows))
        self.assertEqual("metrics", rows[0]["stream"])
        self.assertIn("thread/tokenUsage/updated", rows[0]["payload"])

    def test_token_usage_marks_the_runtime_card_as_compacting(self):
        core = (Path(__file__).resolve().parents[1] / "static" / "core.js").read_text(encoding="utf-8")
        conversation = (Path(__file__).resolve().parents[1] / "static" / "conversation.js").read_text(encoding="utf-8")
        self.assertIn('method === "thread/tokenusage/updated"', core)
        self.assertIn('method === "thread/compacted"', core)
        self.assertIn('livePhases.set(id, phase); renderTurnProgress();', conversation)
        self.assertIn('livePhases.delete(id); renderTurnProgress();', conversation)

    def test_fast_native_timeline_returns_token_usage_metrics(self):
        source = Path(self.app.__file__).read_text(encoding="utf-8")
        self.assertIn('metrics = runtime_metric_events(candidates)', source)
        self.assertIn('"metrics": metrics, "fast": True', source)

    def test_complete_goal_clears_stale_failure(self):
        task_id = "complete-clears-stale-error"
        self.make_task(task_id, "available")
        self.app.db.execute(
            "UPDATE tasks SET goal='done',goal_status='active',last_error='contextWindowExceeded' WHERE id=?",
            (task_id,),
        )
        try:
            self.app.persist_observed_goal_status(task_id, "complete", 12)
            task = self.app.task_or_404(task_id)
            self.assertEqual("", task["last_error"])
            self.assertFalse(task["retry_forever"])
        finally:
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_manual_message_does_not_change_active_goal_state(self):
        task = {"goal": "build the activity galaxy", "goal_status": "active", "retry_forever": 0}
        self.assertEqual("active", self.app.goal_status_for_turn(task, "message"))
        self.assertEqual("active", self.app.goal_status_for_turn(task, "goal_resume"))

    def test_goal_turn_checks_queued_messages_before_continuing(self):
        source = Path(self.app.__file__).read_text(encoding="utf-8")
        marker = 'queued_message = db.one(\n                    "SELECT id FROM task_messages WHERE task_id=? AND status=\'queued\''
        self.assertIn(marker, source)
        self.assertIn("if queued_message:\n                    break\n                goal_task", source)
        self.assertIn('remote_goal_status = "paused" if run_mode == "goal_resume"', source)
        self.assertIn("goal_staging_tasks.add(task[\"id\"])", source)
        self.assertIn("stale_objective or staging_pause", source)
        self.assertIn('{"threadId": thread_id, "objective": goal_task["goal"], "status": "active"}', source)

    def test_normal_queue_submission_stays_fifo_during_active_dashboard_goal(self):
        task_id = f"goal-queue-steer-{time.time_ns()}"
        self.make_task(task_id, "running")
        self.app.db.execute(
            "UPDATE tasks SET goal='finish it',goal_status='active',execution_source='dashboard' WHERE id=?",
            (task_id,),
        )
        steered = {"id": "goal-queue-message", "status": "steered"}
        try:
            with (
                mock.patch.object(self.app, "dashboard_owns_task", return_value=True),
                mock.patch.object(self.app, "steer_task_message", new=mock.AsyncMock(return_value=steered)) as steer,
                mock.patch.object(self.app, "schedule_task_drain") as schedule,
            ):
                result = asyncio.run(self.app.enqueue_task_message(task_id, "guide the current work", "goal-queue-message", "queue"))
            self.assertEqual("queued", result["status"])
            steer.assert_not_awaited()
            schedule.assert_called_once_with(task_id)
        finally:
            self.app.db.execute("DELETE FROM task_messages WHERE id='goal-queue-message'")
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_manual_message_cannot_activate_paused_goal(self):
        task = {"goal": "build the activity galaxy", "goal_status": "paused", "retry_forever": 1}
        self.assertEqual("paused", self.app.goal_status_for_turn(task, "message"))

    def test_goal_cannot_be_paused_before_auto_resume_is_disabled(self):
        task_id = f"goal-pause-order-{time.time_ns()}"
        self.make_task(task_id, "available")
        self.app.db.execute(
            "UPDATE tasks SET goal='finish it',goal_status='active',retry_forever=1 WHERE id=?",
            (task_id,),
        )
        with self.assertRaises(self.app.HTTPException) as raised:
            asyncio.run(self.app.patch_goal(task_id, self.app.GoalPatch(status="paused"), None))
        self.assertEqual(409, raised.exception.status_code)
        self.assertEqual("active", self.app.task_or_404(task_id)["goal_status"])

    def test_goal_pause_control_is_disabled_when_auto_resume_is_enabled(self):
        source = Path(self.app.__file__).with_name("static").joinpath("conversation.js").read_text(encoding="utf-8")
        self.assertIn('goalActionDisabledReason: goalAction === "pause" && retryOn ? "disable_retry" : ""', source)
        self.assertIn('$("#goal-run-toggle").disabled = !controls.goalActionEnabled;', source)
        self.assertIn('uiLabel("goalPauseRetryEnabled")', source)

    def test_completed_goal_can_render_as_a_start_button(self):
        source = Path(self.app.__file__).with_name("static").joinpath("conversation.js").read_text(encoding="utf-8")
        self.assertIn('goalStatus === "complete" ? "restart"', source)
        self.assertIn('restart: "restartGoal"', source)

    def test_completed_goal_can_be_started_again(self):
        task_id = f"restart-completed-goal-{time.time_ns()}"
        self.make_task(task_id, "available")
        self.app.db.execute(
            "UPDATE tasks SET goal='run it again',goal_status='complete' WHERE id=?",
            (task_id,),
        )
        launched = {"id": task_id, "status": "running", "goal_status": "active", "run_mode": "goal_resume"}
        try:
            with (
                mock.patch.object(self.app, "sync_current_goal", new=mock.AsyncMock(return_value="")),
                mock.patch.object(self.app, "launch", new=mock.AsyncMock(return_value=launched)) as launch,
                mock.patch.object(self.app, "broadcast_task", new=mock.AsyncMock()),
                mock.patch.object(self.app, "broadcast_overview", new=mock.AsyncMock()),
            ):
                result = asyncio.run(self.app.start_goal(task_id, None))
            self.assertEqual("active", self.app.task_or_404(task_id)["goal_status"])
            launch.assert_awaited_once_with(task_id, "goal-resume")
            self.assertEqual("goal_resume", result["run_mode"])
        finally:
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_quick_session_switch_does_not_wait_for_global_refresh(self):
        source = Path(self.app.__file__).with_name("static").joinpath("settings.js").read_text(encoding="utf-8")
        start = source.index("async function createQuickSession()")
        end = source.index("function setFormBusy", start)
        flow = source[start:end]
        self.assertNotIn("await refresh(false)", flow)
        self.assertIn("mergeTask(created)", flow)
        self.assertIn("await selectSession(created.id)", flow)

    def test_quick_session_creation_does_not_wait_for_overview_clients(self):
        source = Path(self.app.__file__).read_text(encoding="utf-8")
        start = source.index("async def create_quick_task")
        end = source.index("@app.get(\"/api/tasks/{task_id}\")", start)
        flow = source[start:end]
        self.assertIn('asyncio.create_task(broadcast_overview(task_id, {"type": "created", "quick": True}))', flow)
        self.assertNotIn('await broadcast_overview(task_id, {"type": "created", "quick": True})', flow)

    def test_enabling_auto_resume_starts_a_paused_goal(self):
        task_id = f"goal-retry-enable-{time.time_ns()}"
        self.make_task(task_id, "available")
        self.app.db.execute(
            "UPDATE tasks SET goal='finish it',goal_status='paused',retry_forever=0 WHERE id=?",
            (task_id,),
        )
        started = {**self.app.task_or_404(task_id), "goal_status": "active", "retry_forever": True}
        with mock.patch.object(self.app, "start_goal", new=mock.AsyncMock(return_value=started)) as start:
            result = asyncio.run(self.app.patch_task(task_id, self.app.TaskPatch(retry_forever=True), None))
        self.assertTrue(result["retry_forever"])
        self.assertEqual("active", result["goal_status"])
        start.assert_awaited_once_with(task_id, None)

    def test_appserver_retry_waits_for_previous_turn_cleanup(self):
        source = Path(self.app.__file__).read_text(encoding="utf-8")
        self.assertIn("async def launch_after_turn_cleanup", source)
        self.assertIn('asyncio.create_task(launch_after_turn_cleanup(task["id"], "goal-resume"', source)
        self.assertNotIn('await launch(task["id"], "goal-resume", "", "", set())', source)

    def test_stop_clears_unowned_running_state(self):
        task_id = "orphan-thread"
        self.make_task(task_id)
        self.app.db.execute("UPDATE tasks SET run_mode='goal_resume',last_error='old error' WHERE id=?", (task_id,))
        result = asyncio.run(self.app.stop_task_run(task_id))
        self.assertEqual("stopped", result["status"])
        self.assertEqual("", result["run_mode"])
        self.assertIsNone(result["active_session_id"])
        self.assertEqual("", result["last_error"])

    def test_user_stop_of_external_turn_is_not_recorded_as_failure(self):
        task_id = "external-user-stop"
        self.make_task(task_id, "running")
        external = {
            "thread_id": task_id,
            "turn_id": "external-turn",
            "session_id": "external-session",
            "path": str(Path(self.temp.name) / "external.jsonl"),
        }
        self.app.db.execute(
            "INSERT INTO sessions (id,task_id,status,attempt,command,started_at) VALUES (?,?,?,?,?,?)",
            (external["session_id"], task_id, "running", 1, "external", self.app.now()),
        )
        self.app.register_external_turn(task_id, external)
        try:
            with mock.patch.object(self.app, "interrupt_rollout_writers"), \
                 mock.patch.object(self.app, "broadcast_task", new=mock.AsyncMock()), \
                 mock.patch.object(self.app, "broadcast_overview", new=mock.AsyncMock()):
                result = asyncio.run(self.app.stop_task_run(task_id))
            self.assertEqual("stopped", result["status"])
            self.assertIsNone(result["active_session_id"])
            self.assertEqual("", result["last_error"])
            session = self.app.db.one("SELECT status FROM sessions WHERE id=?", (external["session_id"],))
            self.assertEqual("stopped", session["status"])
        finally:
            self.app.clear_external_turns(task_id)
            self.app.db.execute("DELETE FROM sessions WHERE task_id=?", (task_id,))
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_one_task_stays_running_until_all_terminal_turns_finish(self):
        task_id = "shared-thread"
        self.make_task(task_id, "available")
        path = str(Path(self.temp.name) / "shared.jsonl")
        first = {"timestamp": self.app.now(), "type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-a"}}
        second = {"timestamp": self.app.now(), "type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-b"}}
        asyncio.run(self.app.apply_external_turn_boundary(task_id, task_id, first, path=path))
        asyncio.run(self.app.apply_external_turn_boundary(task_id, task_id, second, path=path))
        self.assertEqual(2, len(self.app.external_turn_sets[task_id]))
        self.assertEqual("running", self.app.task_or_404(task_id)["status"])

        first_done = {"timestamp": self.app.now(), "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-a"}}
        asyncio.run(self.app.apply_external_turn_boundary(task_id, task_id, first_done, path=path))
        self.assertEqual("running", self.app.task_or_404(task_id)["status"])
        self.assertEqual("turn-b", self.app.task_or_404(task_id)["external_turn_id"])

        second_done = {"timestamp": self.app.now(), "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-b"}}
        asyncio.run(self.app.apply_external_turn_boundary(task_id, task_id, second_done, path=path))
        self.assertEqual("available", self.app.task_or_404(task_id)["status"])
        self.assertNotIn(task_id, self.app.external_turn_sets)

    def test_matching_dashboard_turn_is_kept_when_terminal_writer_exists(self):
        task_id = "shared-surface-thread"
        self.make_task(task_id, "running", "dashboard-session")
        self.app.running[task_id] = object()
        self.app.appserver_turn_ids[task_id] = "turn-shared"
        record = {
            "timestamp": self.app.now(),
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "turn-shared"},
        }

        class OverviewClient:
            def __init__(self):
                self.messages = []

            async def send_json(self, payload):
                self.messages.append(payload)

        client = OverviewClient()
        self.app.overview_clients.add(client)
        try:
            with mock.patch.object(self.app, "native_rollout_writer_pids", return_value={12345}):
                asyncio.run(self.app.apply_external_turn_boundary(task_id, task_id, record, path="/tmp/shared-rollout"))
            self.assertIn("turn-shared", self.app.external_turn_sets[task_id])
            task = self.app.task_or_404(task_id)
            self.assertEqual("running", task["status"])
            self.assertEqual("mixed", task["execution_source"])
            self.assertTrue(any(message.get("type") == "task_status" for message in client.messages))

            response = {
                "timestamp": self.app.now(),
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "id": "terminal-agent-message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "terminal live reply"}],
                    "internal_chat_message_metadata_passthrough": {"turn_id": "turn-shared"},
                },
            }
            asyncio.run(self.app.process_native_rollout_record(task_id, task_id, response, "/tmp/shared-rollout"))
            event = self.app.db.one("SELECT payload FROM events WHERE session_id=? ORDER BY id DESC LIMIT 1", (f"external:{task_id}:turn-shared",))
            self.assertIn("terminal live reply", event["payload"])
        finally:
            self.app.overview_clients.discard(client)
            self.app.running.pop(task_id, None)
            self.app.appserver_turn_ids.pop(task_id, None)
            self.app.clear_external_turns(task_id)

    def test_dashboard_only_matching_turn_is_not_misclassified_as_terminal(self):
        task_id = "dashboard-only-thread"
        self.make_task(task_id, "running")
        self.app.running[task_id] = object()
        self.app.appserver_turn_ids[task_id] = "turn-dashboard"
        record = {
            "timestamp": self.app.now(),
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "turn-dashboard"},
        }
        try:
            with mock.patch.object(self.app, "native_rollout_writer_pids", return_value=set()):
                asyncio.run(self.app.apply_external_turn_boundary(task_id, task_id, record, path="/tmp/dashboard-rollout"))
            self.assertNotIn(task_id, self.app.external_turn_sets)
        finally:
            self.app.running.pop(task_id, None)
            self.app.appserver_turn_ids.pop(task_id, None)

    def test_persisted_dashboard_turn_is_not_listed_as_external_session(self):
        task_id = "persisted-dashboard-thread"
        dashboard_session = "persisted-dashboard-session"
        stale_session = "persisted-dashboard-stale-session"
        external_session = f"external:{task_id}:turn-dashboard-persisted"
        turn_id = "turn-dashboard-persisted"
        stamp = self.app.now()
        self.make_task(task_id, "running")
        try:
            self.app.db.execute(
                "INSERT INTO sessions (id,task_id,status,attempt,command,started_at,codex_session_id) VALUES (?,?,?,?,?,?,?)",
                (dashboard_session, task_id, "succeeded", 2, "codex app-server", stamp, task_id),
            )
            self.app.db.execute(
                "INSERT INTO sessions (id,task_id,status,attempt,command,started_at,codex_session_id,summary) VALUES (?,?,?,?,?,?,?,?)",
                (stale_session, task_id, "interrupted", 1, "codex app-server", stamp, task_id, "Dashboard is restarting"),
            )
            self.app.db.execute(
                "INSERT INTO events (session_id,task_id,ts,stream,payload) VALUES (?,?,?,?,?)",
                (dashboard_session, task_id, stamp, "app-server", json.dumps({"type": "userMessage", "turn_id": turn_id, "text": "hello"})),
            )
            record = {
                "timestamp": stamp,
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": turn_id},
            }
            with mock.patch.object(self.app, "native_rollout_writer_pids", return_value=set()):
                asyncio.run(self.app.apply_external_turn_boundary(task_id, task_id, record, path="/tmp/dashboard-persisted-rollout"))
            self.assertIsNone(self.app.db.one("SELECT id FROM sessions WHERE id=?", (external_session,)))
            self.assertTrue(self.app.has_persisted_dashboard_turn(task_id, turn_id))
            self.app.db.execute(
                "INSERT INTO sessions (id,task_id,status,attempt,command,started_at,codex_session_id) VALUES (?,?,?,?,?,?,?)",
                (external_session, task_id, "running", 0, "codex resume", stamp, task_id),
            )
            session_ids = {row["id"] for row in asyncio.run(self.app.list_sessions())}
            self.assertIn(dashboard_session, session_ids)
            self.assertNotIn(stale_session, session_ids)
            self.assertNotIn(external_session, session_ids)
        finally:
            self.app.clear_external_turns(task_id)
            self.app.db.execute("DELETE FROM events WHERE task_id=?", (task_id,))
            self.app.db.execute("DELETE FROM sessions WHERE task_id=?", (task_id,))
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_duplicate_message_from_two_transports_is_collapsed(self):
        task_id = "dedupe-thread"
        payload = {"type": "agentMessage", "text": "same reply", "item_id": "agent-1"}
        self.assertFalse(self.app.duplicate_live_event(task_id, payload))
        self.assertTrue(self.app.duplicate_live_event(task_id, payload))

    def test_user_message_started_event_is_ignored_and_worker_collapses_duplicates(self):
        task_id = "image-dedupe-thread"
        thread_id = "thread-image-dedupe"
        session_id = "session-image-dedupe"
        server_key = "server-image-dedupe"
        turn_id = "turn-image-dedupe"
        message_id = "message-image-dedupe"
        self.make_task(task_id)
        self.app.db.execute(
            "INSERT INTO sessions (id,task_id,status,attempt,command,started_at) VALUES (?,?,?,?,?,?)",
            (session_id, task_id, "running", 1, "codex app-server", self.app.now()),
        )
        self.app.app_thread_bindings[task_id] = (server_key, thread_id, session_id)

        async def capture(_task_id, _payload):
            return None

        started = {
            "method": "item/started",
            "params": {
                "threadId": thread_id,
                "turnId": turn_id,
                "item": {
                    "type": "userMessage",
                    "id": "item-user-1",
                    "clientId": message_id,
                    "content": [{"type": "text", "text": "look at this"}],
                },
            },
        }
        completed = {
            "method": "item/completed",
            "params": {
                "threadId": thread_id,
                "turnId": turn_id,
                "item": {
                    "type": "userMessage",
                    "id": "item-user-1",
                    "clientId": message_id,
                    "content": [{"type": "text", "text": "look at this"}],
                },
            },
        }

        try:
            with mock.patch.object(self.app, "duplicate_live_event", return_value=False), \
                 mock.patch.object(self.app, "broadcast_task", new=mock.AsyncMock(side_effect=capture)):
                asyncio.run(self.app.handle_appserver_notification(server_key, started))
                asyncio.run(self.app.handle_appserver_notification(server_key, completed))
            rows = self.app.db.all("SELECT payload FROM events WHERE session_id=? ORDER BY id", (session_id,))
            self.assertEqual(1, len(rows))
            self.assertEqual("userMessage", json.loads(rows[0]["payload"])["type"])

            script = f"""
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync({json.dumps(str(Path(__file__).resolve().parents[1] / "static/chat-worker.js"))}, "utf8");
const context = {{ self: {{ postMessage() {{}} }} }};
vm.createContext(context);
vm.runInContext(source, context);
const mergeEvents = context.mergeEvents || context.self.mergeEvents;
if (!mergeEvents) throw new Error("mergeEvents missing");
const merged = mergeEvents(
  [
    {{ id: "event-1", stream: "app-server", payload: JSON.stringify({{ type: "userMessage", text: "look at this", item_id: "item-user-1", client_message_id: "{message_id}" }}) }},
    {{ id: "event-2", stream: "app-server", payload: JSON.stringify({{ type: "userMessage", text: "look at this", item_id: "item-user-2", client_message_id: "{message_id}" }}) }},
    {{ id: "event-3", stream: "rollout", payload: JSON.stringify({{ type: "userMessage", text: "look at this\\n<image name=[Image #1] path=\\"/tmp/image.png\\">\\n</image>", item_id: "item-user-3" }}) }}
  ],
  [
    {{ id: "{message_id}", body: "look at this", status: "sent", created_at: "2026-08-17T00:00:00Z" }}
  ]
);
process.stdout.write(JSON.stringify(merged.map(item => JSON.parse(item.payload).type)));
"""
            result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(["userMessage"], json.loads(result.stdout))
        finally:
            self.app.app_thread_bindings.pop(task_id, None)
            self.app.db.execute("DELETE FROM events WHERE session_id=?", (session_id,))
            self.app.db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_goal_updated_syncs_objective_and_worker_hides_internal_goal_context(self):
        task_id = "goal-objective-sync-thread"
        thread_id = "thread-goal-objective-sync"
        session_id = "session-goal-objective-sync"
        server_key = "server-goal-objective-sync"
        self.make_task(task_id)
        self.app.db.execute("UPDATE tasks SET goal='old objective', goal_status='active' WHERE id=?", (task_id,))
        self.app.db.execute(
            "INSERT INTO sessions (id,task_id,status,attempt,command,started_at) VALUES (?,?,?,?,?,?)",
            (session_id, task_id, "running", 1, "codex app-server", self.app.now()),
        )
        self.app.app_thread_bindings[task_id] = (server_key, thread_id, session_id)
        notification = {
            "method": "thread/goal/updated",
            "params": {
                "threadId": thread_id,
                "goal": {
                    "objective": "new objective",
                    "status": "active",
                    "tokensUsed": 42,
                },
            },
        }
        try:
            with mock.patch.object(self.app, "duplicate_live_event", return_value=False), \
                 mock.patch.object(self.app, "broadcast_task", new=mock.AsyncMock()):
                asyncio.run(self.app.handle_appserver_notification(server_key, notification))
            task = self.app.task_or_404(task_id)
            self.assertEqual("new objective", task["goal"])
            self.assertEqual("active", task["goal_status"])
            self.assertEqual(42, task["goal_tokens_used"])

            self.app.db.execute(
                "UPDATE tasks SET goal='dashboard objective',goal_status='active',goal_tokens_used=7,goal_revision=1 WHERE id=?",
                (task_id,),
            )
            notification["params"]["goal"] = {
                "objective": "old objective",
                "status": "complete",
                "tokensUsed": 99,
            }
            with mock.patch.object(self.app, "duplicate_live_event", return_value=False), \
                 mock.patch.object(self.app, "broadcast_task", new=mock.AsyncMock()):
                asyncio.run(self.app.handle_appserver_notification(server_key, notification))
            guarded = self.app.task_or_404(task_id)
            self.assertEqual("dashboard objective", guarded["goal"])
            self.assertEqual("active", guarded["goal_status"])
            self.assertEqual(7, guarded["goal_tokens_used"])

            notification["params"]["goal"] = {
                "objective": "dashboard objective",
                "status": "paused",
                "tokensUsed": 8,
            }
            self.app.goal_staging_tasks.add(task_id)
            with mock.patch.object(self.app, "duplicate_live_event", return_value=False), \
                 mock.patch.object(self.app, "broadcast_task", new=mock.AsyncMock()):
                asyncio.run(self.app.handle_appserver_notification(server_key, notification))
            staged = self.app.task_or_404(task_id)
            self.assertEqual("active", staged["goal_status"])
            self.assertEqual(8, staged["goal_tokens_used"])
            self.app.goal_staging_tasks.discard(task_id)

            script = f"""
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync({json.dumps(str(Path(__file__).resolve().parents[1] / "static/chat-worker.js"))}, "utf8");
const context = {{ self: {{ postMessage() {{}} }} }};
vm.createContext(context);
vm.runInContext(source, context);
const buildBlocks = context.buildBlocks || context.self.buildBlocks;
if (!buildBlocks) throw new Error("buildBlocks missing");
const blocks = buildBlocks([
  {{ id: "goal-context", stream: "rollout", payload: JSON.stringify({{ type: "userMessage", text: "<codex_internal_context source=\\"goal\\">\\n<objective>old objective</objective>\\n</codex_internal_context>" }}) }},
  {{ id: "goal-update", stream: "app-server", payload: JSON.stringify({{ type: "goal_updated", goal: {{ objective: "old objective" }} }}) }},
  {{ id: "real-user", stream: "app-server", payload: JSON.stringify({{ type: "userMessage", text: "visible user message" }}) }}
], false, {{}});
process.stdout.write(JSON.stringify(blocks.map(block => block.text)));
"""
            result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(["visible user message"], json.loads(result.stdout))
        finally:
            self.app.goal_staging_tasks.discard(task_id)
            self.app.app_thread_bindings.pop(task_id, None)
            self.app.db.execute("DELETE FROM events WHERE session_id=?", (session_id,))
            self.app.db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_native_goal_update_cannot_overwrite_a_newer_dashboard_revision(self):
        task_id = "native-goal-revision-thread"
        self.make_task(task_id, "available")
        self.app.db.execute(
            "UPDATE tasks SET goal='dashboard objective',goal_status='active',goal_revision=1,goal_updated_at=?,updated_at=? WHERE id=?",
            ("2026-08-18T06:00:00+00:00", "2026-08-18T06:00:00+00:00", task_id),
        )
        old_record = {
            "timestamp": "2026-08-18T05:59:00+00:00",
            "type": "event_msg",
            "payload": {"type": "thread_goal_updated", "goal": {
                "objective": "old terminal objective", "status": "complete", "tokensUsed": 99,
            }},
        }
        new_record = {
            "timestamp": "2026-08-18T06:01:00+00:00",
            "type": "event_msg",
            "payload": {"type": "thread_goal_updated", "goal": {
                "objective": "new terminal objective", "status": "active", "tokensUsed": 3,
            }},
        }
        try:
            asyncio.run(self.app.process_native_rollout_record(task_id, task_id, old_record))
            guarded = self.app.task_or_404(task_id)
            self.assertEqual("dashboard objective", guarded["goal"])
            self.assertEqual(1, guarded["goal_revision"])

            asyncio.run(self.app.process_native_rollout_record(task_id, task_id, new_record))
            updated = self.app.task_or_404(task_id)
            self.assertEqual("new terminal objective", updated["goal"])
            self.assertEqual(2, updated["goal_revision"])
            self.assertEqual(3, updated["goal_tokens_used"])
        finally:
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_auto_resume_reloads_goal_before_starting_turn(self):
        source = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
        self.assertIn('task = task_or_404(task["id"])\n        async with goal_sync_lock(task["id"]):', source)
        self.assertIn('async with goal_sync_lock(task["id"]):\n            task = task_or_404(task["id"])\n            goal_revision_at_start', source)
        self.assertIn('"objective": task["goal"]', source)
        self.assertIn('include_goal_memory=run_mode == "goal_resume"', source)
        self.assertIn('continue_existing=mode in {"resume", "auto-retry"}', source)
        self.assertIn('await launch(task_id, "goal-resume")', source)
        self.assertIn('"goal-resume" if goal_auto_resume_enabled(latest)', source)

    def test_worker_hides_codex_environment_context_from_user_messages(self):
        worker = Path(__file__).resolve().parents[1] / "static" / "chat-worker.js"
        hidden_context = json.dumps("<environment_context>\n<current_date>2026-08-18</current_date>\n</environment_context>")
        mixed_context = json.dumps("keep this request\n<environment_context>\n<timezone>Asia/Shanghai</timezone>\n</environment_context>")
        internal_context = json.dumps('<codex_internal_context source="system">secret</codex_internal_context>')
        permissions = json.dumps("<permissions instructions>hidden</permissions instructions>")
        answer = json.dumps("visible answer\n<oai-mem-citation><rollout_ids>hidden</rollout_ids></oai-mem-citation>")
        delta_start = json.dumps("after\n<environment_context><current_date>")
        delta_end = json.dumps("hidden</current_date></environment_context>")
        script = f"""
const fs = require("fs");
const vm = require("vm");
const context = {{ self: {{ postMessage() {{}} }} }};
vm.createContext(context);
vm.runInContext(fs.readFileSync({json.dumps(str(worker))}, "utf8"), context);
const blocks = context.buildBlocks([
  {{ id: "only-hidden", stream: "rollout", payload: JSON.stringify({{ type: "userMessage", text: {hidden_context} }}) }},
  {{ id: "mixed", stream: "rollout", payload: JSON.stringify({{ type: "userMessage", text: {mixed_context} }}) }},
  {{ id: "internal", stream: "rollout", payload: JSON.stringify({{ type: "userMessage", text: {internal_context} }}) }},
  {{ id: "permissions", stream: "rollout", payload: JSON.stringify({{ type: "userMessage", text: {permissions} }}) }},
  {{ id: "answer", stream: "rollout", payload: JSON.stringify({{ type: "agentMessage", text: {answer} }}) }},
  {{ id: "delta-1", stream: "rollout", payload: JSON.stringify({{ type: "agent_delta", item_id: "split", delta: {delta_start} }}) }},
  {{ id: "delta-2", stream: "rollout", payload: JSON.stringify({{ type: "agent_delta", item_id: "split", delta: {delta_end} }}) }},
], false, {{}});
process.stdout.write(JSON.stringify(blocks.map(block => block.text)));
"""
        result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(["keep this request", "visible answer", "after"], json.loads(result.stdout))

        conversation = (worker.parent / "conversation.js").read_text(encoding="utf-8")
        html = (worker.parent / "index.html").read_text(encoding="utf-8")
        self.assertIn('/chat-worker.js?v=20260831-focus-wide', conversation)
        self.assertIn('/conversation.js?v=20260831-focus-wide', html)

    def test_worker_keeps_distinct_agent_items_in_separate_markdown_blocks(self):
        worker = Path(__file__).resolve().parents[1] / "static" / "chat-worker.js"
        first = "远端节点没有目标目录输出。"
        final = """可以，已经按这个方向改了：

- `sglang-omni` 里已恢复：
  ```python
  torch.load(codec_path, map_location=device)
  ```
- `torchada` 新增设备映射。"""
        script = f"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const staticRoot = {json.dumps(str(worker.parent))};
const context = {{ console, atob, self: {{ postMessage() {{}} }} }};
vm.createContext(context);
context.importScripts = (...urls) => {{
  for (const url of urls) vm.runInContext(fs.readFileSync(path.join(staticRoot, url), "utf8"), context, {{ filename: url }});
}};
vm.runInContext(fs.readFileSync({json.dumps(str(worker))}, "utf8"), context);
const blocks = context.buildBlocks([
  {{ id: "first", stream: "app-server", payload: JSON.stringify({{ type: "agentMessage", item_id: "agent-1", text: {json.dumps(first)} }}) }},
  {{ id: "delta-1", stream: "app-server", payload: JSON.stringify({{ type: "agent_delta", item_id: "agent-2", delta: "可以，" }}) }},
  {{ id: "delta-2", stream: "app-server", payload: JSON.stringify({{ type: "agent_delta", item_id: "agent-2", delta: "已经按这个方向改了：" }}) }},
  {{ id: "final", stream: "app-server", payload: JSON.stringify({{ type: "agentMessage", item_id: "agent-2", text: {json.dumps(final)} }}) }},
], false, {{}});
process.stdout.write(JSON.stringify(blocks.map(block => ({{ itemId: block.itemId, text: block.text, html: block.html }}))));
"""
        result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True, encoding="utf-8")
        blocks = json.loads(result.stdout)
        self.assertEqual(["agent-1", "agent-2"], [block["itemId"] for block in blocks])
        self.assertEqual(first, blocks[0]["text"])
        self.assertEqual(final, blocks[1]["text"])
        self.assertEqual(1, blocks[1]["html"].count("<pre>"))
        self.assertEqual(1, blocks[1]["html"].count("</pre>"))

    def test_worker_hides_native_media_tags(self):
        script = f"""
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync({json.dumps(str(Path(__file__).resolve().parents[1] / "static/chat-worker.js"))}, "utf8");
const context = {{ self: {{ postMessage() {{}} }} }};
vm.createContext(context);
vm.runInContext(source, context);
const buildBlocks = context.buildBlocks || context.self.buildBlocks;
if (!buildBlocks) throw new Error("buildBlocks missing");
const blocks = buildBlocks([
  {{ id: "media", stream: "rollout", payload: JSON.stringify({{ type: "userMessage", text: "see this\\n<image name=[Image #1] path=\\"/home/ori/work/image-mswtyzp4.png\\">\\n</image>" }}) }}
], false, {{}});
process.stdout.write(JSON.stringify(blocks[0].html));
"""
        result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True, encoding="utf-8")
        html = json.loads(result.stdout)
        self.assertNotIn("<image", html)
        self.assertNotIn("</image>", html)
        self.assertNotIn("/home/ori/work", html)
        self.assertIn('data-chat-file="image-mswtyzp4.png"', html)
        self.assertIn("chat-image-attachment", html)

    def test_chat_markdown_supports_tables_math_and_safe_rich_blocks(self):
        static = Path(__file__).resolve().parents[1] / "static"
        source = """# Result

| Name | Value |
|:--|--:|
| alpha | 42 |

Inline $E=mc^2$.

Bracket inline \\(a^2+b^2=c^2\\).

$$\\sum_{i=1}^{n} i = \\frac{n(n+1)}{2}$$

\\[\\prod_{j=1}^{m} j = m!\\]

> quoted text

1. first
   - nested

~~removed~~

```python
print("ok")
```

<script>alert(1)</script>

[safe](https://example.com) [unsafe](javascript:alert(1))

[local log](/home/ori/work/result.log)
"""
        script = f"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const staticRoot = {json.dumps(str(static))};
const context = {{ console, atob, self: {{ postMessage() {{}} }} }};
vm.createContext(context);
context.importScripts = (...urls) => {{
  for (const url of urls) vm.runInContext(fs.readFileSync(path.join(staticRoot, url), "utf8"), context, {{ filename: url }});
}};
vm.runInContext(fs.readFileSync(path.join(staticRoot, "chat-worker.js"), "utf8"), context);
process.stdout.write(JSON.stringify(context.markdown({json.dumps(source)})));
"""
        result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True, encoding="utf-8")
        html = json.loads(result.stdout)
        self.assertIn('<div class="markdown-table-wrap"><table>', html)
        self.assertIn('style="text-align:right"', html)
        self.assertEqual(4, html.count('class="katex"'))
        self.assertIn("<blockquote>", html)
        self.assertIn("<ol>", html)
        self.assertIn("<ul>", html)
        self.assertIn("<s>removed</s>", html)
        self.assertIn('class="language-python"', html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn("<script>", html)
        self.assertIn('target="_blank" rel="noopener noreferrer"', html)
        self.assertNotIn('href="javascript:', html)
        self.assertIn('href="#" data-chat-file="/home/ori/work/result.log" data-chat-kind="mention"', html)

        encoded_path = "home/ori/jd/0826/qwen3.5-35b-a3b-en2cn-2000-c5%20%E6%80%A7%E8%83%BD%E5%88%86%E6%9E%90/EN2CN_2000_C5_H200_TP4_REPORT.md"
        encoded_html_script = f"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const staticRoot = {json.dumps(str(static))};
const context = {{ console, atob, self: {{ postMessage() {{}} }} }};
vm.createContext(context);
context.importScripts = (...urls) => {{
  for (const url of urls) vm.runInContext(fs.readFileSync(path.join(staticRoot, url), "utf8"), context, {{ filename: url }});
}};
vm.runInContext(fs.readFileSync(path.join(staticRoot, "chat-worker.js"), "utf8"), context);
process.stdout.write(context.markdown({json.dumps(f'[report]({encoded_path})')}));
"""
        encoded_result = subprocess.run(["node", "-e", encoded_html_script], check=True, capture_output=True, text=True, encoding="utf-8")
        encoded_html = encoded_result.stdout
        self.assertIn('data-chat-file="/home/ori/jd/0826/qwen3.5-35b-a3b-en2cn-2000-c5 性能分析/EN2CN_2000_C5_H200_TP4_REPORT.md"', encoded_html)
        self.assertNotIn("%20", encoded_html)

        for relative in (
            "vendor/markdown-it/markdown-it.min.js",
            "vendor/markdown-it/LICENSE",
            "vendor/markdown-it-texmath/texmath.js",
            "vendor/markdown-it-texmath/LICENSE",
            "vendor/katex/katex.min.js",
            "vendor/katex/katex.min.css",
            "vendor/katex/LICENSE",
            "vendor/katex/fonts/KaTeX_Main-Regular.woff2",
        ):
            self.assertTrue((static / relative).is_file(), relative)

    def test_worker_merges_live_activity_start_and_completion(self):
        worker = Path(__file__).resolve().parents[1] / "static/chat-worker.js"
        script = f"""
const fs = require("fs");
const vm = require("vm");
const context = {{ self: {{ postMessage() {{}} }} }};
vm.createContext(context);
vm.runInContext(fs.readFileSync({json.dumps(str(worker))}, "utf8"), context);
const blocks = context.buildBlocks([
  {{ stream: "app-server", payload: JSON.stringify({{ type: "commandExecution", text: "python3 -m unittest", command: "python3 -m unittest", item_id: "command-1", status: "started" }}) }},
  {{ stream: "app-server", payload: JSON.stringify({{ type: "commandExecution", text: "python3 -m unittest", item_id: "command-1", status: "completed" }}) }}
], true, {{ activityCommand: "running", activityCommandDone: "done", activityWorking: "working" }});
process.stdout.write(JSON.stringify(blocks));
"""
        result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True, encoding="utf-8")
        blocks = json.loads(result.stdout)
        self.assertEqual(1, len(blocks))
        self.assertEqual(1, len(blocks[0]["items"]))
        self.assertEqual("completed", blocks[0]["items"][0]["status"])
        self.assertEqual("done · python3 -m unittest", blocks[0]["items"][0]["text"])

    def test_worker_completion_does_not_erase_started_activity_details(self):
        worker = Path(__file__).resolve().parents[1] / "static/chat-worker.js"
        script = f"""
const fs = require("fs");
const vm = require("vm");
const context = {{ self: {{ postMessage() {{}} }} }};
vm.createContext(context);
vm.runInContext(fs.readFileSync({json.dumps(str(worker))}, "utf8"), context);
const blocks = context.buildBlocks([
  {{ stream: "app-server", payload: {{ type: "commandExecution", item_id: "command-1", command: "python3 -m unittest", tool: "exec_command", arguments: "{{}}", status: "started" }} }},
  {{ stream: "app-server", payload: {{ type: "commandExecution", item_id: "command-1", status: "completed" }} }}
], true, {{ activityCommand: "running", activityCommandDone: "done", activityWorking: "working" }});
process.stdout.write(JSON.stringify(blocks));
"""
        result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True, encoding="utf-8")
        item = json.loads(result.stdout)[0]["items"][0]
        self.assertEqual("python3 -m unittest", item["command"])
        self.assertEqual("exec_command", item["tool"])
        self.assertEqual("{}", item["arguments"])
        self.assertEqual("completed", item["status"])

    def test_worker_hides_raw_codex_protocol_but_keeps_normalized_command(self):
        worker = Path(__file__).resolve().parents[1] / "static/chat-worker.js"
        script = f"""
const fs = require("fs");
const vm = require("vm");
const context = {{ self: {{ postMessage() {{}} }} }};
vm.createContext(context);
vm.runInContext(fs.readFileSync({json.dumps(str(worker))}, "utf8"), context);
const blocks = context.buildBlocks([
  {{ stream: "app-server", payload: JSON.stringify({{ type: "codex", method: "turn/diff/updated", params: {{}} }}) }},
  {{ stream: "app-server", payload: JSON.stringify({{ type: "commandExecution", text: "python3 -m unittest", item_id: "command-1", status: "started" }}) }},
  {{ stream: "app-server", payload: JSON.stringify({{ type: "codex", method: "item/commandExecution/outputDelta", params: {{ itemId: "command-1", delta: "partial output" }} }}) }},
  {{ stream: "app-server", payload: JSON.stringify({{ type: "codex", method: "thread/tokenUsage/updated", params: {{}} }}) }},
  {{ stream: "app-server", payload: JSON.stringify({{ type: "commandExecution", text: "python3 -m unittest", command: "python3 -m unittest", item_id: "command-1", status: "completed", output: "OK" }}) }}
], true, {{ activityCommand: "running", activityCommandDone: "done", activityWorking: "working" }});
process.stdout.write(JSON.stringify(blocks));
"""
        result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True, encoding="utf-8")
        blocks = json.loads(result.stdout)
        self.assertEqual(1, len(blocks))
        self.assertEqual(1, len(blocks[0]["items"]))
        self.assertEqual("completed", blocks[0]["items"][0]["status"])
        self.assertEqual("python3 -m unittest", blocks[0]["items"][0]["command"])
        self.assertEqual("OK", blocks[0]["items"][0]["output"])
        self.assertNotIn("outputDelta", json.dumps(blocks))

    def test_worker_builds_key_activity_tree_without_command_nodes(self):
        worker = Path(__file__).resolve().parents[1] / "static/chat-worker.js"
        script = f"""
const fs = require("fs");
const vm = require("vm");
const context = {{ self: {{ postMessage() {{}} }} }};
vm.createContext(context);
vm.runInContext(fs.readFileSync({json.dumps(str(worker))}, "utf8"), context);
const labels = {{ activityPlanning: "planning", activityCommandDone: "done", activityWorking: "working" }};
const events = [
  {{ id: "user-1", ts: 1, stream: "user", payload: {{ type: "userMessage", text: "帮我修复会话切换时消息丢失的问题", item_id: "user-1", turn_id: "turn-1" }} }},
  {{ id: "plan-1", ts: 2, stream: "app-server", payload: {{ type: "planUpdate", item_id: "plan-1", turn_id: "turn-1", plan: [{{ step: "排查 timeline 缓存边界", status: "in_progress" }}, {{ step: "修复并验证消息恢复", status: "pending" }}] }} }},
  {{ id: "command-1", ts: 3, stream: "app-server", payload: {{ type: "commandExecution", item_id: "command-1", turn_id: "turn-1", command: "rg timeline static", status: "completed", exit_code: 0 }} }},
  {{ id: "plan-2", ts: 4, stream: "app-server", payload: {{ type: "planUpdate", item_id: "plan-2", turn_id: "turn-1", plan: [{{ step: "排查 timeline 缓存边界", status: "completed" }}, {{ step: "修复并验证消息恢复", status: "in_progress" }}] }} }},
  {{ id: "user-2", ts: 5, stream: "user", payload: {{ type: "userMessage", text: "不是前端缓存，改成检查后端分页游标", item_id: "user-2", turn_id: "turn-2" }} }}
];
const visibleBlocks = context.buildBlocks(events, false, labels);
const semanticBlocks = context.buildBlocks(events, true, labels);
const decisionBlocks = context.buildBlocks([events[0], {{ id: "reason-1", ts: 2, stream: "rollout", payload: {{ type: "reasoning", item_id: "reason-1", turn_id: "turn-1", text: "确认根因是后端分页游标没有推进" }} }}], true, labels);
process.stdout.write(JSON.stringify({{ visibleBlocks, nodes: context.buildExplorationTree(semanticBlocks, true), failed: context.buildExplorationTree(semanticBlocks, false, "failed"), stopped: context.buildExplorationTree(semanticBlocks, false, "stopped"), decision: context.buildExplorationTree(decisionBlocks, true) }}));
"""
        result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True, encoding="utf-8")
        data = json.loads(result.stdout)
        nodes = data["nodes"]
        self.assertEqual(4, len(nodes))
        self.assertEqual(["direction", "plan", "plan", "steering"], [node["kind"] for node in nodes])
        self.assertEqual(1, sum(node["status"] == "active" for node in nodes))
        self.assertEqual("abandoned", nodes[2]["status"])
        self.assertEqual(nodes[1]["id"], nodes[3]["parentId"])
        self.assertEqual(["rg timeline static"], nodes[1]["commands"])
        self.assertEqual([], nodes[2]["commands"])
        self.assertFalse(any(node["kind"] == "command" for node in nodes))
        self.assertFalse(any(block["role"] == "activities" for block in data["visibleBlocks"]))
        self.assertEqual("failed", data["failed"][-1]["status"])
        self.assertEqual("planned", data["stopped"][-1]["status"])
        self.assertEqual(["direction", "decision"], [node["kind"] for node in data["decision"]])
        self.assertIn("确认根因", data["decision"][-1]["title"])

    def test_exploration_map_is_separate_from_chat_and_incremental(self):
        static = Path(__file__).resolve().parents[1] / "static"
        html = (static / "index.html").read_text(encoding="utf-8")
        conversation = (static / "conversation.js").read_text(encoding="utf-8")
        tree = (static / "exploration-tree.js").read_text(encoding="utf-8")
        styles = (static / "styles.css").read_text(encoding="utf-8")
        self.assertIn('id="exploration-map-open"', html)
        self.assertIn('id="exploration-map"', html)
        self.assertIn('/exploration-tree.js?v=20260818-thread-ops-i18n', html)
        self.assertIn("explorationNodes: []", (static / "core.js").read_text(encoding="utf-8"))
        self.assertIn("event.data.explorationNodes", conversation)
        self.assertIn("!state.explorationPrecomputed && state.explorationNeedsSync", conversation)
        self.assertIn("const explorationCache = new Map()", (static / "chat-worker.js").read_text(encoding="utf-8"))
        self.assertIn("function explorationLayout", tree)
        self.assertIn("function loadPrecomputedActivityMap", tree)
        self.assertIn("/activity-map", tree)
        self.assertNotIn("timeline?limit=500", tree)
        self.assertIn("position.depth * 244", tree)
        self.assertIn("layout.maxDepth * 244", tree)
        self.assertIn("V ${endY} H ${endX}", tree)
        self.assertIn("data-exploration-jump", tree)
        self.assertIn('addEventListener("wheel"', tree)
        self.assertIn('addEventListener("pointermove"', tree)
        self.assertIn("suppressExplorationClick", tree)
        pointer_down = tree.split('addEventListener("pointerdown"', 1)[1].split('addEventListener("pointermove"', 1)[0]
        self.assertNotIn("setPointerCapture", pointer_down)
        self.assertGreater(tree.index("setPointerCapture"), tree.index("Math.hypot(dx, dy) < 4"))
        self.assertIn("updateExplorationZoom", tree)
        self.assertIn('id="exploration-zoom-reset"', html)
        self.assertIn('id="exploration-zoom-fit"', html)
        self.assertIn("function frameExplorationBranch", tree)
        self.assertIn("frameExplorationNodes([parent.id", tree)
        self.assertIn("explorationFramePending = true", tree)
        self.assertIn("explorationStatusActive:", (static / "core.js").read_text(encoding="utf-8"))
        self.assertIn('uiLabel("explorationLegend")', tree)
        self.assertIn('window.addEventListener("languagechange"', tree)
        self.assertIn('id="exploration-map-open-label"', html)
        self.assertNotIn('content: "● 进行中', styles)
        self.assertIn(".exploration-map-world", styles)
        self.assertIn(".exploration-map-viewport", styles)
        self.assertIn(".exploration-node-time", styles)
        self.assertIn('id="exploration-map-eyebrow"', html)
        self.assertNotIn("setInterval", tree)

    def test_exploration_worker_reuses_full_history_projection(self):
        worker = Path(__file__).resolve().parents[1] / "static/chat-worker.js"
        script = f"""
const fs = require("fs");
const vm = require("vm");
const posts = [];
const context = {{ self: {{ postMessage(value) {{ posts.push(value); }} }} }};
vm.createContext(context);
vm.runInContext(fs.readFileSync({json.dumps(str(worker))}, "utf8"), context);
const events = [{{ id: "user-1", ts: 1, stream: "user", payload: {{ type: "userMessage", text: "帮我排查完整历史缓存问题", item_id: "user-1", turn_id: "turn-1" }} }}];
context.self.onmessage({{ data: {{ requestId: 1, taskId: "task-1", events, explorationEvents: events, explorationRevision: 1, messages: [], rawActivity: true, labels: {{}}, taskRunning: true, taskStatus: "running" }} }});
context.self.onmessage({{ data: {{ requestId: 2, taskId: "task-1", events: [], explorationEvents: null, explorationRevision: 1, messages: [], rawActivity: true, labels: {{}}, taskRunning: true, taskStatus: "running" }} }});
process.stdout.write(JSON.stringify(posts));
"""
        result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True, encoding="utf-8")
        posts = json.loads(result.stdout)
        self.assertEqual(2, len(posts))
        self.assertEqual(posts[0]["explorationNodes"], posts[1]["explorationNodes"])
        self.assertEqual("direction", posts[1]["explorationNodes"][0]["kind"])

    def test_activity_graph_is_persisted_and_incremental_events_are_idempotent(self):
        from codex_partner.activity_graph import project_events

        task_id = f"activity-graph-{time.time_ns()}"
        self.make_task(task_id)
        initial = [{
            "id": "user-1", "ts": "2026-08-18T01:00:00+00:00", "stream": "native",
            "payload": {"type": "userMessage", "text": "帮我实现持久化活动图索引", "item_id": "user-1", "turn_id": "turn-1"},
        }]
        nodes, seen = project_events(initial, task_running=True, task_status="running")
        self.assertEqual(["direction"], [node["kind"] for node in nodes])
        self.app.activity_graph_store.mark_building(task_id, self.app.now())
        self.app.activity_graph_store.replace(task_id, nodes, seen, len(initial), self.app.now())

        failed_command = {
            "id": "command-1", "ts": "2026-08-18T01:01:00+00:00", "stream": "app-server",
            "payload": {"type": "commandExecution", "command": "false", "status": "failed", "exit_code": 1, "item_id": "command-1", "turn_id": "turn-1"},
        }
        first = self.app.activity_graph_store.apply_event(task_id, failed_command, True, "running", self.app.now())
        second = self.app.activity_graph_store.apply_event(task_id, failed_command, True, "running", self.app.now())
        snapshot = self.app.activity_graph_store.snapshot(task_id)
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual("ready", snapshot["status"])
        self.assertEqual(["false"], snapshot["nodes"][0]["commands"])
        self.assertEqual(1, snapshot["nodes"][0]["failures"])

        status_patch = self.app.activity_graph_store.apply_status(task_id, False, "available", self.app.now())
        self.assertEqual("completed", status_patch["upsert_nodes"][0]["status"])
        api_snapshot = asyncio.run(self.app.task_activity_map(task_id, _=None))
        self.assertEqual("completed", api_snapshot["nodes"][0]["status"])

    def test_activity_graph_frontend_never_scans_full_timeline(self):
        root = Path(__file__).resolve().parents[1]
        app_source = (root / "app.py").read_text(encoding="utf-8")
        tree = (root / "static" / "exploration-tree.js").read_text(encoding="utf-8")
        conversation = (root / "static" / "conversation.js").read_text(encoding="utf-8")
        self.assertIn('"/api/tasks/{task_id}/activity-map"', app_source)
        self.assertIn("seed_activity_graph_builds()", app_source)
        self.assertIn("schedule_activity_graph_event(task_id, overview_source)", app_source)
        self.assertNotIn("timeline?limit=500", tree)
        self.assertIn("activity_map_patch", conversation)
        self.assertIn("applyActivityMapSnapshot(activityMap)", conversation)
        self.assertIn("function explorationLayout(nodes, graphEdges = [])", tree)
        self.assertIn("exploration-galaxy-lane", tree)

    def test_activity_graph_plans_form_semantic_branches(self):
        from codex_partner.activity_graph import project_events

        events = [
            {"id": "u1", "ts": 1, "stream": "native", "payload": {"type": "userMessage", "text": "帮我实现完整活动树", "item_id": "u1", "turn_id": "t1"}},
            {"id": "p1", "ts": 2, "stream": "native", "payload": {"type": "plan", "turn_id": "t1", "plan": [
                {"step": "分析数据模型", "status": "completed"},
                {"step": "实现树形布局", "status": "in_progress"},
                {"step": "验证分支交互", "status": "pending"},
            ]}},
            {"id": "u2", "ts": 3, "stream": "native", "payload": {"type": "userMessage", "text": "继续增加节点搜索和状态筛选", "item_id": "u2", "turn_id": "t2"}},
            {"id": "p2", "ts": 4, "stream": "native", "payload": {"type": "plan", "turn_id": "t2", "plan": [
                {"step": "增加节点搜索", "status": "in_progress"},
                {"step": "增加状态筛选", "status": "pending"},
            ]}},
            {"id": "u3", "ts": 5, "stream": "native", "payload": {"type": "userMessage", "text": "不是这个方向，改成先优化树的分叉逻辑", "item_id": "u3", "turn_id": "t3"}},
        ]
        nodes, _ = project_events(events, task_running=True, task_status="running")
        directions = [node for node in nodes if node["kind"] in {"direction", "steering"}]
        first, second, steering = directions
        children = {}
        for node in nodes:
            children.setdefault(node.get("parentId"), []).append(node)
        self.assertEqual(first["id"], second["parentId"])
        self.assertEqual(first["id"], steering["parentId"])
        first_phase = next(node for node in nodes if node["kind"] == "phase" and node["turnId"] == "t1")
        second_phase = next(node for node in nodes if node["kind"] == "phase" and node["turnId"] == "t2")
        self.assertEqual(3, len(children[first["id"]]))
        self.assertEqual([second_phase], children[second["id"]])
        self.assertEqual(3, len(children[first_phase["id"]]))
        self.assertEqual(2, len(children[second_phase["id"]]))

    def test_activity_graph_reconnects_related_topics_instead_of_flattening_time(self):
        from codex_partner.activity_graph import project_edges, project_events

        def user(event_id, text):
            return {"id": event_id, "ts": event_id, "stream": "native", "payload": {
                "type": "userMessage", "text": text, "item_id": event_id, "turn_id": event_id,
            }}

        nodes, _ = project_events([
            user("u1", "帮我修复会话消息分页缓存"),
            user("u2", "继续排查消息分页游标和缓存恢复"),
            user("u3", "增加 MUSA kernel 性能分析和寄存器检查"),
            user("u4", "消息分页缓存恢复仍然失败，检查分页游标"),
        ], task_running=True, task_status="running")
        directions = [node for node in nodes if node["kind"] == "direction"]
        first, pagination, musa, pagination_return = directions
        self.assertEqual(first["id"], pagination["parentId"])
        self.assertEqual(first["id"], musa["parentId"])
        self.assertEqual(pagination["id"], pagination_return["parentId"])
        edges = project_edges(nodes)
        relation = next(edge for edge in edges if edge["targetId"] == pagination_return["id"] and edge["kind"] == "related")
        self.assertEqual(first["id"], relation["sourceId"])

    def test_activity_graph_persists_typed_edges(self):
        from codex_partner.activity_graph import project_edges, project_events

        task_id = f"activity-edges-{time.time_ns()}"
        self.make_task(task_id)
        events = [
            {"id": "u1", "ts": 1, "stream": "native", "payload": {"type": "userMessage", "text": "帮我实现知识图谱活动地图", "item_id": "u1", "turn_id": "t1"}},
            {"id": "p1", "ts": 2, "stream": "native", "payload": {"type": "plan", "turn_id": "t1", "plan": [{"step": "实现多关系边", "status": "in_progress"}]}},
        ]
        nodes, seen = project_events(events, task_running=True, task_status="running")
        expected = project_edges(nodes)
        self.app.activity_graph_store.mark_building(task_id, self.app.now())
        self.app.activity_graph_store.replace(task_id, nodes, seen, len(events), self.app.now())
        snapshot = self.app.activity_graph_store.snapshot(task_id)
        self.assertEqual(expected, snapshot["edges"])
        self.assertIn("contains", {edge["kind"] for edge in snapshot["edges"]})

    def test_activity_graph_recall_is_hidden_and_query_relevant(self):
        from codex_partner.activity_graph import recall_context

        snapshot = {"nodes": [
            {"id": "map", "kind": "decision", "status": "completed", "title": "使用多关系知识图谱", "summary": "地图支持语义边", "score": 9, "files": ["static/exploration-tree.js"]},
            {"id": "gpu", "kind": "direction", "status": "completed", "title": "分析 MUSA kernel spill", "summary": "检查寄存器", "score": 8},
        ], "edges": []}
        context = recall_context("继续实现探索地图关系", snapshot, "构建星系地图")
        self.assertIn('<memory_context source="codex-partner-activity-graph">', context)
        self.assertIn("使用多关系知识图谱", context)
        self.assertNotIn("分析 MUSA kernel spill", context)
        self.assertEqual("", recall_context("交互修复验证：只回答 OK", snapshot))
        current_snapshot = {"nodes": snapshot["nodes"] + [
            {"id": "current", "kind": "steering", "status": "active", "title": "交互修复验证：只回答 OK", "summary": "当前 turn", "score": 10},
        ], "edges": [{"id": "current-edge", "sourceId": "gpu", "targetId": "current", "kind": "branch", "score": 1}]}
        self.assertEqual("", recall_context("交互修复验证：只回答 OK", current_snapshot))

        task = {"id": "recall-task", "prompt": "", "context": "", "goal": "构建星系地图", "memory_mode": "enabled"}
        with mock.patch.object(self.app.activity_graph_store, "snapshot", return_value=snapshot):
            inputs = self.app.appserver_turn_inputs(task, "继续实现探索地图关系")
        self.assertIn("继续实现探索地图关系", inputs[0]["text"])
        self.assertIn("<memory_context", inputs[0]["text"])

        task["goal"] = "分析 MUSA kernel spill"
        with mock.patch.object(self.app.activity_graph_store, "snapshot", return_value=snapshot):
            manual_inputs = self.app.appserver_turn_inputs(task, "交互修复验证：只回答 OK")
            goal_inputs = self.app.appserver_turn_inputs(task, "继续", include_goal_memory=True)
        self.assertNotIn("<memory_context", manual_inputs[0]["text"])
        self.assertEqual("/goal resume", goal_inputs[0]["text"])
        self.assertNotIn("使用多关系知识图谱", goal_inputs[0]["text"])

    def test_activity_history_expands_independently_from_message_history(self):
        static = Path(__file__).resolve().parents[1] / "static"
        worker = static / "chat-worker.js"
        script = f"""
const fs = require("fs");
const vm = require("vm");
const context = {{ self: {{ postMessage() {{}} }} }};
vm.createContext(context);
vm.runInContext(fs.readFileSync({json.dumps(str(worker))}, "utf8"), context);
const events = Array.from({{ length: 21 }}, (_, index) => ({{
  id: `event-${{index}}`,
  stream: "app-server",
  payload: JSON.stringify({{ type: "commandExecution", command: `command-${{index}}`, item_id: `command-${{index}}`, status: "completed" }})
}}));
const blocks = context.buildBlocks(events, true, {{ activityCommandDone: "done", activityWorking: "working" }});
process.stdout.write(JSON.stringify(blocks));
"""
        result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True, encoding="utf-8")
        blocks = json.loads(result.stdout)
        self.assertEqual(1, len(blocks))
        self.assertEqual(21, len(blocks[0]["items"]))
        self.assertTrue(blocks[0]["activityKey"].startswith("app-server:event-0"))

        conversation = (static / "conversation.js").read_text(encoding="utf-8")
        core = (static / "core.js").read_text(encoding="utf-8")
        styles = (static / "styles.css").read_text(encoding="utf-8")
        function_start = conversation.index("function loadEarlierActivity")
        function_end = conversation.index("\n}\n", function_start)
        activity_loader = conversation[function_start:function_end]
        self.assertIn("visibleCount + 16", activity_loader)
        self.assertIn("paintVirtualChat(false)", activity_loader)
        self.assertNotIn("loadOlderTimeline", activity_loader)
        self.assertIn('class="activity-load-older"', conversation)
        self.assertIn("data-activity-key", conversation)
        self.assertIn("activityVisibleCounts: {}", core)
        self.assertIn("loadEarlierActivity", core)
        self.assertIn(".activity-load-older", styles)
        self.assertIn("function activityEventIsVisible", conversation)
        self.assertIn('String(item?.output || "").trim()', conversation)
        self.assertIn("const displayItems = block.items.filter", conversation)
        self.assertIn("<small>${displayItems.length}</small>", conversation)
        self.assertNotIn('if (!command && !plan.length && ["exec", "functions.exec"].includes(toolName))', conversation)

    def test_full_activity_transcript_stays_open_across_chat_repaints(self):
        static = Path(__file__).resolve().parents[1] / "static"
        core = (static / "core.js").read_text(encoding="utf-8")
        conversation = (static / "conversation.js").read_text(encoding="utf-8")
        html = (static / "index.html").read_text(encoding="utf-8")
        self.assertIn("activityOutputOpen: {}", core)
        self.assertIn("state.activityOutputOpen = {}", conversation)
        self.assertIn('data-activity-output-key="${esc(outputKey)}"', conversation)
        self.assertIn("Object.prototype.hasOwnProperty.call(state.activityOutputOpen, outputKey)", conversation)
        self.assertIn("state.activityOutputOpen[output.dataset.activityOutputKey] = output.open", conversation)
        self.assertIn('/conversation.js?v=20260831-focus-wide', html)

    def test_message_history_skips_activity_only_pages(self):
        static = Path(__file__).resolve().parents[1] / "static"
        pagination = static / "history-pagination.js"
        script = f"""
const fs = require("fs");
const vm = require("vm");
const context = {{}};
vm.createContext(context);
vm.runInContext(fs.readFileSync({json.dumps(str(pagination))}, "utf8"), context);
const pages = {{
  newest: {{
    items: Array.from({{ length: 20 }}, (_, index) => ({{ id: `activity-${{index}}`, payload: JSON.stringify({{ type: "commandExecution" }}) }})),
    next_cursor: "middle",
    has_more: true,
  }},
  middle: {{
    items: [
      {{ id: "user-2", payload: JSON.stringify({{ type: "userMessage" }}) }},
      {{ id: "agent-2", payload: JSON.stringify({{ type: "agentMessage" }}) }},
    ],
    next_cursor: "oldest",
    has_more: true,
  }},
  oldest: {{
    items: [
      {{ id: "user-1", payload: {{ type: "browserMessage" }} }},
      {{ id: "agent-1", payload: {{ type: "agentMessage" }} }},
    ],
    next_cursor: "done",
    has_more: false,
  }},
}};
const cursors = [];
(async () => {{
  const result = await context.HistoryPagination.fetchEarlierTimelinePages(async cursor => {{
    cursors.push(cursor);
    return pages[cursor];
  }}, {{ cursor: "newest", messageTarget: 4, maxPages: 8 }});
  process.stdout.write(JSON.stringify({{ result, cursors }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
        result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True, encoding="utf-8")
        payload = json.loads(result.stdout)
        self.assertEqual(["newest", "middle", "oldest"], payload["cursors"])
        self.assertEqual(3, payload["result"]["pages_loaded"])
        self.assertEqual(4, payload["result"]["message_count"])
        self.assertEqual("user-1", payload["result"]["items"][0]["id"])
        self.assertEqual("activity-19", payload["result"]["items"][-1]["id"])

        conversation = (static / "conversation.js").read_text(encoding="utf-8")
        html = (static / "index.html").read_text(encoding="utf-8")
        self.assertIn("HistoryPagination.fetchEarlierTimelinePages", conversation)
        self.assertIn("messageTarget: 12, maxPages: 8", conversation)
        self.assertIn('/history-pagination.js?v=20260817-message-history', html)
        self.assertIn('/conversation.js?v=20260831-focus-wide', html)

    def test_sent_browser_messages_follow_the_loaded_timeline_boundary(self):
        worker = Path(__file__).resolve().parents[1] / "static" / "chat-worker.js"
        script = f"""
const fs = require("fs");
const vm = require("vm");
const context = {{ self: {{ postMessage() {{}} }} }};
vm.createContext(context);
vm.runInContext(fs.readFileSync({json.dumps(str(worker))}, "utf8"), context);
const messages = [
  {{ id: "old", body: "old sent message", status: "sent", created_at: "2026-08-17T09:00:00Z" }},
  {{ id: "current", body: "current sent message", status: "sent", created_at: "2026-08-17T11:00:00Z" }},
  {{ id: "running", body: "currently running message", status: "running", created_at: "2026-08-17T08:00:00Z" }},
];
const recentEvents = [
  {{ id: "recent", ts: "2026-08-17T10:00:00Z", stream: "app-server", payload: JSON.stringify({{ type: "agentMessage", text: "recent reply" }}) }},
];
const olderEvents = [
  {{ id: "older", ts: "2026-08-17T08:30:00Z", stream: "app-server", payload: JSON.stringify({{ type: "agentMessage", text: "older reply" }}) }},
  ...recentEvents,
];
const bodies = events => context.mergeEvents(events, messages)
  .filter(event => event.stream === "user")
  .map(event => (typeof event.payload === "string" ? JSON.parse(event.payload) : event.payload).text);
process.stdout.write(JSON.stringify({{ recent: bodies(recentEvents), older: bodies(olderEvents) }}));
"""
        result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True, encoding="utf-8")
        payload = json.loads(result.stdout)
        self.assertEqual(["currently running message", "current sent message"], payload["recent"])
        self.assertEqual(["currently running message", "old sent message", "current sent message"], payload["older"])

        conversation = (worker.parent / "conversation.js").read_text(encoding="utf-8")
        self.assertIn('/chat-worker.js?v=20260831-focus-wide', conversation)

    def test_metrics_user_event_does_not_hide_its_browser_message(self):
        worker = Path(__file__).resolve().parents[1] / "static" / "chat-worker.js"
        script = f"""
const fs = require("fs");
const vm = require("vm");
const context = {{ self: {{ postMessage() {{}} }} }};
vm.createContext(context);
vm.runInContext(fs.readFileSync({json.dumps(str(worker))}, "utf8"), context);
const messageId = "message-in-metrics";
const merged = context.mergeEvents([
  {{
    id: "history-boundary",
    ts: "2026-08-17T14:00:00Z",
    stream: "native",
    payload: JSON.stringify({{ type: "agentMessage", text: "recent reply" }}),
  }},
  {{
    id: "metric-user",
    ts: "2026-08-17T14:30:00Z",
    stream: "metrics",
    payload: JSON.stringify({{ type: "userMessage", text: "keep this message", client_message_id: messageId }}),
  }},
], [
  {{ id: messageId, body: "keep this message", status: "steered", created_at: "2026-08-17T14:30:00Z" }},
]);
const browserMessages = merged.filter(event => event.stream === "user").map(event => event.payload.text);
process.stdout.write(JSON.stringify(browserMessages));
"""
        result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(["keep this message"], json.loads(result.stdout))

    def test_model_slash_command_supports_named_updates_and_defaults(self):
        task_id = "model-command-thread"
        self.make_task(task_id)
        self.app.db.execute("UPDATE tasks SET model='gpt-5.5', reasoning_effort='low' WHERE id=?", (task_id,))

        class Client:
            async def request(self, method, params):
                raise AssertionError(method)

        async def run(command):
            with mock.patch.object(self.app, "appserver_for_task", new=mock.AsyncMock(return_value=(Client(), None))):
                return await self.app.execute_slash_command(self.app.task_or_404(task_id), "model", "", command, False)

        result = asyncio.run(run(["model=gpt-5.6", "effort=high"]))
        self.assertTrue(result["ok"])
        task = self.app.task_or_404(task_id)
        self.assertEqual("gpt-5.6", task["model"])
        self.assertEqual("high", task["reasoning_effort"])

        result = asyncio.run(run(["effort=default"]))
        self.assertTrue(result["ok"])
        task = self.app.task_or_404(task_id)
        self.assertEqual("gpt-5.6", task["model"])
        self.assertEqual("", task["reasoning_effort"])

        result = asyncio.run(run(["model=default"]))
        self.assertTrue(result["ok"])
        task = self.app.task_or_404(task_id)
        self.assertEqual("", task["model"])
        self.assertEqual("", task["reasoning_effort"])

    def test_streaming_deltas_are_never_collapsed(self):
        task_id = "delta-thread"
        payload = {"type": "agent_delta", "delta": "same fragment", "item_id": "agent-1"}
        self.assertFalse(self.app.duplicate_live_event(task_id, payload))
        self.assertFalse(self.app.duplicate_live_event(task_id, payload))

    def test_running_message_waits_in_durable_queue(self):
        task_id = "steer-thread"
        self.make_task(task_id)

        class FakeClient:
            def __init__(self):
                self.calls = []

            async def request(self, method, params):
                self.calls.append((method, params))
                return {}

        async def exercise():
            client = FakeClient()
            try:
                with mock.patch.object(self.app, "schedule_task_drain") as schedule:
                    result = await self.app.enqueue_task_message(task_id, "follow up", "message-1")
                self.assertEqual("queued", result["status"])
                self.assertEqual([], client.calls)
                schedule.assert_called_once_with(task_id)
            finally:
                self.app.db.execute("DELETE FROM task_messages WHERE id='message-1'")
                self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

        asyncio.run(exercise())

    def test_terminal_message_requires_takeover_confirmation_before_queueing(self):
        task_id = "terminal-confirm-thread"
        self.make_task(task_id, "running")
        self.app.register_external_turn(task_id, {"turn_id": "terminal-turn", "path": "/tmp/terminal-turn.jsonl"})
        try:
            with mock.patch.object(self.app, "refresh_live_external_turn", new=mock.AsyncMock(return_value=False)):
                with self.assertRaises(self.app.HTTPException) as raised:
                    asyncio.run(self.app.post_task_message(
                        task_id,
                        self.app.TaskMessageIn(message="take over", client_message_id="terminal-confirm-message"),
                        None,
                    ))
            self.assertEqual(409, raised.exception.status_code)
            self.assertEqual("terminal_turn_active", raised.exception.detail["code"])
            self.assertIsNone(self.app.db.one("SELECT id FROM task_messages WHERE id='terminal-confirm-message'"))
        finally:
            self.app.clear_external_turns(task_id)
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_terminal_process_cleanup_has_posix_and_windows_paths(self):
        source = inspect.getsource(self.app.close_terminal_session)
        helper = inspect.getsource(self.app.terminate_terminal_process)
        waiter = inspect.getsource(self.app.wait_terminal_process)
        self.assertIn("terminate_terminal_process(pid)", source)
        self.assertIn("IS_WINDOWS", helper)
        self.assertIn("os.killpg", helper)
        self.assertIn("os.kill(pid", helper)
        self.assertIn("waitpid", waiter)
        self.assertIn("os.kill(pid, 0)", waiter)

    def test_confirmed_terminal_takeover_stops_terminal_then_queues_message(self):
        task_id = "terminal-takeover-thread"
        self.make_task(task_id, "running")
        self.app.register_external_turn(task_id, {"turn_id": "terminal-turn", "path": "/tmp/terminal-turn.jsonl"})
        try:
            with mock.patch.object(self.app, "refresh_live_external_turn", new=mock.AsyncMock(return_value=False)), \
                 mock.patch.object(self.app, "stop_terminal_for_web_takeover", new=mock.AsyncMock(return_value=True)) as stop, \
                 mock.patch.object(self.app, "schedule_task_drain") as drain:
                result = asyncio.run(self.app.post_task_message(
                    task_id,
                    self.app.TaskMessageIn(
                        message="take over",
                        client_message_id="terminal-takeover-message",
                        takeover_terminal=True,
                    ),
                    None,
                ))
            stop.assert_awaited_once_with(task_id)
            drain.assert_called_once_with(task_id)
            self.assertEqual("queued", result["status"])
        finally:
            self.app.clear_external_turns(task_id)
            self.app.db.execute("DELETE FROM task_messages WHERE task_id=?", (task_id,))
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_terminal_takeover_clears_external_owner_without_auto_resume_race(self):
        task_id = "terminal-takeover-stop-thread"
        session_id = "external-terminal-takeover-session"
        self.make_task(task_id, "running")
        self.app.db.execute(
            "INSERT INTO sessions (id,task_id,status,attempt,command,started_at,codex_session_id) VALUES (?,?,?,?,?,?,?)",
            (session_id, task_id, "running", 1, "codex resume", self.app.now(), task_id),
        )
        self.app.register_external_turn(task_id, {
            "turn_id": "terminal-turn",
            "session_id": session_id,
            "path": "/tmp/terminal-takeover.jsonl",
            "started_at": self.app.now(),
        })
        try:
            with mock.patch.object(self.app, "refresh_live_external_turn", new=mock.AsyncMock(return_value=False)), \
                 mock.patch.object(self.app, "interrupt_browser_terminal", new=mock.AsyncMock()) as browser_interrupt, \
                 mock.patch.object(self.app, "interrupt_rollout_writers", return_value={900}) as writer_interrupt, \
                 mock.patch.object(self.app, "schedule_task_drain") as drain:
                stopped = asyncio.run(self.app.stop_terminal_for_web_takeover(task_id))
            self.assertTrue(stopped)
            browser_interrupt.assert_awaited_once_with(task_id)
            writer_interrupt.assert_called_once_with("/tmp/terminal-takeover.jsonl")
            drain.assert_not_called()
            self.assertNotIn(task_id, self.app.external_turns)
            self.assertEqual("stopped", self.app.task_or_404(task_id)["status"])
            self.assertEqual("stopped", self.app.db.one("SELECT status FROM sessions WHERE id=?", (session_id,))["status"])
            event = self.app.db.one("SELECT payload FROM events WHERE session_id=? ORDER BY id DESC LIMIT 1", (session_id,))
            self.assertEqual("turn_aborted", json.loads(event["payload"])["type"])
        finally:
            self.app.clear_external_turns(task_id)
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_binary_dump_is_rejected_before_queueing(self):
        task_id = "binary-dump-thread"
        self.make_task(task_id, "available")
        try:
            with self.assertRaisesRegex(Exception, "二进制文件"):
                asyncio.run(self.app.enqueue_task_message(task_id, "[Attachments：image.png]\n�PNG\x00IHDR�IDAT"))
            self.assertIsNone(self.app.db.one("SELECT id FROM task_messages WHERE task_id=?", (task_id,)))
        finally:
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_manual_dispatch_can_steer_the_active_turn(self):
        task_id = "manual-steer-thread"
        message_id = "manual-steer-message"
        self.make_task(task_id)
        stamp = self.app.now()
        self.app.db.execute(
            "INSERT INTO task_messages (id,task_id,body,status,created_at) VALUES (?,?,?,?,?)",
            (message_id, task_id, "send now", "queued", stamp),
        )

        class FakeClient:
            def __init__(self):
                self.calls = []

            async def request(self, method, params):
                self.calls.append((method, params))
                return {}

        async def exercise():
            client = FakeClient()
            blocker = asyncio.create_task(asyncio.Event().wait())
            self.app.app_servers["test"] = client
            self.app.app_thread_bindings[task_id] = ("test", "thread-1", "session-1")
            self.app.appserver_turn_ids[task_id] = "turn-1"
            self.app.appserver_turn_tasks[task_id] = blocker
            try:
                result = await self.app.dispatch_task_message(task_id, message_id, None)
                self.assertEqual("steered", result["status"])
                self.assertEqual("turn/steer", client.calls[0][0])
                self.assertEqual("turn-1", client.calls[0][1]["expectedTurnId"])
            finally:
                blocker.cancel()
                await asyncio.gather(blocker, return_exceptions=True)
                self.app.app_servers.pop("test", None)
                self.app.app_thread_bindings.pop(task_id, None)
                self.app.appserver_turn_ids.pop(task_id, None)
                self.app.appserver_turn_tasks.pop(task_id, None)
                self.app.db.execute("DELETE FROM task_messages WHERE task_id=?", (task_id,))
                self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

        asyncio.run(exercise())

    def test_alt_enter_steer_race_falls_back_to_durable_queue(self):
        task_id = "steer-race-queue-thread"
        message_id = "steer-race-queue-message"
        self.make_task(task_id, "running")
        try:
            with mock.patch.object(self.app, "schedule_task_drain") as drain:
                result = asyncio.run(self.app.steer_task_message(
                    task_id,
                    self.app.TaskMessageIn(message="keep this message", client_message_id=message_id),
                    None,
                ))
            self.assertEqual("queued", result["status"])
            self.assertEqual("keep this message", result["body"])
            drain.assert_called_once_with(task_id)
        finally:
            self.app.db.execute("DELETE FROM task_messages WHERE task_id=?", (task_id,))
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_manual_dispatch_uses_persisted_mixed_turn_id(self):
        task_id = "manual-steer-mixed-thread"
        message_id = "manual-steer-mixed-message"
        self.make_task(task_id)
        stamp = self.app.now()
        self.app.db.execute(
            "UPDATE tasks SET execution_source='mixed',execution_turn_id='turn-current' WHERE id=?",
            (task_id,),
        )
        self.app.db.execute(
            "INSERT INTO task_messages (id,task_id,body,status,created_at) VALUES (?,?,?,?,?)",
            (message_id, task_id, "send now", "queued", stamp),
        )

        class FakeClient:
            def __init__(self):
                self.calls = []

            async def request(self, method, params):
                self.calls.append((method, params))
                return {}

        async def exercise():
            client = FakeClient()
            blocker = asyncio.create_task(asyncio.Event().wait())
            self.app.app_servers["test-mixed"] = client
            self.app.app_thread_bindings[task_id] = ("test-mixed", "thread-1", "session-1")
            self.app.appserver_turn_ids[task_id] = "turn-stale"
            self.app.appserver_turn_tasks[task_id] = blocker
            try:
                result = await self.app.dispatch_task_message(task_id, message_id, None)
                self.assertEqual("steered", result["status"])
                self.assertEqual("turn-current", client.calls[0][1]["expectedTurnId"])
                self.assertEqual("turn-current", self.app.appserver_turn_ids[task_id])
            finally:
                blocker.cancel()
                await asyncio.gather(blocker, return_exceptions=True)
                self.app.app_servers.pop("test-mixed", None)
                self.app.app_thread_bindings.pop(task_id, None)
                self.app.appserver_turn_ids.pop(task_id, None)
                self.app.appserver_turn_tasks.pop(task_id, None)
                self.app.db.execute("DELETE FROM task_messages WHERE task_id=?", (task_id,))
                self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

        asyncio.run(exercise())

    def test_manual_dispatch_reports_steer_error_when_message_remains_queued(self):
        task_id = "manual-steer-error-thread"
        message_id = "manual-steer-error-message"
        self.make_task(task_id)
        stamp = self.app.now()
        self.app.db.execute(
            "INSERT INTO task_messages (id,task_id,body,status,created_at) VALUES (?,?,?,?,?)",
            (message_id, task_id, "send now", "queued", stamp),
        )

        class FakeClient:
            async def request(self, _method, _params):
                raise RuntimeError("expected active turn id mismatch")

        async def exercise():
            blocker = asyncio.create_task(asyncio.Event().wait())
            self.app.app_servers["test-error"] = FakeClient()
            self.app.app_thread_bindings[task_id] = ("test-error", "thread-1", "session-1")
            self.app.appserver_turn_ids[task_id] = "turn-1"
            self.app.appserver_turn_tasks[task_id] = blocker
            try:
                result = await self.app.dispatch_task_message(task_id, message_id, None)
                self.assertEqual("queued", result["status"])
                self.assertIn("expected active turn id mismatch", result["dispatch_error"])
                row = self.app.db.one("SELECT status,error FROM task_messages WHERE id=?", (message_id,))
                self.assertEqual("queued", row["status"])
                self.assertIn("expected active turn id mismatch", row["error"])
            finally:
                blocker.cancel()
                await asyncio.gather(blocker, return_exceptions=True)
                self.app.app_servers.pop("test-error", None)
                self.app.app_thread_bindings.pop(task_id, None)
                self.app.appserver_turn_ids.pop(task_id, None)
                self.app.appserver_turn_tasks.pop(task_id, None)
                self.app.task_workers.pop(task_id, None)
                self.app.db.execute("DELETE FROM task_messages WHERE task_id=?", (task_id,))
                self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

        asyncio.run(exercise())

    def test_dispatch_waits_for_external_terminal_turn_instead_of_returning_409(self):
        task_id = "external-terminal-queue"
        message_id = "external-terminal-message"
        self.make_task(task_id, "running")
        stamp = self.app.now()
        self.app.db.execute(
            "INSERT INTO task_messages (id,task_id,body,status,created_at) VALUES (?,?,?,?,?)",
            (message_id, task_id, "run after terminal", "queued", stamp),
        )
        self.app.external_turns[task_id] = {"turn_id": "terminal-turn"}
        try:
            with mock.patch.object(self.app, "schedule_task_drain") as drain, \
                 mock.patch.object(self.app, "steer_task_message", new=mock.AsyncMock()) as steer:
                result = asyncio.run(self.app.dispatch_task_message(task_id, message_id, None))
            self.assertTrue(result["waiting_for_turn"])
            self.assertEqual("queued", result["status"])
            self.assertEqual("terminal", result["execution_source"])
            drain.assert_called_once_with(task_id)
            steer.assert_not_awaited()
        finally:
            self.app.external_turns.pop(task_id, None)
            self.app.db.execute("DELETE FROM task_messages WHERE task_id=?", (task_id,))
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_goal_retry_defaults_off_and_manual_override_is_preserved(self):
        task_id = "goal-retry-thread"
        self.make_task(task_id, "available")
        self.app.db.execute("UPDATE tasks SET codex_session_id='' WHERE id=?", (task_id,))
        result = asyncio.run(self.app.patch_goal(task_id, self.app.GoalPatch(objective="finish it")))
        self.assertFalse(result["retry_forever"])
        self.assertEqual(0, result["retry_explicit"])

        started = {**self.app.task_or_404(task_id), "goal_status": "active", "retry_forever": True, "retry_explicit": 1}
        with mock.patch.object(self.app, "start_goal", new=mock.AsyncMock(return_value=started)) as start:
            result = asyncio.run(self.app.patch_task(task_id, self.app.TaskPatch(retry_forever=True)))
        self.assertTrue(result["retry_forever"])
        self.assertEqual(1, result["retry_explicit"])
        start.assert_awaited_once_with(task_id, None)

        with mock.patch.object(self.app, "schedule_task_drain"):
            result = asyncio.run(self.app.patch_goal(task_id, self.app.GoalPatch(objective="finish it better")))
        self.assertTrue(result["retry_forever"])
        self.assertEqual(1, result["retry_explicit"])

    def test_new_task_with_goal_keeps_auto_retry_off_by_default(self):
        result = asyncio.run(self.app.create_task(self.app.TaskCreate(
            name="Goal defaults off",
            prompt="Start",
            goal="Finish it",
            workspace=self.temp.name,
        ), None))
        self.assertFalse(result["retry_forever"])
        self.assertEqual(0, result["retry_explicit"])

        explicit = asyncio.run(self.app.create_task(self.app.TaskCreate(
            name="Goal explicit on",
            prompt="Start",
            goal="Finish it",
            workspace=self.temp.name,
            retry_forever=True,
        ), None))
        self.assertTrue(explicit["retry_forever"])
        self.assertEqual(1, explicit["retry_explicit"])

    def test_memory_mode_can_change_while_turn_is_running(self):
        task_id = "memory-live-toggle"
        self.make_task(task_id, "running")
        try:
            result = asyncio.run(self.app.codex_operation(task_id, self.app.OperationIn(operation="memory-disable"), None))
            self.assertEqual("disabled", result["memory_mode"])
            self.assertTrue(result["deferred"])
            self.assertEqual("disabled", self.app.task_or_404(task_id)["memory_mode"])
        finally:
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_clear_queue_removes_only_waiting_messages(self):
        task_id = "clear-message-queue"
        self.make_task(task_id, "running")
        stamp = self.app.now()
        rows = [("clear-queued-1", "queued"), ("clear-sent-1", "sent")]
        for message_id, status in rows:
            self.app.db.execute(
                "INSERT INTO task_messages (id,task_id,body,status,created_at) VALUES (?,?,?,?,?)",
                (message_id, task_id, message_id, status, stamp),
            )
        try:
            result = asyncio.run(self.app.clear_task_messages(task_id, None))
            self.assertEqual(["clear-queued-1"], result["message_ids"])
            self.assertIsNone(self.app.db.one("SELECT id FROM task_messages WHERE id=?", ("clear-queued-1",)))
            self.assertIsNotNone(self.app.db.one("SELECT id FROM task_messages WHERE id=?", ("clear-sent-1",)))
        finally:
            self.app.db.execute("DELETE FROM task_messages WHERE task_id=?", (task_id,))
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_incomplete_goal_is_not_a_provider_failure(self):
        self.assertFalse(self.app.is_provider_failure(self.app.GoalIncompleteError("still active")))
        self.assertTrue(self.app.is_provider_failure(RuntimeError("transport failed")))
        with mock.patch.object(self.app, "app_shutting_down", True):
            self.assertFalse(self.app.is_provider_failure(RuntimeError("app-server exited")))

    def test_dashboard_restart_queues_active_appserver_turn(self):
        task_id = "restart-active-appserver"
        session_id = "restart-active-session"
        message_id = "restart-active-message"
        self.make_task(task_id, "running")
        stamp = self.app.now()
        self.app.db.execute(
            "UPDATE tasks SET active_session_id=?,execution_source='dashboard',execution_turn_id='turn-live' WHERE id=?",
            (session_id, task_id),
        )
        self.app.db.execute(
            "INSERT INTO sessions (id,task_id,status,attempt,command,started_at) VALUES (?,?,?,?,?,?)",
            (session_id, task_id, "running", 1, "codex app-server", stamp),
        )
        self.app.db.execute(
            "INSERT INTO task_messages (id,task_id,body,status,session_id,created_at,started_at) VALUES (?,?,?,?,?,?,?)",
            (message_id, task_id, "continue", "running", session_id, stamp, stamp),
        )
        try:
            self.app.queue_appserver_turn_for_restart(task_id, session_id, message_id)
            task = self.app.task_or_404(task_id)
            session = self.app.db.one("SELECT * FROM sessions WHERE id=?", (session_id,))
            message = self.app.db.one("SELECT * FROM task_messages WHERE id=?", (message_id,))
            self.assertEqual("queued", task["status"])
            self.assertEqual("dashboard", task["execution_source"])
            self.assertEqual("", task["execution_turn_id"])
            self.assertEqual("interrupted", session["status"])
            self.assertEqual("queued", message["status"])
            self.assertIsNone(message["session_id"])
        finally:
            self.app.db.execute("DELETE FROM task_messages WHERE task_id=?", (task_id,))
            self.app.db.execute("DELETE FROM sessions WHERE task_id=?", (task_id,))
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_goal_supervisor_follows_consecutive_native_turns(self):
        async def exercise():
            class Client:
                reader_error = None

                def __init__(self):
                    self.reader_task = asyncio.create_task(asyncio.sleep(60))

            client = Client()
            events = asyncio.Queue()
            try:
                await events.put({"method": "turn/completed", "turn": {"id": "turn-1", "status": "completed"}})
                await events.put({"method": "turn/started", "turn": {"id": "turn-2", "status": "inProgress"}})
                await events.put({"method": "turn/completed", "turn": {"id": "turn-2", "status": "completed"}})
                first = await self.app.wait_for_appserver_turn(client, events, "turn-1")
                second_id = await self.app.wait_for_next_appserver_turn(client, events, "turn-1")
                second = await self.app.wait_for_appserver_turn(client, events, second_id)
                return first, second_id, second
            finally:
                client.reader_task.cancel()
                await asyncio.gather(client.reader_task, return_exceptions=True)

        first, second_id, second = asyncio.run(exercise())
        self.assertEqual("turn-1", first["id"])
        self.assertEqual("turn-2", second_id)
        self.assertEqual("turn-2", second["id"])

    def test_clearing_goal_uses_native_clear_and_disables_retry(self):
        task_id = "clear-native-goal-thread"
        self.make_task(task_id, "available")
        self.app.db.execute(
            "UPDATE tasks SET goal='old goal',goal_status='paused',retry_forever=0,codex_session_id=? WHERE id=?",
            (task_id, task_id),
        )

        class FakeClient:
            def __init__(self):
                self.calls = []

            async def request(self, method, params):
                self.calls.append((method, params))
                return {}

        client = FakeClient()
        try:
            with mock.patch.object(self.app, "appserver_for_task", new=mock.AsyncMock(return_value=(client, None))):
                result = asyncio.run(self.app.patch_goal(task_id, self.app.GoalPatch(objective=""), None))
            self.assertIn(("thread/goal/clear", {"threadId": task_id}), client.calls)
            self.assertEqual("", result["goal"])
            self.assertEqual("none", result["goal_status"])
            self.assertFalse(result["retry_forever"])
            self.assertEqual(0, result["goal_tokens_used"])
            self.assertEqual(1, result["goal_revision"])
        finally:
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_editing_a_completed_goal_moves_it_to_paused_with_retry_off(self):
        task_id = "edit-completed-goal-thread"
        self.make_task(task_id, "available")
        self.app.db.execute(
            "UPDATE tasks SET goal='old completed goal',goal_status='complete',retry_forever=0,codex_session_id=? WHERE id=?",
            (task_id, task_id),
        )

        class FakeClient:
            async def request(self, method, params):
                if method == "thread/goal/update":
                    return {"goal": {"objective": params["objective"], "status": params["status"], "tokensUsed": 0}}
                return {}

        try:
            with mock.patch.object(self.app, "appserver_for_task", new=mock.AsyncMock(return_value=(FakeClient(), None))):
                result = asyncio.run(self.app.patch_goal(task_id, self.app.GoalPatch(objective="revised goal"), None))
            self.assertEqual("revised goal", result["goal"])
            self.assertEqual("paused", result["goal_status"])
            self.assertFalse(result["retry_forever"])
            self.assertEqual("start", result["interaction"]["goalAction"])
        finally:
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_goal_edit_remains_committed_when_native_sync_fails(self):
        task_id = "goal-sync-failure-thread"
        self.make_task(task_id, "available")

        class FailingClient:
            async def request(self, method, params):
                raise RuntimeError("native writer unavailable")

        try:
            with (
                mock.patch.object(self.app, "appserver_for_task", new=mock.AsyncMock(return_value=(FailingClient(), None))),
                mock.patch.object(self.app, "ensure_thread_loaded", new=mock.AsyncMock()),
            ):
                result = asyncio.run(self.app.patch_goal(task_id, self.app.GoalPatch(objective="new durable objective"), None))
            self.assertEqual("new durable objective", result["goal"])
            self.assertEqual(1, result["goal_revision"])
            self.assertEqual("paused", result["goal_status"])
            self.assertIn("local Goal is saved", result["goal_sync_error"])
            self.assertEqual("new durable objective", self.app.task_or_404(task_id)["goal"])
        finally:
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_goal_start_activates_latest_revision_and_starts_goal_turn(self):
        task_id = "atomic-goal-start"
        self.make_task(task_id, "available")
        self.app.db.execute("UPDATE tasks SET goal='latest objective',goal_status='paused',goal_revision=4 WHERE id=?", (task_id,))
        launched = {"id": task_id, "status": "running", "goal": "latest objective", "goal_status": "active", "run_mode": "goal_resume"}
        try:
            with (
                mock.patch.object(self.app, "sync_current_goal", new=mock.AsyncMock(return_value="")) as sync,
                mock.patch.object(self.app, "launch", new=mock.AsyncMock(return_value=launched)) as launch,
                mock.patch.object(self.app, "broadcast_task", new=mock.AsyncMock()),
                mock.patch.object(self.app, "broadcast_overview", new=mock.AsyncMock()),
            ):
                result = asyncio.run(self.app.start_goal(task_id, None))
            self.assertEqual("active", self.app.task_or_404(task_id)["goal_status"])
            self.assertEqual(5, self.app.task_or_404(task_id)["goal_revision"])
            sync.assert_not_awaited()
            launch.assert_awaited_once_with(task_id, "goal-resume")
            self.assertEqual("goal_resume", result["run_mode"])
        finally:
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_goal_actions_are_serialized_per_task(self):
        task_id = "serialized-goal-actions"
        self.make_task(task_id, "available")
        active = 0
        max_active = 0

        async def fake_start(_task_id):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.02)
            active -= 1
            return {"id": task_id}

        async def exercise():
            with mock.patch.object(self.app, "_start_goal", new=mock.AsyncMock(side_effect=fake_start)) as start:
                await asyncio.gather(
                    self.app.start_goal(task_id, None),
                    self.app.start_goal(task_id, None),
                )
            self.assertEqual(2, start.await_count)
            self.assertEqual(1, max_active)

        try:
            asyncio.run(exercise())
        finally:
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_provider_cannot_change_while_task_has_a_writer(self):
        task_id = "provider-change-active-writer"
        self.make_task(task_id, "running")
        try:
            with mock.patch.object(self.app, "dashboard_owns_task", return_value=True):
                with self.assertRaises(self.app.HTTPException) as raised:
                    asyncio.run(self.app.patch_task(task_id, self.app.TaskPatch(provider_id="other-provider"), None))
            self.assertEqual(409, raised.exception.status_code)
            self.assertIn("before changing its provider", raised.exception.detail)
        finally:
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_goal_start_does_not_adopt_a_stale_external_turn(self):
        task_id = f"stale-external-goal-{time.time_ns()}"
        self.make_task(task_id, "stopped")
        self.app.db.execute("UPDATE tasks SET goal='latest objective',goal_status='paused' WHERE id=?", (task_id,))
        self.app.register_external_turn(task_id, {
            "thread_id": task_id,
            "turn_id": "dead-turn",
            "started_at": self.app.now(),
            "session_id": f"external:{task_id}:dead-turn",
            "path": "/tmp/codex-partner-missing-rollout.jsonl",
        })
        launched = {"id": task_id, "status": "running", "goal_status": "active", "run_mode": "goal_resume"}
        try:
            with (
                mock.patch.object(self.app, "sync_current_goal", new=mock.AsyncMock(return_value="")),
                mock.patch.object(self.app, "launch", new=mock.AsyncMock(return_value=launched)) as launch,
                mock.patch.object(self.app, "broadcast_task", new=mock.AsyncMock()),
                mock.patch.object(self.app, "broadcast_overview", new=mock.AsyncMock()),
            ):
                result = asyncio.run(self.app.start_goal(task_id, None))
            launch.assert_awaited_once_with(task_id, "goal-resume")
            self.assertNotIn(task_id, self.app.external_turns)
            self.assertEqual("goal_resume", result["run_mode"])
        finally:
            self.app.clear_external_turns(task_id)
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_goal_start_reports_a_launch_race_without_a_real_owner(self):
        task_id = f"goal-start-race-{time.time_ns()}"
        self.make_task(task_id, "stopped")
        self.app.db.execute("UPDATE tasks SET goal='latest objective',goal_status='paused' WHERE id=?", (task_id,))
        try:
            with (
                mock.patch.object(self.app, "sync_current_goal", new=mock.AsyncMock(return_value="")),
                mock.patch.object(self.app, "refresh_live_external_turn", new=mock.AsyncMock(return_value=False)),
                mock.patch.object(self.app, "launch", new=mock.AsyncMock(side_effect=self.app.HTTPException(409, "Task is already running"))),
                mock.patch.object(self.app, "broadcast_task", new=mock.AsyncMock()),
                mock.patch.object(self.app, "broadcast_overview", new=mock.AsyncMock()),
            ):
                with self.assertRaises(self.app.HTTPException) as caught:
                    asyncio.run(self.app.start_goal(task_id, None))
            self.assertEqual(409, caught.exception.status_code)
            result = self.app.task_or_404(task_id)
            self.assertEqual("paused", result["goal_status"])
            self.assertEqual("", result["run_mode"])
            self.assertIn("already running", result["last_error"])
        finally:
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_goal_start_ignores_stale_running_status_without_an_owner(self):
        task_id = f"stale-running-goal-{time.time_ns()}"
        self.make_task(task_id, "running")
        self.app.db.execute("UPDATE tasks SET goal='latest objective',goal_status='paused' WHERE id=?", (task_id,))
        launched = {"id": task_id, "status": "running", "goal_status": "active", "run_mode": "goal_resume"}
        try:
            with (
                mock.patch.object(self.app, "sync_current_goal", new=mock.AsyncMock(return_value="")),
                mock.patch.object(self.app, "refresh_live_external_turn", new=mock.AsyncMock(return_value=False)),
                mock.patch.object(self.app, "dashboard_owns_task", return_value=False),
                mock.patch.object(self.app, "launch", new=mock.AsyncMock(return_value=launched)) as launch,
                mock.patch.object(self.app, "broadcast_task", new=mock.AsyncMock()),
                mock.patch.object(self.app, "broadcast_overview", new=mock.AsyncMock()),
            ):
                result = asyncio.run(self.app.start_goal(task_id, None))
            launch.assert_awaited_once_with(task_id, "goal-resume")
            self.assertEqual("running", result["status"])
        finally:
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_manual_turn_completion_honors_goal_start_takeover(self):
        source = Path(self.app.__file__).read_text(encoding="utf-8")
        self.assertIn("resume_goal_after_cleanup = False", source)

    def test_goal_pause_pauses_goal_and_stops_conversation(self):
        task_id = "atomic-goal-pause"
        self.make_task(task_id, "running")
        self.app.db.execute("UPDATE tasks SET goal='ship it',goal_status='active',retry_forever=0,goal_revision=2 WHERE id=?", (task_id,))
        try:
            with (
                mock.patch.object(self.app, "sync_current_goal", new=mock.AsyncMock(return_value="")),
                mock.patch.object(self.app, "stop_task_run", new=mock.AsyncMock(side_effect=lambda _id, pause_goal=False: self.app.task_or_404(_id))) as stop,
                mock.patch.object(self.app, "broadcast_task", new=mock.AsyncMock()),
                mock.patch.object(self.app, "broadcast_overview", new=mock.AsyncMock()),
            ):
                result = asyncio.run(self.app.pause_goal(task_id, None))
            self.assertEqual("paused", result["goal_status"])
            self.assertFalse(result["retry_forever"])
            stop.assert_awaited_once_with(task_id, pause_goal=False)
        finally:
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_goal_pause_api_rejects_active_auto_resume_until_retry_is_disabled(self):
        task_id = "goal-pause-retry-on"
        self.make_task(task_id, "running")
        self.app.db.execute(
            "UPDATE tasks SET goal='active goal',goal_status='active',retry_forever=1 WHERE id=?",
            (task_id,),
        )
        try:
            with mock.patch.object(self.app, "release_completed_dashboard_owner", new=mock.AsyncMock()):
                with self.assertRaises(self.app.HTTPException) as raised:
                    asyncio.run(self.app.pause_goal(task_id, None))
            self.assertEqual(409, raised.exception.status_code)
            task = self.app.task_or_404(task_id)
            self.assertEqual("active", task["goal_status"])
            self.assertTrue(task["retry_forever"])
        finally:
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_goal_pause_clears_stale_session_binding_when_task_is_available(self):
        task_id = "goal-pause-stale-binding"
        self.make_task(task_id, "available", active_session_id="stale-session")
        self.app.db.execute(
            "UPDATE tasks SET goal='continue normally',goal_status='active',execution_source='dashboard',execution_turn_id='stale-turn' WHERE id=?",
            (task_id,),
        )
        try:
            async def fake_stop(actual_task_id, pause_goal=False):
                self.assertEqual(task_id, actual_task_id)
                self.assertFalse(pause_goal)
                self.app.db.execute(
                    "UPDATE tasks SET active_session_id=NULL,execution_source='',execution_turn_id='',run_mode='' WHERE id=?",
                    (task_id,),
                )
                return self.app.task_or_404(task_id)

            with (
                mock.patch.object(self.app, "sync_current_goal", new=mock.AsyncMock(return_value="")),
                mock.patch.object(self.app, "stop_task_run", new=mock.AsyncMock(side_effect=fake_stop)) as stop,
                mock.patch.object(self.app, "broadcast_task", new=mock.AsyncMock()),
                mock.patch.object(self.app, "broadcast_overview", new=mock.AsyncMock()),
            ):
                result = asyncio.run(self.app.pause_goal(task_id, None))
            self.assertEqual("paused", result["goal_status"])
            self.assertIsNone(result["active_session_id"])
            stop.assert_not_awaited()
        finally:
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_conversation_stop_pauses_goal_even_when_turn_is_already_stopped(self):
        task_id = "conversation-stop-pauses-goal"
        self.make_task(task_id, "available")
        self.app.db.execute("UPDATE tasks SET goal='ship it',goal_status='active',retry_forever=1,last_error='old failure' WHERE id=?", (task_id,))
        try:
            with (
                mock.patch.object(self.app, "broadcast_task", new=mock.AsyncMock()),
                mock.patch.object(self.app, "broadcast_overview", new=mock.AsyncMock()),
            ):
                result = asyncio.run(self.app.stop_task_run(task_id))
            self.assertEqual("paused", result["goal_status"])
            self.assertFalse(result["retry_forever"])
            self.assertEqual("", result["last_error"])
        finally:
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_health_counts_terminal_owned_task(self):
        task_id = "health-terminal-thread"
        self.make_task(task_id, "running")
        result = asyncio.run(self.app.health())
        self.assertGreaterEqual(result["running"], 1)
        self.assertTrue(result["codex_available"])
        self.assertIsNone(result["codex_install"])

    def test_empty_workspace_uses_stable_non_temp_default(self):
        workspace = self.app.resolve_task_workspace("")
        self.assertEqual(self.app.DEFAULT_WORKSPACE, workspace)
        self.assertEqual(str(workspace), asyncio.run(self.app.health())["default_workspace"])
        temp_roots = (Path("/tmp"), Path("/var/tmp"), Path("/dev/shm"))
        self.assertFalse(any(workspace == root or root in workspace.parents for root in temp_roots))

    def test_relative_workspace_is_resolved_from_stable_default(self):
        child = self.app.DEFAULT_WORKSPACE / f"codex-dashboard-test-{os.getpid()}"
        child.mkdir(exist_ok=True)
        try:
            self.assertEqual(child.resolve(), self.app.resolve_task_workspace(child.name))
        finally:
            child.rmdir()

    def test_new_local_sessions_wait_for_session_id_before_creating_workspace(self):
        with tempfile.TemporaryDirectory(dir=self.temp.name) as directory:
            root = Path(directory) / "codex_partner"
            with mock.patch.object(self.app, "SESSION_WORKSPACE_ROOT", root):
                quick = asyncio.run(self.app.create_quick_task(self.app.QuickTaskCreate(), None))
                regular = asyncio.run(self.app.create_task(
                    self.app.TaskCreate(name="Regular", prompt="Start"), None
                ))
                command = self.app.create_command_task(quick, "", "available")
            for task in (quick, regular, command):
                workspace = Path(task["workspace"])
                self.assertEqual(root.resolve(), workspace)
                self.assertFalse((root / task["id"]).exists())
            self.app.db.execute("DELETE FROM tasks WHERE id IN (?,?,?)", (quick["id"], regular["id"], command["id"]))

    def test_default_workspace_aligns_to_codex_thread_id(self):
        with tempfile.TemporaryDirectory(dir=self.temp.name) as directory:
            root = Path(directory) / "codex_partner"
            with mock.patch.object(self.app, "SESSION_WORKSPACE_ROOT", root):
                task = asyncio.run(self.app.create_quick_task(self.app.QuickTaskCreate(), None))
                target = self.app.align_session_workspace(task["id"], "thread-123")
                self.assertEqual((root / "thread-123").resolve(), target)
                self.assertTrue(target.is_dir())
                self.assertFalse((root / task["id"]).exists())
                self.assertEqual(str(target), self.app.task_or_404(task["id"])["workspace"])
                self.assertEqual(target, self.app.align_session_workspace(task["id"], "thread-123"))
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task["id"],))

    def test_interrupted_workspace_alignment_finishes_database_update(self):
        with tempfile.TemporaryDirectory(dir=self.temp.name) as directory:
            root = Path(directory) / "codex_partner"
            with mock.patch.object(self.app, "SESSION_WORKSPACE_ROOT", root):
                task = asyncio.run(self.app.create_quick_task(self.app.QuickTaskCreate(), None))
                current = root / task["id"]
                current.mkdir()
                self.app.db.execute("UPDATE tasks SET workspace=? WHERE id=?", (str(current), task["id"]))
                current.rmdir()
                target = root / "thread-after-rename"
                target.mkdir()
                aligned = self.app.align_session_workspace(task["id"], target.name)
                self.assertEqual(target.resolve(), aligned)
                self.assertEqual(str(target.resolve()), self.app.task_or_404(task["id"])["workspace"])
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task["id"],))

    def test_temp_directory_cannot_be_configured_as_default(self):
        with mock.patch.dict(os.environ, {"CODEX_DASHBOARD_DEFAULT_WORKSPACE": "/tmp/codex-dashboard"}):
            self.assertEqual(Path.home().resolve(), self.app.configured_default_workspace())

    def test_workspace_picker_is_confined_to_configured_roots(self):
        task_id = "workspace-picker-thread"
        self.make_task(task_id, "available")
        with tempfile.TemporaryDirectory(dir=self.temp.name) as workspace:
            child = Path(workspace) / "child"
            child.mkdir()
            escape = Path(workspace) / "escape"
            escape.symlink_to(Path(workspace).parent, target_is_directory=True)
            self.app.db.execute("UPDATE tasks SET workspace=? WHERE id=?", (workspace, task_id))
            with mock.patch.object(self.app, "WORKSPACE_ROOTS", (Path(workspace).resolve(),)):
                result = asyncio.run(self.app.task_workspace_picker(task_id, str(workspace)))
                self.assertEqual(str(Path(workspace).resolve()), result["path"])
                self.assertIsNone(result["parent"])
                self.assertIn(str(child.resolve()), [entry["path"] for entry in result["entries"]])
                self.assertNotIn("escape", [entry["name"] for entry in result["entries"]])
                with self.assertRaises(self.app.HTTPException) as raised:
                    asyncio.run(self.app.task_workspace_picker(task_id, str(Path(workspace).parent)))
                self.assertEqual(403, raised.exception.status_code)

    def test_workspace_upload_download_and_overwrite_protection(self):
        task_id = "workspace-transfer-thread"
        self.make_task(task_id, "available")
        self.assertFalse(hasattr(self.app, "WORKSPACE_UPLOAD_MAX_BYTES"))

        class UploadRequest:
            def __init__(self, content: bytes):
                self.content = content
                self.headers = {"content-length": str(len(content))}

            async def stream(self):
                yield self.content[:2]
                yield self.content[2:]

        async def exercise():
            with tempfile.TemporaryDirectory(dir=self.temp.name) as directory:
                relative = Path(directory).relative_to(self.temp.name).as_posix()
                first = await self.app.upload_workspace_file(
                    task_id, UploadRequest(b"first"), path=relative, filename="result.txt"
                )
                self.assertEqual(5, first["bytes"])
                target = Path(directory) / "result.txt"
                self.assertEqual(b"first", target.read_bytes())

                with self.assertRaises(self.app.HTTPException) as conflict:
                    await self.app.upload_workspace_file(
                        task_id, UploadRequest(b"second"), path=relative, filename="result.txt"
                    )
                self.assertEqual(409, conflict.exception.status_code)
                self.assertEqual(b"first", target.read_bytes())

                replaced = await self.app.upload_workspace_file(
                    task_id, UploadRequest(b"second"), path=relative, filename="result.txt", overwrite=True
                )
                self.assertEqual(6, replaced["bytes"])
                self.assertEqual(b"second", target.read_bytes())
                preview = await self.app.task_workspace(task_id, f"{relative}/result.txt")
                self.assertTrue(preview["editable"])
                edited = await self.app.update_workspace_file(
                    task_id,
                    self.app.WorkspaceFileUpdate(content="edited\n"),
                    path=f"{relative}/result.txt",
                )
                self.assertEqual(7, edited["entry"]["size"])
                self.assertEqual("edited\n", target.read_text(encoding="utf-8"))
                response = await self.app.download_workspace_file(task_id, mock.Mock(headers={}), f"{relative}/result.txt")
                self.assertEqual(str(target.resolve()), str(response.path))

                with self.assertRaises(self.app.HTTPException) as invalid_name:
                    await self.app.upload_workspace_file(
                        task_id, UploadRequest(b"bad"), path=relative, filename="../bad.txt"
                    )
                self.assertEqual(400, invalid_name.exception.status_code)

                secret = Path(directory) / ".env"
                secret.write_text("SECRET=value", encoding="utf-8")
                with self.assertRaises(self.app.HTTPException) as hidden:
                    await self.app.download_workspace_file(task_id, mock.Mock(headers={}), f"{relative}/.env")
                self.assertEqual(403, hidden.exception.status_code)
                with self.assertRaises(self.app.HTTPException) as hidden_edit:
                    await self.app.update_workspace_file(
                        task_id,
                        self.app.WorkspaceFileUpdate(content="changed"),
                        path=f"{relative}/.env",
                    )
                self.assertEqual(403, hidden_edit.exception.status_code)

                binary = Path(directory) / "binary.dat"
                binary.write_bytes(b"binary\0data")
                binary_preview = await self.app.task_workspace(task_id, f"{relative}/binary.dat")
                self.assertFalse(binary_preview["editable"])
                with self.assertRaises(self.app.HTTPException) as binary_edit:
                    await self.app.update_workspace_file(
                        task_id,
                        self.app.WorkspaceFileUpdate(content="changed"),
                        path=f"{relative}/binary.dat",
                    )
                self.assertEqual(400, binary_edit.exception.status_code)

                image = Path(directory) / "large.png"
                self.app.Image.new("RGB", (1600, 1200), (42, 120, 88)).save(image, "PNG")
                thumbnail = await self.app.thumbnail_workspace_file(
                    task_id, f"{relative}/large.png", size=320, _=None
                )
                thumbnail_path = Path(thumbnail.path)
                self.assertLess(thumbnail_path.stat().st_size, image.stat().st_size)
                with self.app.Image.open(thumbnail_path) as generated:
                    self.assertEqual("WEBP", generated.format)
                    self.assertLessEqual(max(generated.size), 320)

        asyncio.run(exercise())

    def test_large_workspace_text_is_paged_and_media_is_not_read_as_text(self):
        task_id = "workspace-large-preview"
        self.make_task(task_id, "available")
        try:
            text = Path(self.temp.name) / "large.log"
            text.write_text("A" * 1_100_000 + "B" * 1_100_000, encoding="utf-8")
            first = asyncio.run(self.app.task_workspace(task_id, "large.log", limit=1_048_576))
            self.assertEqual("text", first["preview_kind"])
            self.assertTrue(first["truncated"])
            self.assertEqual(1_048_576, first["next_offset"])
            self.assertFalse(first["editable"])
            second = asyncio.run(self.app.task_workspace(task_id, "large.log", offset=first["next_offset"], limit=1_048_576))
            self.assertEqual(first["next_offset"], second["offset"])
            self.assertTrue(second["content"].startswith("A"))

            unicode_text = Path(self.temp.name) / "unicode.log"
            unicode_text.write_text("你" * 400_000, encoding="utf-8")
            unicode_first = asyncio.run(self.app.task_workspace(task_id, "unicode.log", limit=1_048_576))
            unicode_second = asyncio.run(self.app.task_workspace(task_id, "unicode.log", offset=unicode_first["next_offset"], limit=1_048_576))
            self.assertNotIn("�", unicode_first["content"] + unicode_second["content"])
            self.assertEqual(400_000, len(unicode_first["content"] + unicode_second["content"]))

            video = Path(self.temp.name) / "large.mp4"
            video.write_bytes(b"\0" * (2 * 1024 * 1024))
            preview = asyncio.run(self.app.task_workspace(task_id, "large.mp4"))
            self.assertEqual("video", preview["preview_kind"])
            self.assertEqual("", preview["content"])
            self.assertFalse(preview["editable"])
        finally:
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_absolute_workspace_artifact_can_be_previewed_but_not_edited(self):
        task_id = "absolute-artifact-preview"
        self.make_task(task_id, "available")
        external_root = Path(tempfile.mkdtemp(prefix="codex-partner-artifacts-"))
        artifact = external_root / "server.log"
        artifact.write_text("external artifact", encoding="utf-8")
        original_roots = self.app.WORKSPACE_ROOTS
        configured_root = external_root / ".." / external_root.name
        self.app.WORKSPACE_ROOTS = (configured_root,)
        absolute = str(artifact)
        try:
            preview = asyncio.run(self.app.task_workspace(task_id, absolute))
            self.assertEqual(artifact.resolve(), Path(preview["entry"]["path"]).resolve())
            self.assertEqual("external artifact", preview["content"])
            with self.assertRaises(self.app.HTTPException) as edited:
                asyncio.run(self.app.update_workspace_file(task_id, self.app.WorkspaceFileUpdate(content="nope"), absolute))
            self.assertEqual(400, edited.exception.status_code)
        finally:
            self.app.WORKSPACE_ROOTS = original_roots
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            artifact.unlink(missing_ok=True)
            external_root.rmdir()

    def test_media_byte_ranges_support_seeking(self):
        self.assertEqual((0, 99), self.app.parse_byte_range("bytes=0-99", 1000))
        self.assertEqual((900, 999), self.app.parse_byte_range("bytes=-100", 1000))
        self.assertEqual((900, 999), self.app.parse_byte_range("bytes=900-", 1000))
        with self.assertRaises(ValueError):
            self.app.parse_byte_range("bytes=2000-", 1000)
        with self.assertRaises(ValueError):
            self.app.parse_byte_range("bytes=0-0", 0)

    def test_large_media_frontend_uses_streaming_urls_instead_of_blobs(self):
        source = (Path(__file__).resolve().parents[1] / "static" / "conversation.js").read_text(encoding="utf-8")
        hydrate = source[source.index("async function hydrateChatFiles"):source.index("function chatFileExtension")]
        streaming_media = hydrate[hydrate.index('} else if (["audio", "video"].includes(mediaKind)'):hydrate.index("} else {", hydrate.index('} else if (["audio", "video"].includes(mediaKind)'))]
        viewer = source[source.index("async function openChatFileViewer"):source.index("function localizeInspectorText")]
        downloader = source[source.index("async function downloadWorkspaceFile"):source.index("function openWorkspaceFileEditor")]
        self.assertIn("const url = chatDownloadUrl(taskId, path)", hydrate)
        self.assertIn("const url = chatDownloadUrl(taskId, path)", viewer)
        self.assertNotIn("response.blob()", streaming_media)
        self.assertNotIn("chatFileObjectUrl", viewer)
        self.assertNotIn("response.blob()", downloader)
        self.assertIn("加载后续内容", source)

    def test_source_locations_open_the_real_text_file_and_target_line(self):
        source = (Path(__file__).resolve().parents[1] / "static" / "conversation.js").read_text(encoding="utf-8")
        self.assertIn("function chatFileReference(path)", source)
        self.assertIn("decodeURIComponent(value)", source)
        self.assertIn("path: reference.path", source)
        self.assertIn('target.className = "media-viewer-target-line"', source)
        script = f"""
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync({json.dumps(str(Path(__file__).resolve().parents[1] / "static/conversation.js"))}, "utf8");
const start = source.indexOf("function chatFileReference(path)");
const end = source.indexOf("async function chatFileObjectUrl", start);
const context = {{ URLSearchParams }};
vm.createContext(context);
vm.runInContext(source.slice(start, end), context);
process.stdout.write(JSON.stringify([
  context.chatFileReference("/repo/install.sh:79"),
  context.chatFileReference("/repo/main.py:12:4"),
  context.chatFileReference("/repo/main.cc#L25C3"),
  context.chatFileReference("home/ori/report%20files/result.md"),
  context.chatViewerKind("/repo/install.sh:79"),
  context.chatDownloadUrl("task", "/repo/install.sh:79")
]));
"""
        result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True, encoding="utf-8")
        parsed = json.loads(result.stdout)
        self.assertEqual({"path": "/repo/install.sh", "line": 79, "column": 0}, parsed[0])
        self.assertEqual({"path": "/repo/main.py", "line": 12, "column": 4}, parsed[1])
        self.assertEqual({"path": "/repo/main.cc", "line": 25, "column": 3}, parsed[2])
        self.assertEqual({"path": "/home/ori/report files/result.md", "line": 0, "column": 0}, parsed[3])
        self.assertEqual("text", parsed[4])
        self.assertIn("path=%2Frepo%2Finstall.sh", parsed[5])

    def test_legacy_staging_attachment_is_restored_to_session_workspace(self):
        task_id = "legacy-attachment-thread"
        self.make_task(task_id, "available")
        with tempfile.TemporaryDirectory(dir=self.temp.name) as directory:
            root = Path(directory) / "codex_partner"
            session = root / task_id
            session.mkdir(parents=True)
            legacy = root / "notes.txt"
            legacy.write_text("old upload", encoding="utf-8")
            other_session = root / "other-session"
            other_session.mkdir()
            (other_session / "secret.txt").write_text("nope", encoding="utf-8")
            self.app.db.execute("UPDATE tasks SET workspace=? WHERE id=?", (str(session), task_id))
            with mock.patch.object(self.app, "SESSION_WORKSPACE_ROOT", root):
                result = asyncio.run(self.app.task_workspace(task_id, "notes.txt"))
                self.assertEqual("old upload", result["content"])
                self.assertEqual("notes.txt", result["entry"]["path"])
                self.assertEqual("old upload", (session / "notes.txt").read_text(encoding="utf-8"))
                with self.assertRaises(self.app.HTTPException) as raised:
                    asyncio.run(self.app.task_workspace(task_id, "other-session/secret.txt"))
                self.assertEqual(404, raised.exception.status_code)
        self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_ssh_config_discovery_follows_includes_and_ignores_patterns(self):
        with tempfile.TemporaryDirectory(dir=self.temp.name) as directory:
            root = Path(directory)
            included = root / "included.conf"
            included.write_text("Host build-box\n  HostName 10.0.0.8\nHost *.internal\n", encoding="utf-8")
            config = root / "config"
            config.write_text(f"Include {included}\nHost direct-box other-box\n", encoding="utf-8")
            self.assertEqual(["build-box", "direct-box", "other-box"], self.app.ssh_config_aliases(config))

    def test_ssh_command_normalizes_explicit_port_without_persisting_arguments(self):
        self.assertEqual(
            "root@10.20.35.9:62288",
            self.app.validate_ssh_host("ssh -p 62288 root@10.20.35.9 123456"),
        )

    def test_auth_ssh_port_prefers_current_openssh_connection(self):
        with mock.patch.dict(
            self.app.os.environ,
            {"SSH_CONNECTION": "192.168.4.82 36814 10.20.35.9 62288"},
            clear=True,
        ):
            self.assertEqual(62288, self.app.detect_auth_ssh_port())

    def test_auth_ssh_port_reads_sshd_config_when_not_in_login_environment(self):
        with tempfile.TemporaryDirectory(dir=self.temp.name) as directory:
            config = Path(directory) / "codex-62288.conf"
            config.write_text("Port 62288\nListenAddress 0.0.0.0\n", encoding="utf-8")
            with mock.patch.dict(self.app.os.environ, {}, clear=True), mock.patch.object(
                self.app, "_sshd_config_paths", return_value=[config]
            ):
                self.assertEqual(62288, self.app.detect_auth_ssh_port())

    def test_ssh_failure_only_requests_password_for_authentication_errors(self):
        self.assertEqual("needs_password", self.app.ssh_failure_status("Permission denied (publickey,password)."))
        self.assertEqual("failed", self.app.ssh_failure_status("connect to host example port 22: Connection timed out"))
        self.assertEqual("failed", self.app.ssh_failure_status("Connection refused"))

    def test_remote_quick_task_keeps_host_and_remote_workspace(self):
        async def exercise():
            connection = {"connected": True, "remote_home": "/home/remote", "codex_bin": "/usr/bin/codex"}
            with mock.patch.object(self.app, "require_ssh_connection", new=mock.AsyncMock(return_value=connection)):
                task = await self.app.create_quick_task(
                    self.app.QuickTaskCreate(name="Remote", workspace="~/project", ssh_host="build-box")
                )
            self.assertEqual("build-box", task["ssh_host"])
            self.assertEqual("/home/remote/project", task["workspace"])
            self.assertTrue(task["yolo"])
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task["id"],))

        asyncio.run(exercise())

    def test_remote_workspace_uses_ssh_filesystem_backend(self):
        task_id = "remote-workspace-thread"
        self.make_task(task_id, "available")
        self.app.db.execute("UPDATE tasks SET ssh_host=?,workspace=? WHERE id=?", ("build-box", "/srv/project", task_id))
        expected = {"root": "/srv/project", "entry": {"kind": "directory", "path": ""}, "entries": []}
        with mock.patch.object(self.app, "remote_fs_json", new=mock.AsyncMock(return_value=expected)) as remote:
            result = asyncio.run(self.app.task_workspace(task_id, "src"))
        self.assertEqual(expected, result)
        remote.assert_awaited_once()

    def test_remote_appserver_uses_shared_openssh_transport(self):
        async def exercise():
            task = {"ssh_host": "build-box"}
            connection = {"connected": True, "remote_home": "/home/remote", "codex_bin": "/opt/codex/bin/codex"}
            key = self.app.appserver_key(None, task)
            self.app.app_servers.pop(key, None)
            with mock.patch.object(self.app, "require_ssh_connection", new=mock.AsyncMock(return_value=connection)), mock.patch.object(
                self.app.AppServerClient, "start", new=mock.AsyncMock()
            ):
                client = await self.app.appserver_for(None, task)
            self.assertEqual(key, client.key)
            self.assertFalse(client.local)
            self.assertIn("build-box", client.command)
            self.assertIn("/opt/codex/bin/codex app-server --stdio", client.command[-1])
            self.app.app_servers.pop(key, None)

        asyncio.run(exercise())

    def test_task_appservers_are_isolated_and_release_only_their_writer(self):
        provider = {"id": "test-provider"}
        first = {"id": "thread-one", "ssh_host": ""}
        second = {"id": "thread-two", "ssh_host": ""}
        first_key = self.app.appserver_key(provider, first)
        second_key = self.app.appserver_key(provider, second)
        self.assertNotEqual(first_key, second_key)
        self.assertEqual("test-provider", self.app.appserver_key(provider, None))

        class Client:
            closed = False

            async def close(self):
                self.closed = True

        async def exercise():
            first_client, second_client = Client(), Client()
            self.app.app_servers[first_key] = first_client
            self.app.app_servers[second_key] = second_client
            self.app.app_thread_bindings[first["id"]] = (first_key, first["id"], "session-one")
            self.app.app_thread_bindings[second["id"]] = (second_key, second["id"], "session-two")
            try:
                await self.app.close_task_appserver(provider, first, first_client)
                self.assertTrue(first_client.closed)
                self.assertNotIn(first_key, self.app.app_servers)
                self.assertNotIn(first["id"], self.app.app_thread_bindings)
                self.assertIs(second_client, self.app.app_servers[second_key])
                self.assertIn(second["id"], self.app.app_thread_bindings)
            finally:
                self.app.app_servers.pop(first_key, None)
                self.app.app_servers.pop(second_key, None)
                self.app.app_thread_bindings.pop(first["id"], None)
                self.app.app_thread_bindings.pop(second["id"], None)

        asyncio.run(exercise())

    def test_unset_task_provider_resolves_to_the_same_effective_provider_everywhere(self):
        task = {"id": "providerless-thread", "provider_id": None, "ssh_host": ""}
        provider = {"id": "effective-default"}
        with mock.patch.object(self.app, "provider_rows", return_value=[provider]):
            self.assertIs(provider, self.app.provider_for_task(task))
            self.assertEqual(
                self.app.appserver_key(provider, task),
                self.app.appserver_key(self.app.provider_for_task(task), task),
            )

    def test_appserver_removes_alternate_provider_keys_for_the_same_task(self):
        task = {"id": "one-writer-thread", "provider_id": None, "ssh_host": ""}
        desired = {"id": "effective-provider"}
        stale_key = self.app.appserver_key(None, task)
        desired_key = self.app.appserver_key(desired, task)

        class Client:
            closed = False

            async def close(inner_self):
                inner_self.closed = True

        stale_client = Client()

        async def exercise():
            self.app.app_servers[stale_key] = stale_client
            self.app.app_thread_bindings[task["id"]] = (stale_key, task["id"], "old-session")
            with mock.patch.object(self.app, "provider_rows", return_value=[desired]), \
                 mock.patch.object(self.app, "stop_persistent_appserver", new=mock.AsyncMock()) as stop:
                await self.app.remove_stale_task_appservers(task, desired_key)
            self.assertTrue(stale_client.closed)
            self.assertNotIn(stale_key, self.app.app_servers)
            self.assertNotIn(task["id"], self.app.app_thread_bindings)
            stopped = {call.args[0] for call in stop.await_args_list}
            self.assertIn(stale_key, stopped)
            self.assertNotIn(desired_key, stopped)

        asyncio.run(exercise())

    def test_permanent_delete_releases_task_appserver(self):
        task_id = "deleted-task-appserver"
        self.make_task(task_id, "trashed")
        self.app.db.execute(
            "UPDATE tasks SET trashed=1,codex_session_id='' WHERE id=?",
            (task_id,),
        )
        with mock.patch.object(self.app, "provider_rows", return_value=[]), \
             mock.patch.object(self.app, "close_task_appserver", new=mock.AsyncMock()) as close, \
             mock.patch.object(self.app, "stop_persistent_appserver", new=mock.AsyncMock()) as stop, \
             mock.patch.object(self.app, "broadcast_overview_removed", new=mock.AsyncMock()):
            result = asyncio.run(self.app.permanently_delete_trash(task_id, None))
        self.assertTrue(result["deleted"])
        close.assert_awaited_once()
        stop.assert_awaited_once()
        self.assertIsNone(self.app.db.one("SELECT id FROM tasks WHERE id=?", (task_id,)))

    def test_launch_persists_effective_provider_before_opening_appserver(self):
        task_id = "persist-effective-provider"
        self.make_task(task_id, "available")
        provider = {"id": "effective-provider", "name": "Effective"}

        async def fake_launch_appserver(task, selected, *_args):
            self.assertEqual(provider["id"], selected["id"])
            self.assertEqual(provider["id"], task["provider_id"])
            return task | {"status": "running"}

        try:
            with mock.patch.object(self.app, "provider_rows", return_value=[provider]), \
                 mock.patch.object(self.app, "refresh_live_external_turn", new=mock.AsyncMock(return_value=False)), \
                 mock.patch.object(self.app, "dashboard_owns_task", return_value=False), \
                 mock.patch.object(self.app, "require_codex"), \
                 mock.patch.object(self.app, "launch_appserver", new=mock.AsyncMock(side_effect=fake_launch_appserver)), \
                 mock.patch.object(self.app, "broadcast_overview", new=mock.AsyncMock()):
                asyncio.run(self.app.launch(task_id, "resume"))
            self.assertEqual(provider["id"], self.app.task_or_404(task_id)["provider_id"])
        finally:
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_remote_thread_fork_keeps_ssh_host_and_workspace(self):
        task_id = "remote-fork-source"
        self.make_task(task_id, "available")
        self.app.db.execute(
            "UPDATE tasks SET name=?,ssh_host=?,workspace=?,codex_session_id=? WHERE id=?",
            ("Remote source", "build-box", "/srv/project", "remote-thread", task_id),
        )

        class FakeClient:
            async def request(self, method, _params):
                if method == "thread/resume":
                    return {"thread": {"id": "remote-thread"}}
                if method == "thread/fork":
                    return {"thread": {"id": "remote-forked-thread"}}
                raise AssertionError(method)

        async def exercise():
            task = self.app.task_or_404(task_id)
            with mock.patch.object(self.app, "appserver_for", new=mock.AsyncMock(return_value=FakeClient())):
                result = await self.app.run_thread_operation(task, self.app.OperationIn(operation="fork"))
            forked = self.app.task_or_404(result["task"]["id"])
            self.assertEqual("build-box", forked["ssh_host"])
            self.assertEqual("/srv/project", forked["workspace"])
            self.assertEqual("remote-forked-thread", forked["codex_session_id"])
            self.assertEqual("remote-forked-thread", forked["id"])
            self.assertFalse(forked["retry_forever"])
            self.app.db.execute("DELETE FROM tasks WHERE id IN (?,?)", (task_id, forked["id"]))

        asyncio.run(exercise())

    def test_thread_operation_resumes_rollout_before_compacting(self):
        task_id = "compact-unloaded-thread"
        app = self.app
        self.make_task(task_id, "available")
        self.app.db.execute(
            "UPDATE tasks SET codex_session_id=? WHERE id=?",
            (task_id, task_id),
        )

        class FakeClient:
            def __init__(self):
                self.calls = []
                self.closed = False

            async def request(self, method, params):
                self.calls.append((method, params))
                if method == "thread/resume":
                    return {"thread": {"id": task_id}}
                if method == "thread/compact/start":
                    waiter = app.compaction_waiters.get(task_id)
                    if waiter and not waiter.done():
                        waiter.set_result({"threadId": task_id})
                    return {"ok": True}
                if method == "thread/unsubscribe":
                    return {"status": "unsubscribed"}
                raise AssertionError(method)

            async def close(self):
                self.closed = True

        async def exercise():
            client = FakeClient()
            task = self.app.task_or_404(task_id)
            key = self.app.appserver_key(None, task)
            self.app.app_servers[key] = client
            with mock.patch.object(self.app, "appserver_for", new=mock.AsyncMock(return_value=client)):
                result = await self.app.run_thread_operation(task, self.app.OperationIn(operation="compact"))
            self.assertTrue(result["ok"])
            self.assertEqual(
                ["thread/resume", "thread/compact/start", "thread/unsubscribe"],
                [method for method, _params in client.calls],
            )
            self.assertTrue(client.closed)
            self.assertNotIn(key, self.app.app_servers)

        try:
            asyncio.run(exercise())
        finally:
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_goal_pause_and_resume_slash_commands_use_button_semantics(self):
        task_id = "goal-slash-lifecycle"
        self.make_task(task_id, "available")
        self.app.db.execute("UPDATE tasks SET goal=?,goal_status='active' WHERE id=?", ("Finish the task", task_id))

        async def exercise():
            task = self.app.task_or_404(task_id)
            with mock.patch.object(self.app, "pause_goal", new=mock.AsyncMock(return_value={"goal_status": "paused"})) as pause, mock.patch.object(
                self.app, "start_goal", new=mock.AsyncMock(return_value={"goal_status": "active"})
            ) as start:
                paused = await self.app.execute_slash_command(task, "goal", "pause", ["pause"])
                resumed = await self.app.execute_slash_command(task, "goal", "resume", ["resume"])
            self.assertTrue(paused["ok"])
            self.assertIn("自动重试已关闭", paused["message"])
            self.assertTrue(resumed["ok"])
            pause.assert_awaited_once_with(task_id, None)
            start.assert_awaited_once_with(task_id, None)

        try:
            asyncio.run(exercise())
        finally:
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_goal_slash_view_is_local_and_edit_does_not_activate(self):
        task_id = "goal-slash-local-view"
        self.make_task(task_id, "available")
        self.app.db.execute(
            "UPDATE tasks SET goal=?,goal_status='paused',codex_session_id=? WHERE id=?",
            ("Existing objective", task_id, task_id),
        )

        async def exercise():
            task = self.app.task_or_404(task_id)
            with mock.patch.object(self.app, "appserver_for_task", new=mock.AsyncMock(side_effect=AssertionError("read must stay local"))):
                viewed = await self.app.execute_slash_command(task, "goal", "", [])
            self.assertEqual("edit_goal", viewed["ui_action"])
            self.assertEqual("Existing objective", viewed["goal"]["objective"])

            saved = {**task, "goal": "New objective", "goal_status": "paused"}
            with mock.patch.object(self.app, "patch_goal", new=mock.AsyncMock(return_value=saved)) as patch, mock.patch.object(
                self.app, "start_goal", new=mock.AsyncMock()
            ) as start:
                result = await self.app.execute_slash_command(task, "goal", "New objective", ["New", "objective"])
            patch.assert_awaited_once()
            start.assert_not_awaited()
            self.assertEqual("paused", result["goal"]["status"])
            self.assertIn("/goal resume", result["message"])

        try:
            asyncio.run(exercise())
        finally:
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_auto_retry_preserves_real_unfinished_goal_status(self):
        task_id = "auto-retry-public-goal-status"
        self.make_task(task_id, "stopped")
        self.app.db.execute(
            "UPDATE tasks SET goal=?,goal_status='blocked',retry_forever=1,retry_explicit=1 WHERE id=?",
            ("Keep working", task_id),
        )
        try:
            self.assertEqual("blocked", self.app.task_or_404(task_id)["goal_status"])
            self.assertEqual("blocked", self.app.task_summary(task_id)["goal_status"])
            listed = asyncio.run(self.app.list_tasks(None))
            self.assertEqual("blocked", next(row for row in listed if row["id"] == task_id)["goal_status"])
            raw = self.app.db.one("SELECT goal_status FROM tasks WHERE id=?", (task_id,))
            self.assertEqual("blocked", raw["goal_status"])
            self.app.db.execute("UPDATE tasks SET goal_status='complete' WHERE id=?", (task_id,))
            self.assertEqual("complete", self.app.task_or_404(task_id)["goal_status"])
        finally:
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_task_snapshots_expose_and_clear_external_running_state(self):
        task_id = "external-running-sidebar-state"
        self.make_task(task_id, "stopped")
        self.app.external_turns[task_id] = {
            "turn_id": "terminal-turn",
            "started_at": "2026-08-20T10:00:00+00:00",
            "phase": "running",
        }
        self.app.external_turn_sets[task_id] = {"terminal-turn": self.app.external_turns[task_id]}
        try:
            listed = asyncio.run(self.app.list_tasks(None))
            row = next(item for item in listed if item["id"] == task_id)
            self.assertTrue(row["external_running"])
            self.assertEqual("terminal-turn", row["external_turn_id"])
            self.app.clear_external_turns(task_id)
            cleared = self.app.task_summary(task_id)
            self.assertFalse(cleared["external_running"])
            self.assertIsNone(cleared["external_turn_id"])
            self.assertEqual(0, cleared["external_turn_count"])
        finally:
            self.app.clear_external_turns(task_id)
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_slash_command_catalog_only_advertises_implemented_commands(self):
        source = inspect.getsource(self.app.execute_slash_command) + inspect.getsource(self.app._execute_slash_command_body)
        for entry in self.app.SLASH_COMMANDS:
            self.assertIn(f'"{entry["name"]}"', source, entry["name"])
        advertised = {entry["name"] for entry in self.app.SLASH_COMMANDS}
        self.assertNotIn("vim", advertised)
        self.assertTrue(set(self.app.SLASH_ALIASES.values()).issubset(advertised))
        static = Path(__file__).resolve().parents[1] / "static"
        conversation = (static / "conversation.js").read_text(encoding="utf-8")
        frontend = (static / "app.js").read_text(encoding="utf-8")
        self.assertIn("palette.innerHTML = matches.map", conversation)
        self.assertNotIn("matches.slice(0, 18)", conversation)
        self.assertIn("palette.scrollTop += activeRect.bottom - paletteRect.bottom + 8", conversation)
        self.assertIn("event.stopPropagation(); state.commandIndex", frontend)

    def test_shift_enter_keeps_native_textarea_newline(self):
        source = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('if (event.key === "Enter" && event.shiftKey) return;', source)
        self.assertNotIn('if (event.key === "Enter" && event.shiftKey) { event.preventDefault(); return; }', source)

    def test_slash_toggle_arguments_reject_silent_misconfiguration(self):
        task_id = "slash-invalid-toggle"
        self.make_task(task_id, "available")

        async def exercise():
            task = self.app.task_or_404(task_id)
            for command, argument in (("fast", "maybe"), ("plan", "maybe"), ("raw", "maybe"), ("title", "maybe")):
                with self.assertRaises(self.app.HTTPException, msg=command):
                    await self.app.execute_slash_command(task, command, argument, [argument])
            with self.assertRaises(self.app.HTTPException):
                await self.app.execute_slash_command(task, "model", "effort=impossible", ["effort=impossible"])

        try:
            asyncio.run(exercise())
        finally:
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_slash_approve_resolves_latest_pending_approval(self):
        task_id = "slash-approve-pending"
        self.make_task(task_id, "running")

        async def exercise():
            future = asyncio.get_running_loop().create_future()
            self.app.pending_appserver_requests["approval-1"] = {
                "id": "approval-1",
                "task_id": task_id,
                "method": "item/commandExecution/requestApproval",
                "params": {},
                "future": future,
            }
            result = await self.app.execute_slash_command(self.app.task_or_404(task_id), "approve", "", [])
            self.assertTrue(result["ok"])
            self.assertEqual({"decision": "accept"}, future.result())

        try:
            asyncio.run(exercise())
        finally:
            self.app.pending_appserver_requests.pop("approval-1", None)
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_thread_control_compact_can_confirm_interrupting_active_turn(self):
        task_id = "compact-active-turn-confirmed"
        self.make_task(task_id, "running")

        async def stop(task_id_arg):
            self.assertEqual(task_id, task_id_arg)
            self.app.db.execute("UPDATE tasks SET status='stopped' WHERE id=?", (task_id,))
            return self.app.task_or_404(task_id)

        async def exercise():
            payload = self.app.OperationIn(operation="compact", args=["interrupt-active-turn"])
            with mock.patch.object(self.app, "stop_task_run", new=mock.AsyncMock(side_effect=stop)) as stopped, mock.patch.object(
                self.app, "run_thread_operation", new=mock.AsyncMock(return_value={"ok": True, "operation": "compact"})
            ) as compacted:
                result = await self.app.codex_operation(task_id, payload, None)
            self.assertTrue(result["ok"])
            stopped.assert_awaited_once()
            forwarded = compacted.await_args.args[1]
            self.assertNotIn("interrupt-active-turn", forwarded.args)

        try:
            asyncio.run(exercise())
        finally:
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_thread_control_compact_does_not_interrupt_without_confirmation(self):
        task_id = "compact-active-turn-unconfirmed"
        self.make_task(task_id, "running")

        async def exercise():
            with self.assertRaises(self.app.HTTPException) as caught:
                await self.app.codex_operation(task_id, self.app.OperationIn(operation="compact"), None)
            self.assertEqual(409, caught.exception.status_code)

        try:
            asyncio.run(exercise())
        finally:
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_running_thread_allows_rename_and_fork_metadata_operations(self):
        task_id = "running-thread-metadata-operations"
        self.make_task(task_id, "running")

        async def exercise():
            with mock.patch.object(self.app, "require_codex"), \
                 mock.patch.object(self.app, "USE_APP_SERVER", True), \
                 mock.patch.object(self.app, "run_thread_operation", new=mock.AsyncMock(return_value={"ok": True})) as operation:
                for name in ("rename", "fork"):
                    result = await self.app.codex_operation(task_id, self.app.OperationIn(operation=name), None)
                    self.assertTrue(result["ok"])
                self.assertEqual(["rename", "fork"], [call.args[1].operation for call in operation.await_args_list])
        try:
            asyncio.run(exercise())
        finally:
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_thread_rename_is_persisted_with_a_local_revision(self):
        task_id = "rename-thread-revision"
        self.make_task(task_id, "available")
        self.app.db.execute("UPDATE tasks SET codex_session_id=? WHERE id=?", (task_id, task_id))

        class FakeClient:
            async def request(self, method, params):
                if method == "thread/resume":
                    return {"thread": {"id": task_id}}
                if method == "thread/name/set":
                    self.name = params["name"]
                    return {}
                raise AssertionError(method)

        try:
            with mock.patch.object(self.app, "appserver_for", new=mock.AsyncMock(return_value=FakeClient())):
                result = asyncio.run(self.app.run_thread_operation(
                    self.app.task_or_404(task_id), self.app.OperationIn(operation="rename", args=["New dashboard name"]),
                ))
            renamed = self.app.task_or_404(task_id)
            self.assertEqual("New dashboard name", renamed["name"])
            self.assertEqual(1, renamed["name_revision"])
            self.assertEqual("New dashboard name", result["task"]["name"])
        finally:
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_installed_skills_are_discovered_and_personal_skills_are_managed(self):
        codex_root = self.app.CODEX_HOME / "skills"
        system_path = codex_root / ".system" / "system-skill" / "SKILL.md"
        personal_path = codex_root / "personal-skill" / "SKILL.md"
        system_path.parent.mkdir(parents=True, exist_ok=True)
        personal_path.parent.mkdir(parents=True, exist_ok=True)
        system_path.write_text("---\nname: system-skill\ndescription: Built in\n---\n\nSystem body\n", encoding="utf-8")
        personal_path.write_text("---\nname: personal-skill\ndescription: Personal\ncustom: keep-me\n---\n\nOld body\n", encoding="utf-8")

        with tempfile.TemporaryDirectory(dir=self.temp.name) as agent_directory:
            agent_root = Path(agent_directory) / "skills"
            agent_path = agent_root / "agent-skill" / "SKILL.md"
            agent_path.parent.mkdir(parents=True)
            agent_path.write_text("---\nname: agent-skill\ndescription: Agent\n---\n\nAgent body\n", encoding="utf-8")
            roots = (("Codex", codex_root, True), ("Agent", agent_root, True))
            with mock.patch.object(self.app, "installed_skill_roots", return_value=roots):
                rows = asyncio.run(self.app.list_skills(None))
                by_name = {row["name"]: row for row in rows}
                self.assertEqual("Codex system", by_name["system-skill"]["source"])
                self.assertTrue(by_name["system-skill"]["editable"])
                self.assertFalse(by_name["system-skill"]["deletable"])
                self.assertEqual("Agent", by_name["agent-skill"]["source"])
                self.assertTrue(by_name["agent-skill"]["editable"])
                self.assertTrue(by_name["agent-skill"]["deletable"])
                self.assertTrue(by_name["personal-skill"]["editable"])

                updated = asyncio.run(self.app.update_skill(
                    by_name["personal-skill"]["id"],
                    self.app.SkillIn(name="personal-skill-renamed", description="Updated", content="New body"),
                    None,
                ))
                renamed_path = codex_root / "personal-skill-renamed" / "SKILL.md"
                self.assertEqual("personal-skill-renamed", updated["name"])
                self.assertEqual("New body", updated["content"])
                self.assertFalse(personal_path.exists())
                metadata, body = self.app.parse_skill_document(renamed_path)
                self.assertEqual("keep-me", metadata["custom"])
                self.assertEqual("Updated", metadata["description"])
                self.assertEqual("New body", body.strip())

                created = asyncio.run(self.app.create_skill(
                    self.app.SkillIn(name="dashboard-created", description="Created", content="# Instructions"),
                    None,
                ))
                self.assertTrue(created["installed"])
                self.assertTrue(created["editable"])
                self.assertTrue(Path(created["path"]).is_file())
                asyncio.run(self.app.delete_skill(created["id"], None))
                self.assertFalse(Path(created["path"]).exists())

                system_updated = asyncio.run(self.app.update_skill(
                    by_name["system-skill"]["id"],
                    self.app.SkillIn(name="system-skill", description="Editable", content="Updated system body"),
                    None,
                ))
                self.assertEqual("Updated system body", system_updated["content"])
                with self.assertRaises(self.app.HTTPException) as protected_delete:
                    asyncio.run(self.app.delete_skill(system_updated["id"], None))
                self.assertEqual(403, protected_delete.exception.status_code)

    def test_skill_actions_distinguish_edit_from_protected_delete(self):
        static = Path(__file__).resolve().parents[1] / "static"
        settings = (static / "settings.js").read_text(encoding="utf-8")
        styles = (static / "styles.css").read_text(encoding="utf-8")
        html = (static / "index.html").read_text(encoding="utf-8")
        self.assertIn('class="danger-text" data-skill-delete', settings)
        self.assertIn('disabled title="${esc(uiLabel("protectedSkillDelete"))}"', settings)
        self.assertIn(".panel-item button.danger-text:not(:disabled)", styles)
        self.assertNotIn(".panel-item button:last-child", styles)
        self.assertIn('/styles.css?v=20260831-focus-wide', html)
        self.assertIn('/core.js?v=20260831-focus-wide', html)
        self.assertIn('/settings.js?v=20260825-directed-ssh-login', html)

    def test_provider_probe_status_and_endpoint_refresh(self):
        provider_id = f"provider-status-{os.getpid()}"
        history_task_id = f"provider-history-{os.getpid()}"
        stamp = self.app.now()
        self.app.db.execute(
            "INSERT INTO providers (id,name,kind,model,model_provider,base_url,enabled,priority,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (provider_id, "Provider status test", "codex", "test-model", "fake-provider", "http://127.0.0.1:1/v1", 1, 999, stamp, stamp),
        )

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        try:
            with mock.patch.object(self.app.urllib.request, "urlopen", side_effect=self.app.urllib.error.URLError("offline")):
                unavailable = asyncio.run(self.app.check_provider(provider_id, None))
            self.assertEqual("unavailable", unavailable["health_status"])
            self.assertIn("offline", unavailable["health_detail"])

            with mock.patch.object(self.app.urllib.request, "urlopen", return_value=Response()):
                healthy = asyncio.run(self.app.check_provider(provider_id, None))
            self.assertEqual("healthy", healthy["health_status"])
            self.assertIn("HTTP 200", healthy["health_detail"])

            self.app.record_provider_outcome(self.app.db.one("SELECT * FROM providers WHERE id=?", (provider_id,)), False, "runtime failed")
            failed = next(row for row in self.app.provider_public_rows() if row["id"] == provider_id)
            self.assertEqual("error", failed["health_status"])
            self.assertEqual(1, failed["failure_count"])
            self.app.record_provider_outcome(self.app.db.one("SELECT * FROM providers WHERE id=?", (provider_id,)), True)
            recovered = next(row for row in self.app.provider_public_rows() if row["id"] == provider_id)
            self.assertEqual("healthy", recovered["health_status"])
            self.assertEqual(1, recovered["success_count"])

            self.make_task(history_task_id, "succeeded")
            self.app.db.execute(
                "INSERT INTO sessions (id,task_id,status,attempt,provider_id,command,started_at) VALUES (?,?,?,?,?,?,?)",
                (f"{history_task_id}-session", history_task_id, "retrying", 1, provider_id, "codex", stamp),
            )
            public = next(row for row in self.app.provider_public_rows() if row["id"] == provider_id)
            self.assertEqual(0, public["in_use_count"])
        finally:
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (history_task_id,))
            self.app.db.execute("DELETE FROM providers WHERE id=?", (provider_id,))

    def test_provider_api_key_is_saved_but_never_returned(self):
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        payload = self.app.ProviderIn(
            name="Direct key test",
            model="test-model",
            model_provider="direct-key-test",
            api_key="dashboard-secret",
            base_url="https://provider.invalid/v1",
            priority=998,
        )
        provider_id = ""
        try:
            with mock.patch.object(self.app.urllib.request, "urlopen", return_value=Response()):
                created = asyncio.run(self.app.create_provider(payload, None))
            provider_id = created["id"]
            self.assertTrue(created["has_saved_key"])
            self.assertNotIn("api_key", created)
            self.assertNotIn("api_key_env", created)
            stored = self.app.db.one("SELECT * FROM providers WHERE id=?", (provider_id,))
            self.assertEqual("dashboard-secret", stored["api_key"])
            self.assertEqual("dashboard-secret", self.app.provider_api_key(stored))

            preserve = self.app.ProviderIn(
                name="Direct key test",
                model="test-model",
                model_provider="direct-key-test",
                base_url="https://provider.invalid/v1",
                priority=998,
            )
            with mock.patch.object(self.app.urllib.request, "urlopen", return_value=Response()):
                unchanged = asyncio.run(self.app.update_provider(provider_id, preserve, None))
            self.assertTrue(unchanged["has_saved_key"])
            self.assertEqual("dashboard-secret", self.app.db.one("SELECT api_key FROM providers WHERE id=?", (provider_id,))["api_key"])

            cleared = preserve.model_copy(update={"clear_api_key": True})
            with mock.patch.object(self.app.urllib.request, "urlopen", return_value=Response()):
                public = asyncio.run(self.app.update_provider(provider_id, cleared, None))
            self.assertFalse(public["has_saved_key"])
            self.assertEqual("", self.app.db.one("SELECT api_key FROM providers WHERE id=?", (provider_id,))["api_key"])
        finally:
            if provider_id:
                self.app.db.execute("DELETE FROM providers WHERE id=?", (provider_id,))

    def test_new_provider_must_verify_models_and_reject_duplicate_endpoint(self):
        class Response:
            status = 200

            def read(self):
                return b'{"data":[{"id":"model-a"},{"id":"model-b","name":"Model B"}]}'

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        base_url = f"https://provider-verify-{os.getpid()}.invalid/v1"
        provider_id = ""
        try:
            with mock.patch.object(self.app.urllib.request, "urlopen", return_value=Response()):
                verified = asyncio.run(self.app.verify_provider(self.app.ProviderVerifyIn(base_url=base_url, model="model-a", api_key="secret"), None))
            self.assertTrue(verified["ok"])
            self.assertEqual(["model-a", "model-b"], [item["id"] for item in verified["models"]])
            with mock.patch.object(self.app.urllib.request, "urlopen", return_value=Response()):
                created = asyncio.run(self.app.create_provider(self.app.ProviderIn(name="verify-test", model="model-a", api_key="secret", base_url=base_url), None))
            provider_id = created["id"]
            self.assertEqual("healthy", created["health_status"])
            with self.assertRaises(self.app.HTTPException) as duplicate:
                asyncio.run(self.app.create_provider(self.app.ProviderIn(name="duplicate", model="model-a", api_key="secret", base_url=base_url + "/"), None))
            self.assertEqual(409, duplicate.exception.status_code)
        finally:
            if provider_id:
                self.app.db.execute("DELETE FROM providers WHERE id=?", (provider_id,))

    def test_task_model_rows_merge_app_server_and_provider_models(self):
        rows = self.app.merge_model_rows(
            [{"id": "app-model"}, {"id": "shared", "name": "App"}],
            [{"id": "shared", "label": "Provider duplicate"}, {"id": "provider-model", "label": "Provider"}],
        )
        self.assertEqual(["app-model", "shared", "provider-model"], [row["id"] for row in rows])
        self.assertEqual("App", rows[1]["name"])

    def test_running_task_workspace_cannot_change_mid_turn(self):
        task_id = "workspace-running-thread"
        self.make_task(task_id, "running")
        with tempfile.TemporaryDirectory(dir=self.temp.name) as workspace:
            with self.assertRaises(self.app.HTTPException) as raised:
                asyncio.run(self.app.patch_task(task_id, self.app.TaskPatch(workspace=workspace)))
        self.assertEqual(409, raised.exception.status_code)
        self.assertEqual(self.temp.name, self.app.task_or_404(task_id)["workspace"])

    def test_tasks_are_always_sorted_by_latest_activity(self):
        older_id = "sort-running-older"
        newer_id = "sort-stopped-newer"
        self.make_task(older_id, "running")
        self.make_task(newer_id, "stopped")
        self.app.db.execute("UPDATE tasks SET updated_at=? WHERE id=?", ("2026-01-01T00:00:00+00:00", older_id))
        self.app.db.execute("UPDATE tasks SET updated_at=? WHERE id=?", ("2026-01-02T00:00:00+00:00", newer_id))
        with mock.patch.object(self.app, "sync_native_threads", new=mock.AsyncMock(return_value={})):
            rows = asyncio.run(self.app.list_tasks())
        ids = [row["id"] for row in rows]
        self.assertLess(ids.index(newer_id), ids.index(older_id))

    def test_inactive_sessions_move_to_recycle_bin_after_thirty_days(self):
        old_id = "inactive-trash-old"
        recent_id = "inactive-trash-recent"
        running_id = "inactive-trash-running"
        queued_id = "inactive-trash-queued"
        for task_id, status in ((old_id, "stopped"), (recent_id, "stopped"), (running_id, "running"), (queued_id, "stopped")):
            self.make_task(task_id, status)
        self.app.db.execute("UPDATE tasks SET last_interaction_at=? WHERE id=?", ("2026-07-01T00:00:00+00:00", old_id))
        self.app.db.execute("UPDATE tasks SET last_interaction_at=? WHERE id=?", ("2026-07-20T00:00:00+00:00", recent_id))
        self.app.db.execute("UPDATE tasks SET last_interaction_at=? WHERE id=?", ("2026-07-01T00:00:00+00:00", running_id))
        self.app.db.execute("UPDATE tasks SET last_interaction_at=? WHERE id=?", ("2026-07-01T00:00:00+00:00", queued_id))
        self.app.db.execute(
            "INSERT INTO task_messages (id,task_id,body,status,created_at) VALUES (?,?,?,?,?)",
            ("inactive-trash-pending-message", queued_id, "pending", "queued", "2026-07-01T00:00:00+00:00"),
        )
        try:
            moved = self.app.trash_inactive_tasks(datetime(2026, 8, 17, tzinfo=timezone.utc))
            self.assertEqual([old_id], moved)
            old = self.app.task_or_404(old_id)
            self.assertTrue(old["trashed"])
            self.assertEqual("trashed", old["status"])
            self.assertEqual("inactive_30_days", old["trashed_reason"])
            self.assertFalse(self.app.task_or_404(recent_id)["trashed"])
            self.assertFalse(self.app.task_or_404(running_id)["trashed"])
            self.assertFalse(self.app.task_or_404(queued_id)["trashed"])

            restored = asyncio.run(self.app.restore_trash(old_id, None))
            self.assertFalse(restored["trashed"])
            self.assertEqual("", restored["trashed_reason"])
            self.assertEqual(restored["updated_at"], restored["last_interaction_at"])
        finally:
            for task_id in (old_id, recent_id, running_id, queued_id):
                self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_chat_events_advance_last_interaction_timestamp(self):
        task_id = "last-interaction-event"
        session_id = "last-interaction-session"
        self.make_task(task_id, "stopped")
        self.app.db.execute(
            "INSERT INTO sessions (id,task_id,status,attempt,command,started_at) VALUES (?,?,?,?,?,?)",
            (session_id, task_id, "succeeded", 1, "codex", "2026-07-01T00:00:00+00:00"),
        )
        self.app.db.execute("UPDATE tasks SET last_interaction_at=? WHERE id=?", ("2026-07-01T00:00:00+00:00", task_id))
        try:
            self.app.db.execute(
                "INSERT INTO events (session_id,ts,stream,payload) VALUES (?,?,?,?)",
                (session_id, "2026-08-17T01:00:00+00:00", "app-server", json.dumps({"type": "agentMessage", "text": "done"})),
            )
            self.assertEqual("2026-08-17T01:00:00+00:00", self.app.task_or_404(task_id)["last_interaction_at"])
        finally:
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_queued_message_refreshes_task_recency_and_overview(self):
        task_id = "queued-message-recency"
        self.make_task(task_id, "stopped")
        old_stamp = "2026-01-01T00:00:00+00:00"
        self.app.db.execute("UPDATE tasks SET updated_at=? WHERE id=?", (old_stamp, task_id))

        class OverviewClient:
            def __init__(self):
                self.messages = []

            async def send_json(self, payload):
                self.messages.append(payload)

        client = OverviewClient()
        self.app.overview_clients.add(client)
        try:
            with mock.patch.object(self.app, "schedule_task_drain"):
                result = asyncio.run(self.app.enqueue_task_message(task_id, "latest input", "recency-message", "queue"))
            self.assertEqual("queued", result["status"])
            self.assertGreater(self.app.task_or_404(task_id)["updated_at"], old_stamp)
            updates = [message for message in client.messages if message.get("type") == "task_status"]
            self.assertEqual(1, len(updates))
            self.assertEqual(task_id, updates[0]["task"]["id"])
        finally:
            self.app.overview_clients.discard(client)

    def test_dispatch_queued_message_starts_selected_message(self):
        task_id = "dispatch-queued-message"
        message_id = "dispatch-message-1"
        self.make_task(task_id, "stopped")
        stamp = self.app.now()
        self.app.db.execute(
            "INSERT INTO task_messages (id,task_id,body,status,created_at) VALUES (?,?,?,?,?)",
            (message_id, task_id, "run this now", "queued", stamp),
        )
        try:
            with mock.patch.object(self.app, "launch", new=mock.AsyncMock(return_value={"session_id": "dispatch-session"})):
                result = asyncio.run(self.app.dispatch_task_message(task_id, message_id, None))
            self.assertEqual("dispatching", result["status"])
            self.assertEqual("dispatch-session", result["session_id"])
            self.assertEqual("dispatching", self.app.db.one("SELECT status FROM task_messages WHERE id=?", (message_id,))["status"])
        finally:
            self.app.db.execute("DELETE FROM task_messages WHERE id=?", (message_id,))
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_completed_appserver_owner_does_not_block_queued_message(self):
        task_id = "dispatch-after-completed-owner"
        message_id = "dispatch-after-completed-owner-message"
        self.make_task(task_id, "stopped")
        stamp = self.app.now()
        self.app.db.execute(
            "INSERT INTO task_messages (id,task_id,body,status,created_at) VALUES (?,?,?,?,?)",
            (message_id, task_id, "run after stale owner", "queued", stamp),
        )

        class FakeClient:
            def __init__(self):
                self.process = object()
                self.calls = []

            async def request(self, method, params):
                self.calls.append((method, params))
                return {"status": "unsubscribed"}

        async def exercise():
            client = FakeClient()
            completed = asyncio.create_task(asyncio.sleep(0))
            await completed
            self.app.app_servers["stale-owner"] = client
            self.app.appserver_turn_tasks[task_id] = completed
            self.app.appserver_turn_ids[task_id] = "stale-turn"
            self.app.app_thread_bindings[task_id] = ("stale-owner", task_id, "stale-session")
            self.app.running[task_id] = client.process
            with mock.patch.object(self.app, "launch", new=mock.AsyncMock(return_value={"session_id": "fresh-session"})):
                result = await self.app.dispatch_task_message(task_id, message_id, None)
            self.assertEqual("dispatching", result["status"])
            self.assertEqual([("thread/unsubscribe", {"threadId": task_id})], client.calls)
            self.assertNotIn(task_id, self.app.appserver_turn_tasks)
            self.assertNotIn(task_id, self.app.running)

        try:
            asyncio.run(exercise())
        finally:
            self.app.app_servers.pop("stale-owner", None)
            self.app.appserver_turn_tasks.pop(task_id, None)
            self.app.appserver_turn_ids.pop(task_id, None)
            self.app.app_thread_bindings.pop(task_id, None)
            self.app.running.pop(task_id, None)
            self.app.db.execute("DELETE FROM task_messages WHERE task_id=?", (task_id,))
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_queued_message_waits_for_active_turn_then_dispatches(self):
        task_id = "queued-message-auto-dispatch"
        message_id = "queued-message-auto-dispatch-1"
        self.make_task(task_id, "running")
        stamp = self.app.now()
        self.app.db.execute(
            "INSERT INTO task_messages (id,task_id,body,status,created_at) VALUES (?,?,?,?,?)",
            (message_id, task_id, "continue after the current turn", "queued", stamp),
        )
        launched = asyncio.Event()

        async def fake_launch(*_args, **_kwargs):
            launched.set()
            return {"session_id": "queued-auto-session"}

        async def exercise():
            with mock.patch.object(self.app, "launch", new=fake_launch):
                self.app.schedule_task_drain(task_id)
                await asyncio.sleep(0.08)
                self.assertFalse(launched.is_set(), "an active turn must not be interrupted")
                self.app.db.execute("UPDATE tasks SET status='available' WHERE id=?", (task_id,))
                await asyncio.wait_for(launched.wait(), timeout=1)
                worker = self.app.task_workers.get(task_id)
                if worker:
                    await worker

        try:
            asyncio.run(exercise())
            row = self.app.db.one("SELECT status,session_id FROM task_messages WHERE id=?", (message_id,))
            self.assertEqual("dispatching", row["status"])
            self.assertEqual("queued-auto-session", row["session_id"])
        finally:
            self.app.task_workers.pop(task_id, None)
            self.app.db.execute("DELETE FROM task_messages WHERE id=?", (message_id,))
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_completed_message_is_idempotent_and_not_requeued(self):
        task_id = "completed-message-not-requeued"
        message_id = "completed-message-1"
        self.make_task(task_id, "available")
        stamp = self.app.now()
        self.app.db.execute(
            "INSERT INTO task_messages (id,task_id,body,status,created_at,finished_at) VALUES (?,?,?,?,?,?)",
            (message_id, task_id, "already delivered", "sent", stamp, stamp),
        )
        try:
            with mock.patch.object(self.app, "schedule_task_drain") as schedule:
                result = asyncio.run(self.app.enqueue_task_message(task_id, "already delivered", message_id, "queue"))
            self.assertEqual("sent", result["status"])
            self.assertEqual("sent", self.app.db.one("SELECT status FROM task_messages WHERE id=?", (message_id,))["status"])
            schedule.assert_not_called()
        finally:
            self.app.db.execute("DELETE FROM task_messages WHERE id=?", (message_id,))
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_restart_requeues_interrupted_message_delivery_states(self):
        task_id = "restart-requeues-message-states"
        self.make_task(task_id, "available")
        stamp = self.app.now()
        message_ids = []
        delivered_id = "restart-running-delivered"
        try:
            for status in ("running", "dispatching", "steering"):
                message_id = f"restart-{status}"
                message_ids.append(message_id)
                self.app.db.execute(
                    "INSERT INTO task_messages (id,task_id,body,status,created_at,started_at,session_id,error) VALUES (?,?,?,?,?,?,?,?)",
                    (message_id, task_id, status, status, stamp, stamp, f"session-{status}", "old error"),
                )
            message_ids.append(delivered_id)
            self.app.db.execute(
                "INSERT INTO sessions (id,task_id,status,attempt,command,started_at) VALUES (?,?,?,?,?,?)",
                ("restart-delivered-session", task_id, "running", 1, "codex app-server", stamp),
            )
            self.app.db.execute(
                "INSERT INTO task_messages (id,task_id,body,status,created_at,started_at,session_id,error) VALUES (?,?,?,?,?,?,?,?)",
                (delivered_id, task_id, "delivered", "running", stamp, stamp, "restart-delivered-session", "old error"),
            )
            self.app.db.execute(
                "INSERT INTO events (session_id,task_id,ts,stream,payload) VALUES (?,?,?,?,?)",
                (
                    "restart-delivered-session",
                    task_id,
                    stamp,
                    "app-server",
                    json.dumps({"type": "userMessage", "text": "delivered", "client_message_id": delivered_id}),
                ),
            )
            with self.app.db.lock, self.app.db.connect() as connection:
                self.app.db._recover_interrupted_work(connection)
                connection.commit()
            rows = self.app.db.all(
                f"SELECT id,status,started_at,finished_at,session_id,error FROM task_messages WHERE id IN ({','.join('?' for _ in message_ids)})",
                tuple(message_ids),
            )
            by_id = {row["id"]: row for row in rows}
            self.assertEqual("sent", by_id[delivered_id]["status"])
            self.assertEqual("", by_id[delivered_id]["error"])
            self.assertIsNotNone(by_id[delivered_id]["finished_at"])
            queued = [row for row in rows if row["id"] != delivered_id]
            self.assertEqual({"queued"}, {row["status"] for row in queued})
            self.assertEqual({None}, {row["started_at"] for row in queued})
            self.assertEqual({None}, {row["finished_at"] for row in queued})
            self.assertEqual({None}, {row["session_id"] for row in queued})
            self.assertEqual({"Dashboard restarted before delivery"}, {row["error"] for row in queued})
        finally:
            for message_id in message_ids:
                self.app.db.execute("DELETE FROM task_messages WHERE id=?", (message_id,))
            self.app.db.execute("DELETE FROM events WHERE session_id='restart-delivered-session'")
            self.app.db.execute("DELETE FROM sessions WHERE id='restart-delivered-session'")
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_startup_reconcile_marks_delivered_messages_sent(self):
        task_id = "startup-delivered-message"
        session_id = "startup-delivered-session"
        message_id = "startup-delivered-id"
        stamp = self.app.now()
        self.make_task(task_id, "available")
        try:
            self.app.db.execute(
                "INSERT INTO sessions (id,task_id,status,attempt,command,started_at) VALUES (?,?,?,?,?,?)",
                (session_id, task_id, "interrupted", 1, "codex app-server", stamp),
            )
            self.app.db.execute(
                "INSERT INTO task_messages (id,task_id,body,status,created_at,error) VALUES (?,?,?,?,?,?)",
                (message_id, task_id, "already sent", "queued", stamp, "Dashboard restarted before delivery"),
            )
            self.app.db.execute(
                "INSERT INTO events (session_id,task_id,ts,stream,payload) VALUES (?,?,?,?,?)",
                (session_id, task_id, stamp, "app-server", json.dumps({"type": "userMessage", "client_message_id": message_id, "text": "already sent"})),
            )
            self.app.reconcile_delivered_task_messages()
            row = self.app.db.one("SELECT status,error,finished_at FROM task_messages WHERE id=?", (message_id,))
            self.assertEqual("sent", row["status"])
            self.assertEqual("", row["error"])
            self.assertIsNotNone(row["finished_at"])
        finally:
            self.app.db.execute("DELETE FROM task_messages WHERE id=?", (message_id,))
            self.app.db.execute("DELETE FROM events WHERE session_id=?", (session_id,))
            self.app.db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_startup_drain_launches_saved_active_goal_and_queued_message(self):
        retry_task = "startup-active-goal-launch"
        queued_task = "startup-queued-message-launch"
        stopped_queue_task = "startup-stopped-queue-waits"
        message_id = "startup-queued-message"
        stopped_message_id = "startup-stopped-queued-message"
        stamp = self.app.now()
        self.make_task(retry_task, "available")
        self.make_task(queued_task, "available")
        self.make_task(stopped_queue_task, "stopped")
        try:
            self.app.db.execute(
                "UPDATE tasks SET goal='continue later',goal_status='active',retry_forever=1 WHERE id=?",
                (retry_task,),
            )
            self.app.db.execute(
                "INSERT INTO task_messages (id,task_id,body,status,created_at) VALUES (?,?,?,?,?)",
                (message_id, queued_task, "explicit queued input", "queued", stamp),
            )
            self.app.db.execute(
                "INSERT INTO task_messages (id,task_id,body,status,created_at) VALUES (?,?,?,?,?)",
                (stopped_message_id, stopped_queue_task, "wait for resume", "queued", stamp),
            )
            startup_ids = self.app.startup_drain_task_ids()
            self.assertIn(queued_task, startup_ids)
            self.assertIn(retry_task, startup_ids)
            self.assertNotIn(stopped_queue_task, startup_ids)
        finally:
            self.app.db.execute("DELETE FROM task_messages WHERE id IN (?,?)", (message_id, stopped_message_id))
            self.app.db.execute("DELETE FROM tasks WHERE id IN (?,?,?)", (retry_task, queued_task, stopped_queue_task))

    def test_port_configuration_rejects_invalid_values(self):
        with mock.patch.dict(os.environ, {"TEST_DASHBOARD_PORT": "9443"}):
            self.assertEqual(9443, self.app.configured_port("TEST_DASHBOARD_PORT", 8787))
        for value in ("0", "65536", "not-a-port"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"TEST_DASHBOARD_PORT": value}):
                with self.assertRaises(RuntimeError):
                    self.app.configured_port("TEST_DASHBOARD_PORT", 8787)

    def test_health_reports_effective_bind_address(self):
        result = asyncio.run(self.app.health())
        self.assertEqual(self.app.DASHBOARD_HOST, result["bind_host"])
        self.assertEqual(self.app.DASHBOARD_PORT, result["bind_port"])
        self.assertTrue(result["server_user"])
        self.assertTrue(result["server_hostname"])

    def test_every_static_button_is_wired_to_client_behavior(self):
        static = Path(__file__).resolve().parents[1] / "static"
        html = (static / "index.html").read_text(encoding="utf-8")
        scripts = "\n".join(
            (static / name).read_text(encoding="utf-8")
            for name in ("core.js", "conversation.js", "exploration-tree.js", "settings.js", "app.js")
        )
        button_ids = set(re.findall(r'<button[^>]+\bid="([^"]+)"', html))
        missing = sorted(
            button_id
            for button_id in button_ids
            if f'#{button_id}' not in scripts and f'"{button_id}"' not in scripts
        )
        self.assertEqual([], missing)
        self.assertNotIn('id="refresh"', html)

    def test_conversation_chrome_uses_themed_dialogs_scrollbars_and_i18n(self):
        static = Path(__file__).resolve().parents[1] / "static"
        html = (static / "index.html").read_text(encoding="utf-8")
        core_js = (static / "core.js").read_text(encoding="utf-8")
        scripts = "\n".join(
            (static / name).read_text(encoding="utf-8")
            for name in ("core.js", "conversation.js", "settings.js", "app.js")
        )
        styles = (static / "styles.css").read_text(encoding="utf-8")

        self.assertIn('<dialog id="app-dialog"', html)
        self.assertIn("function showAppDialog", core_js)
        self.assertNotRegex(scripts, r"(?<![A-Za-z0-9_])(confirm|prompt|alert)\(")
        self.assertIn("*::-webkit-scrollbar-thumb", styles)
        self.assertIn("scrollbar-color:", styles)
        self.assertIn('id="turn-progress-tip"', html)
        self.assertIn('id="media-viewer"', html)
        self.assertIn("tipEvidence", core_js)
        self.assertIn("phaseAnalyzing", core_js)
        self.assertIn("standbyTip", core_js)
        self.assertIn("queueEmpty", core_js)
        self.assertIn("message-meta", scripts)
        worker_js = (static / "chat-worker.js").read_text(encoding="utf-8")
        self.assertIn("numeric < 1e12 ? numeric * 1000 : numeric", worker_js)
        self.assertIn("timestamp(a.event) - timestamp(b.event) || a.index - b.index", worker_js)
        self.assertIn('payload.client_message_id || payload.item_id', worker_js)
        self.assertIn("seenUserKeys.has(id)", worker_js)
        self.assertIn("seenUserBodies.get(body)", worker_js)
        self.assertIn("function openChatFileViewer", scripts)
        self.assertIn("toastui.Editor.factory", scripts)
        self.assertIn("media-viewer-frame", styles)
        self.assertNotIn('id="turn-progress-title"', html)
        self.assertNotIn('id="turn-progress-detail"', html)
        self.assertNotIn('id="turn-progress-elapsed"', html)
        self.assertIn(".turn-progress-inner { position: relative; height: 37px", styles)
        self.assertIn(".goal-bar { width: auto; min-width: 0; height: 37px", styles)
        self.assertIn(".turn-progress.idle .turn-progress-inner", styles)
        self.assertIn(".queued-empty", styles)

    def test_provider_management_is_centralized_in_connection_panel(self):
        static = Path(__file__).resolve().parents[1] / "static"
        html = (static / "index.html").read_text(encoding="utf-8")
        app_js = (static / "app.js").read_text(encoding="utf-8")
        settings_js = (static / "settings.js").read_text(encoding="utf-8")
        self.assertNotIn('data-panel="providers"', html)
        self.assertNotIn("button.dataset.settings", app_js)
        self.assertIn('if (button.id === "connection-status") return openPanel("server")', app_js)
        self.assertIn("connection-providers", settings_js)
        self.assertIn("data-provider-use", settings_js)
        self.assertIn("data-provider-edit", settings_js)
        self.assertIn("data-provider-check", settings_js)
        self.assertIn("/providers/verify", settings_js)
        self.assertIn("provider-submit", settings_js)
        self.assertIn("provider-verified-model", settings_js)
        self.assertIn('name="model" id="provider-edit-model"', settings_js)
        self.assertIn("button.dataset.providerUse", app_js)
        self.assertIn("模型列表 · ${visible.length}/${normalizedModels.length}", settings_js)
        self.assertIn("连接地址和 API Key 都必须填写", settings_js)
        self.assertNotRegex(settings_js, r'模型<input[^>]*name="model"')
        schemas = (Path(__file__).resolve().parents[1] / "codex_partner/schemas.py").read_text(encoding="utf-8")
        app_py = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
        self.assertIn("ProviderVerifyIn", schemas)
        self.assertNotIn("len(result) >= 200", app_py)

    def test_realtime_indicator_does_not_use_ssh_disconnected_label(self):
        static = Path(__file__).resolve().parents[1] / "static"
        core_js = (static / "core.js").read_text(encoding="utf-8")
        app_js = (static / "app.js").read_text(encoding="utf-8")
        settings_js = (static / "settings.js").read_text(encoding="utf-8")
        conversation_js = (static / "conversation.js").read_text(encoding="utf-8")
        styles = (static / "styles.css").read_text(encoding="utf-8")
        self.assertNotIn("服务器未连接", core_js)
        self.assertIn("location.hostname", core_js)
        self.assertIn("serverLabel.textContent = webHost", core_js)
        self.assertIn('overlayOpen ? " panel-open"', core_js)
        self.assertIn("网页服务已连接", core_js)
        self.assertNotIn("state.sshHosts?.find", core_js)
        self.assertIn('openPanel("server")', app_js)
        self.assertNotIn("loadSSHHosts(true).then", app_js)
        self.assertIn("renderServerPanel", settings_js)
        self.assertIn('ssh_host: ""', settings_js)
        self.assertIn("fetch(`/api${path}`", core_js)
        self.assertIn("location.host}/ws/overview", conversation_js)
        self.assertIn("location.host}/ws/tasks/", conversation_js)
        self.assertIn("currentProvider()", core_js)
        self.assertIn("connection-provider-label", (static / "index.html").read_text(encoding="utf-8"))
        self.assertIn("connection-metrics-label", (static / "index.html").read_text(encoding="utf-8"))
        self.assertIn("TTFT", core_js)
        self.assertIn("TPOT", core_js)
        self.assertIn(".conversation-title h2", styles)
        self.assertIn("overflow-wrap: anywhere", styles)

    def test_conversation_controls_live_below_composer(self):
        static = Path(__file__).resolve().parents[1] / "static"
        html = (static / "index.html").read_text(encoding="utf-8")
        app_js = (static / "app.js").read_text(encoding="utf-8")
        conversation_js = (static / "conversation.js").read_text(encoding="utf-8")
        self.assertIn('id="composer-meta"', html)
        self.assertNotIn('id="conversation-meta"', html)
        self.assertNotIn('id="stop-generation"', html)
        self.assertIn('$("#composer-model-select")', conversation_js)
        self.assertIn('当前 API ·', (static / "core.js").read_text(encoding="utf-8"))
        self.assertIn('const delivery = activeTurn && !takeoverTerminal ? "queue" : "auto"', app_js)
        self.assertIn('function toggleGoalRun()', app_js)
        self.assertIn('id="composer-model-select"', html)
        self.assertIn('id="composer-effort-select"', html)
        self.assertIn('id="composer-context-usage"', html)
        self.assertIn("calculateContextUsage", (static / "core.js").read_text(encoding="utf-8"))
        self.assertIn('payload.method !== "thread/tokenUsage/updated"', (static / "core.js").read_text(encoding="utf-8"))
        self.assertIn('/tasks/${encodeURIComponent(task.id)}/commands', conversation_js)
        self.assertIn('command: `/model ${parts.join(" ")}`', conversation_js)
        self.assertIn('result?.ok', conversation_js)
        self.assertIn('class="composer-meta-tools"', html)
        self.assertIn('id="attach-button"', html)
        self.assertIn('new TextDecoder("utf-8", { fatal: true })', app_js)
        self.assertIn("binarySignature", app_js)
        self.assertIn("uploadWorkspaceFile(state.selectedId", app_js)
        self.assertIn("attachmentUploadName", app_js)
        self.assertIn("new File([file]", app_js)
        self.assertIn('uiLabel("binaryAttachment"', app_js)
        self.assertIn("let threadOperationInFlight = false", app_js)
        self.assertIn('runOperation("fork", button)', app_js)
        self.assertIn('toast(uiLabel("sessionDuplicating"))', app_js)
        self.assertIn('mergeTask(result.task)', app_js)
        self.assertIn('forkCreated ? "会话已复制，但打开副本失败" : "复制会话失败"', app_js)
        self.assertIn('uiLabel("sessionRenamed")', app_js)
        self.assertIn('uiLabel("sessionDuplicated")', app_js)
        self.assertIn('/app.js?v=20260831-focus-wide', html)
        self.assertIn('/core.js?v=20260831-focus-wide', html)
        self.assertIn('responseErrorMessage(response)', (static / "core.js").read_text(encoding="utf-8"))
        self.assertIn('/mascot-dance.js?v=20260816-game-sprites', html)
        self.assertIn('/conversation.js?v=20260831-focus-wide', html)
        self.assertIn("/timeline?limit=120", conversation_js)
        self.assertIn("new Worker", conversation_js)
        self.assertIn("chatVirtualStart", conversation_js)
        self.assertIn("appendStreamingDelta", conversation_js)
        worker_js = (static / "chat-worker.js").read_text(encoding="utf-8")
        self.assertIn("chat-image-attachment", worker_js)
        self.assertIn("(?:Attachments?|附件)", worker_js)
        self.assertIn('id="command-button"', html)
        self.assertIn('id="permission-toggle"', html)
        self.assertIn('id="composer-live-state"', html)
        self.assertNotIn('id="composer-thread-id"', html)
        self.assertIn('id="conversation-subtitle"', html)
        self.assertIn("function renderComposerModelSelect", conversation_js)
        self.assertIn("function renderComposerEffortSelect", conversation_js)
        self.assertIn('uiLabel("threadId")', conversation_js)
        self.assertIn("grid-template-columns: 190px minmax(0, 1fr)", (static / "styles.css").read_text(encoding="utf-8"))
        self.assertIn(".queued-messages[hidden] { display: block !important", (static / "styles.css").read_text(encoding="utf-8"))
        self.assertIn("min-height: 37px", (static / "styles.css").read_text(encoding="utf-8"))
        self.assertNotIn("未设置 Goal，点击修改后让 Codex 持续追踪目标", conversation_js)
        self.assertIn('launch_after_turn_cleanup(task["id"], "goal-resume", "", "", set())', (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8"))
        self.assertIn("flex-wrap: nowrap", (static / "styles.css").read_text(encoding="utf-8"))

    def test_composer_model_picker_survives_realtime_status_renders(self):
        static = Path(__file__).resolve().parents[1] / "static"
        conversation_js = (static / "conversation.js").read_text(encoding="utf-8")
        styles = (static / "styles.css").read_text(encoding="utf-8")
        self.assertIn("document.activeElement === select", conversation_js)
        self.assertIn('select.dataset.pendingOptions = "1"', conversation_js)
        self.assertIn("select.dataset.optionsSignature !== signature", conversation_js)
        self.assertIn('select.addEventListener("blur"', conversation_js)
        self.assertIn(".composer-model-control .composer-select", styles)
        self.assertIn("cursor: pointer", styles)
        self.assertIn(".composer-dock { grid-template-columns: minmax(0, 1fr);", styles)
        self.assertIn(".composer-model-control { order: -3; }", styles)
        self.assertIn("height: calc(100dvh - 58px)", styles)
        self.assertIn(".composer-meta button, .composer-meta select", styles)
        self.assertIn("env(safe-area-inset-bottom)", styles)

    def test_active_task_status_is_reconciled_after_missed_completion_event(self):
        static = Path(__file__).resolve().parents[1] / "static"
        core_js = (static / "core.js").read_text(encoding="utf-8")
        app_js = (static / "app.js").read_text(encoding="utf-8")
        self.assertIn("async function reconcileSelectedTaskStatus()", core_js)
        self.assertIn("authoritative.status !== state.selectedTask.status", core_js)
        self.assertIn("setInterval(reconcileSelectedTaskStatus, 5000)", app_js)
        self.assertIn("reconcileSelectedTaskStatus();", app_js)

    def test_goal_pause_button_tracks_goal_status_across_turn_surfaces(self):
        root = Path(__file__).resolve().parents[1]
        conversation_js = (root / "static" / "conversation.js").read_text(encoding="utf-8")
        app_js = (root / "static" / "app.js").read_text(encoding="utf-8")
        database_py = (root / "codex_partner" / "database.py").read_text(encoding="utf-8")
        self.assertIn("function goalIsActive", conversation_js)
        self.assertIn('(task.goal_status || "none") === "active"', conversation_js)
        self.assertIn("interactionState(task)", app_js)
        self.assertIn('controls.goalAction === "pause"', app_js)
        self.assertIn('active: "enabled"', conversation_js)
        self.assertIn('goalProgress.textContent = goalStatusLabel', conversation_js)
        self.assertIn('function goalStatusSummary', conversation_js)
        self.assertIn('function goalCanAutoResume', conversation_js)
        self.assertIn("function interactionState", conversation_js)
        self.assertIn('Goal 和会话已暂停', app_js)
        self.assertIn('${uiLabel("goalStatusPrefix")}：${goalStatusLabel', conversation_js)
        self.assertNotIn('statusLabel(task.status)}', conversation_js)
        self.assertNotIn('sessionStatusPrefix', conversation_js)
        self.assertNotIn('active: "running"', conversation_js)
        self.assertIn("authoritative.run_mode !== state.selectedTask.run_mode", (root / "static" / "core.js").read_text(encoding="utf-8"))
        self.assertIn("run_mode TEXT DEFAULT ''", database_py)
        self.assertEqual("message", self.app.requested_run_mode({"goal": "ship it"}, "message", "message-1"))
        self.assertEqual("operation", self.app.requested_run_mode({"goal": "ship it"}, "resume"))
        self.assertEqual("goal_resume", self.app.requested_run_mode({"goal": "ship it"}, "goal-resume"))
        self.assertEqual("operation", self.app.requested_run_mode({"goal": ""}, "resume"))

    def test_appserver_supervisor_receives_explicit_run_mode(self):
        import inspect

        signature = inspect.signature(self.app.supervise_appserver_turn)
        self.assertIn("run_mode", signature.parameters)
        source = inspect.getsource(self.app.launch_appserver)
        self.assertIn("supervise_appserver_turn(task, provider, mode, run_mode, message", source)

    def test_browser_auth_uses_ssh_cookie_instead_of_access_tokens(self):
        static = Path(__file__).resolve().parents[1] / "static"
        core_js = (static / "core.js").read_text(encoding="utf-8")
        conversation_js = (static / "conversation.js").read_text(encoding="utf-8")
        html = (static / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="ssh-login-dialog"', html)
        self.assertNotIn('id="ssh-login-server"', html)
        self.assertIn("requestSSHLogin", core_js)
        self.assertIn('fetch("/api/auth/login"', core_js)
        login_block = core_js[core_js.index("async function requestSSHLogin") : core_js.index("async function api")]
        self.assertNotIn("location.hostname", login_block)
        self.assertNotIn("/api/auth/status", login_block)
        settings_js = (static / "settings.js").read_text(encoding="utf-8")
        password_form = settings_js[settings_js.index("function openSSHPasswordForm") : settings_js.index("async function checkAllProviders")]
        self.assertNotIn("item.hostname", password_form.split('$("#cancel")')[0])
        self.assertNotIn("codex-dashboard-token", core_js + conversation_js)
        self.assertNotIn("?token=", conversation_js)

    def start_lock_holder(self, thread_id: str):
        """Start a real process that holds one thread's Codex writer lock."""
        lock = self.app.thread_writer_lock_path(thread_id)
        lock.parent.mkdir(parents=True, exist_ok=True)
        holder = subprocess.Popen(
            [sys.executable, "-c", "import sys,time;f=open(sys.argv[1],'w');time.sleep(60)", str(lock)],
        )
        for _ in range(100):
            if lock.is_file() and self.app.rollout_writer_pids(str(lock), refresh=True):
                return holder
            time.sleep(0.05)
        holder.terminate()
        holder.wait()
        self.fail("lock holder never appeared")

    def insert_session(self, task_id: str, session_id: str) -> None:
        self.app.db.execute(
            "INSERT INTO sessions (id,task_id,status,attempt,command,started_at) VALUES (?,?,?,?,?,?)",
            (session_id, task_id, "running", 1, "codex app-server turn/start", self.app.now()),
        )

    def test_foreign_thread_writers_only_report_processes_outside_the_dashboard(self):
        thread_id = "external-owner-1"
        holder = self.start_lock_holder(thread_id)
        try:
            writers = self.app.foreign_thread_writers(thread_id)
            self.assertEqual([holder.pid], [writer["pid"] for writer in writers])
            self.assertIn("time.sleep", writers[0]["command"])
            self.assertEqual([], self.app.foreign_thread_writers("no-such-thread"))
        finally:
            holder.terminate()
            holder.wait()

    def test_foreign_writer_marker_survives_restarts_and_clears_when_the_process_exits(self):
        task_id = "foreign-writer-marker"
        self.make_task(task_id, "running")
        state = self.app.persist_foreign_writer(task_id, [{"pid": os.getpid(), "command": "codex --yolo"}])
        self.assertTrue(state["detected_at"])
        snapshot = self.app.task_or_404(task_id)
        self.assertTrue(snapshot["foreign_writer_active"])
        self.assertEqual([os.getpid()], snapshot["foreign_writer_pids"])
        self.assertEqual(["codex --yolo"], snapshot["foreign_writer_commands"])
        # A process that already exited must clear the marker instead of pinning the badge.
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        self.app.persist_foreign_writer(task_id, [{"pid": dead.pid, "command": "codex"}])
        snapshot = self.app.task_or_404(task_id)
        self.assertFalse(snapshot["foreign_writer_active"])
        self.assertEqual([], snapshot["foreign_writer_pids"])
        self.assertEqual("", self.app.db.one("SELECT foreign_writer FROM tasks WHERE id=?", (task_id,))["foreign_writer"])
        self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_foreign_writer_failure_marks_the_task_and_stops_retrying(self):
        task_id = "foreign-writer-failure"
        session_id = "foreign-writer-session"
        message_id = "foreign-writer-message"
        self.make_task(task_id, "running")
        self.insert_session(task_id, session_id)
        self.app.db.execute(
            "UPDATE tasks SET retry_forever=1,goal='keep going',goal_status='active' WHERE id=?",
            (task_id,),
        )
        task = self.app.task_or_404(task_id)
        error = json.dumps({"code": -32600, "message": f"thread {task_id} already has an active writer"})
        self.assertTrue(self.app.active_writer_error(error))
        asyncio.run(self.app.fail_on_foreign_thread_writer(
            task, task_id, session_id, message_id, error, [{"pid": os.getpid(), "command": "codex --yolo"}],
        ))
        row = self.app.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        self.assertEqual("failed", row["status"])
        self.assertIn("owned by another Codex process", row["last_error"])
        self.assertIn(f"pid {os.getpid()}", row["last_error"])
        self.assertEqual("", row["execution_source"])
        self.assertEqual("", row["run_mode"])
        session = self.app.db.one("SELECT * FROM sessions WHERE id=?", (session_id,))
        # Nothing was queued for another attempt: the writer can never be satisfied.
        self.assertEqual("failed", session["status"])
        self.assertTrue(self.app.task_or_404(task_id)["foreign_writer_active"])
        self.app.db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_supervise_turn_reports_an_external_writer_instead_of_looping(self):
        task_id = "foreign-writer-supervisor"
        session_id = "foreign-writer-supervisor-session"
        self.make_task(task_id, "running")
        self.insert_session(task_id, session_id)
        self.app.db.execute(
            "UPDATE tasks SET retry_forever=1,goal='keep going',goal_status='active',retry_count=7 WHERE id=?",
            (task_id,),
        )
        task = self.app.task_or_404(task_id)
        error = RuntimeError(json.dumps({"code": -32600, "message": "thread already has an active writer"}))
        with mock.patch.object(self.app, "appserver_for", new=mock.AsyncMock(side_effect=error)), \
             mock.patch.object(self.app, "foreign_thread_writers", return_value=[{"pid": os.getpid(), "command": "codex --yolo"}]), \
             mock.patch.object(self.app, "launch_after_turn_cleanup", new=mock.AsyncMock()) as retry:
            asyncio.run(self.app.supervise_appserver_turn(task, None, "message", "", "hello", "", session_id, set()))
        retry.assert_not_awaited()
        row = self.app.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        self.assertEqual("failed", row["status"])
        self.assertIn("owned by another Codex process", row["last_error"])
        self.assertIn(f"pid {os.getpid()}", row["last_error"])
        # Goal auto-resume and the queue worker both stay parked on the marker.
        self.assertTrue(self.app.task_or_404(task_id)["foreign_writer_active"])
        self.assertEqual([], self.app.db.all("SELECT id FROM sessions WHERE task_id=? AND status!='failed'", (task_id,)))
        self.app.db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_any_codex_process_holding_a_thread_is_marked_without_a_failed_turn(self):
        task_id = "foreign-writer-proactive"
        thread_id = "foreign-writer-proactive-thread"
        self.make_task(task_id, "running")
        self.app.db.execute("UPDATE tasks SET codex_session_id=?,status='available' WHERE id=?", (thread_id, task_id))
        self.assertFalse(self.app.task_or_404(task_id)["foreign_writer_active"])
        holder = self.start_lock_holder(thread_id)
        try:
            self.app.refresh_foreign_writer_cache(force=True)
            snapshot = self.app.task_or_404(task_id)
            self.assertTrue(snapshot["foreign_writer_active"])
            self.assertEqual([holder.pid], snapshot["foreign_writer_pids"])
            self.assertIn("time.sleep", snapshot["foreign_writer_commands"][0])
            stored = self.app.db.one("SELECT foreign_writer FROM tasks WHERE id=?", (task_id,))["foreign_writer"]
            self.assertIn(str(holder.pid), stored)
        finally:
            holder.terminate()
            holder.wait()
        # Once that process exits the marker clears itself again.
        self.app.refresh_foreign_writer_cache(force=True)
        self.assertFalse(self.app.task_or_404(task_id)["foreign_writer_active"])
        self.assertEqual("", self.app.db.one("SELECT foreign_writer FROM tasks WHERE id=?", (task_id,))["foreign_writer"])
        self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_proactive_scan_ignores_app_server_transports(self):
        thread_id = "foreign-writer-appserver"
        lock = self.app.thread_writer_lock_path(thread_id)
        lock.parent.mkdir(parents=True, exist_ok=True)
        holder = subprocess.Popen(
            [sys.executable, "-c", "import sys,time;f=open(sys.argv[1],'w');time.sleep(60)", str(lock), "app-server"],
        )
        try:
            for _ in range(100):
                if lock.is_file() and self.app.rollout_writer_pids(str(lock), refresh=True):
                    break
                time.sleep(0.05)
            self.assertEqual({}, self.app.foreign_writer_scan([thread_id]))
            # The failing-turn path still reports whatever really holds the lock.
            self.assertEqual([holder.pid], [writer["pid"] for writer in self.app.foreign_thread_writers(thread_id)])
        finally:
            holder.terminate()
            holder.wait()

    def test_foreign_writer_watch_loop_is_wired_into_the_lifespan(self):
        lifespan = inspect.getsource(self.app.lifespan)
        self.assertIn("foreign_writer_watch_loop()", lifespan)
        self.assertIn("foreign_writer_task.cancel()", lifespan)
        loop = inspect.getsource(self.app.foreign_writer_watch_loop)
        self.assertIn("refresh_foreign_writer_cache, True", loop)
        self.assertIn("persist_foreign_writer", loop)
        self.assertIn("clear_foreign_writer", loop)

    def test_thread_loading_reports_the_external_owner_instead_of_a_500(self):
        task_id = "foreign-writer-load"
        self.make_task(task_id, "running")
        task = self.app.task_or_404(task_id)
        error = RuntimeError(json.dumps({"code": -32600, "message": "thread already has an active writer"}))

        class BlockedClient:
            async def request(self, _method, _params):
                raise error

        with mock.patch.object(self.app, "foreign_thread_writers", return_value=[{"pid": os.getpid(), "command": "codex --yolo"}]):
            with self.assertRaises(self.app.HTTPException) as raised:
                asyncio.run(self.app.ensure_thread_loaded(task, BlockedClient()))
        self.assertEqual(409, raised.exception.status_code)
        self.assertIn("owned by another Codex process", raised.exception.detail)
        self.assertTrue(self.app.task_or_404(task_id)["foreign_writer_active"])

        # A resume error without an external owner keeps its original type.
        with mock.patch.object(self.app, "foreign_thread_writers", return_value=[]):
            with self.assertRaises(RuntimeError):
                asyncio.run(self.app.ensure_thread_loaded(task, BlockedClient()))
        self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_takeover_endpoint_ends_the_external_writer_and_frees_the_thread(self):
        task_id = "foreign-writer-takeover"
        thread_id = "foreign-writer-takeover-thread"
        self.make_task(task_id, "failed")
        self.app.db.execute(
            "UPDATE tasks SET codex_session_id=?,last_error=? WHERE id=?",
            (thread_id, json.dumps({"code": -32600, "message": "already has an active writer"}), task_id),
        )
        holder = self.start_lock_holder(thread_id)
        try:
            result = asyncio.run(self.app.take_over_thread(task_id, _=None))
            self.assertEqual([holder.pid], result["stopped"])
            self.assertEqual([], result["remaining"])
            holder.wait(timeout=10)
            row = self.app.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
            self.assertEqual("available", row["status"])
            self.assertEqual("", row["last_error"])
            self.assertEqual("", row["foreign_writer"])
            self.assertFalse(self.app.task_or_404(task_id)["foreign_writer_active"])
        finally:
            if holder.poll() is None:
                holder.kill()
                holder.wait()
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def test_thread_owner_indicator_is_wired_through_the_browser(self):
        root = Path(__file__).resolve().parents[1]
        static = root / "static"
        html = (static / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="thread-owner-alert"', html)
        self.assertLess(html.index('id="thread-owner-alert"'), html.index('id="approval-center"'))
        core_js = (static / "core.js").read_text(encoding="utf-8")
        for key in ("foreignWriterTitle", "foreignWriterBadge", "foreignWriterBody", "foreignWriterTakeover",
                    "foreignWriterTakeoverConfirm", "foreignWriterTakeoverDone", "foreignWriterTakeoverFailed",
                    "foreignWriterShort", "foreignWriterSessionTitle"):
            self.assertIn(f"{key}: {{", core_js)
        conversation_js = (static / "conversation.js").read_text(encoding="utf-8")
        render = conversation_js[conversation_js.index("function renderThreadOwnerAlert") : conversation_js.index("function renderQueuedMessages")]
        self.assertIn("foreign_writer_active", render)
        self.assertIn("foreign_writer_pids", render)
        self.assertIn("thread-owner-takeover", render)
        self.assertIn("renderThreadOwnerAlert();", conversation_js)
        self.assertIn("session-owner-badge", conversation_js)
        app_js = (static / "app.js").read_text(encoding="utf-8")
        self.assertIn('button.id === "thread-owner-takeover"', app_js)
        self.assertIn("/takeover`, { method: \"POST\" }", app_js)
        styles = (static / "styles.css").read_text(encoding="utf-8")
        self.assertIn(".thread-owner-card", styles)
        self.assertIn(".session-owner-badge", styles)
        database = (root / "codex_partner" / "database.py").read_text(encoding="utf-8")
        self.assertIn('"foreign_writer": "TEXT DEFAULT \'\'",', database)
        snapshot = inspect.getsource(self.app.attach_external_task_state)
        self.assertIn("attach_foreign_writer_state(task)", snapshot)
        endpoint = inspect.getsource(self.app.take_over_thread)
        self.assertIn("signal.SIGTERM", endpoint)
        self.assertIn("foreign_thread_writers", endpoint)
        mutable = inspect.getsource(self.app.broadcast_task)
        self.assertIn('"foreign_writer_active"', mutable)

    def test_browser_auth_login_dialog_hides_and_auto_detects_ssh_port(self):
        root = Path(__file__).resolve().parents[1]
        static = root / "static"
        html = (static / "index.html").read_text(encoding="utf-8")
        core_js = (static / "core.js").read_text(encoding="utf-8")
        self.assertNotIn('id="ssh-login-port"', html)
        self.assertNotIn('name="port"', html)
        login_block = core_js[core_js.index("async function requestSSHLogin") : core_js.index("async function api")]
        self.assertNotIn("/api/auth/status", login_block)
        self.assertNotIn("port:", login_block)
        self.assertNotIn('codexPartner.sshLoginPort', core_js)
        schema = (root / "codex_partner" / "schemas.py").read_text(encoding="utf-8")
        login_schema = schema[schema.index("class SSHLoginIn") : schema.index("class SkillIn")]
        self.assertIn("port: Optional[int] = Field(default=None, ge=1, le=65535)", login_schema)
        source = inspect.getsource(self.app.ssh_login)
        self.assertIn("login_port = payload.port or AUTH_SSH_PORT", source)
        self.assertIn("port=login_port", source)

    def test_ssh_login_verifies_against_the_requested_port(self):
        def run(username, port):
            calls = []

            def fake_verify(user, password, **kwargs):
                calls.append((user, password, kwargs))
                return False, "SSH authentication failed"

            async def attempt():
                payload = self.app.SSHLoginIn(username=username, password="secret", port=port)
                request = SimpleNamespace(
                    client=SimpleNamespace(host="203.0.113.9"),
                    headers={},
                    cookies={},
                    url=SimpleNamespace(scheme="http"),
                )
                with mock.patch.object(self.app, "verify_ssh_password", fake_verify):
                    with self.assertRaises(self.app.HTTPException):
                        await self.app.ssh_login(payload, request)

            self.app.login_throttle.clear("203.0.113.9")
            with mock.patch.object(self.app, "AUTH_MODE", "ssh"):
                asyncio.run(attempt())
            return calls

        requested = run("root", 62288)
        self.assertEqual("root", requested[0][0])
        self.assertEqual(62288, requested[0][2]["port"])
        self.assertEqual(self.app.AUTH_SSH_HOST, requested[0][2]["host"])

        defaulted = run("root", None)
        self.assertEqual(self.app.AUTH_SSH_PORT, defaulted[0][2]["port"])

    def test_direct_local_access_is_passwordless_but_proxied_or_remote_access_is_not(self):
        local_request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={}, cookies={})
        local_session = self.app.request_auth_session(local_request)
        self.assertTrue(local_session["local"])
        self.assertEqual(self.app.getpass.getuser(), local_session["username"])

        local_lan = next(
            (address for address in self.app.LOCAL_SERVER_ADDRESSES if not self.app.ipaddress.ip_address(address).is_loopback),
            None,
        )
        if local_lan:
            lan_request = SimpleNamespace(client=SimpleNamespace(host=local_lan), headers={}, cookies={})
            self.assertTrue(self.app.request_auth_session(lan_request)["local"])

        proxy_request = SimpleNamespace(
            client=SimpleNamespace(host="127.0.0.1"),
            headers={"x-forwarded-for": "192.0.2.10"},
            cookies={},
        )
        remote_request = SimpleNamespace(client=SimpleNamespace(host="192.0.2.10"), headers={}, cookies={})
        self.assertIsNone(self.app.request_auth_session(proxy_request))
        self.assertIsNone(self.app.request_auth_session(remote_request))

        local_socket = SimpleNamespace(client=SimpleNamespace(host="::1"), headers={}, cookies={})
        self.assertTrue(self.app.websocket_auth_session(local_socket)["local"])

    def test_ssh_login_username_and_throttle_are_bounded(self):
        from codex_partner.ssh_auth import LoginThrottle, valid_ssh_username

        self.assertTrue(valid_ssh_username("qingzhiguo"))
        self.assertFalse(valid_ssh_username("user@host"))
        throttle = LoginThrottle(attempts=2, window_seconds=60)
        self.assertTrue(throttle.allowed("client"))
        throttle.fail("client")
        throttle.fail("client")
        self.assertFalse(throttle.allowed("client"))
        throttle.clear("client")
        self.assertTrue(throttle.allowed("client"))

    def test_language_picker_title_collapse_and_mascot_are_wired(self):
        static = Path(__file__).resolve().parents[1] / "static"
        html = (static / "index.html").read_text(encoding="utf-8")
        core_js = (static / "core.js").read_text(encoding="utf-8")
        conversation_js = (static / "conversation.js").read_text(encoding="utf-8")
        dance_js = (static / "mascot-dance.js").read_text(encoding="utf-8")
        dance_css = (static / "mascot-dance.css").read_text(encoding="utf-8")
        self.assertIn('id="language-select"', html)
        for language in ("zh", "en", "fr", "ja", "ko"):
            self.assertIn(f'value="{language}"', html)
        self.assertIn("function applyLanguage", core_js)
        self.assertIn("conversation-title-toggle", html)
        self.assertIn("state.titleExpanded", conversation_js)
        self.assertIn("mascotMarkup", conversation_js)
        self.assertIn("humanMarkup", conversation_js)
        self.assertIn('id="theme-toggle"', html)
        self.assertIn("function toggleTheme", core_js)
        self.assertIn('localStorage.setItem("codex-dashboard-theme", next)', core_js)
        self.assertIn('$("#theme-toggle").onclick = toggleTheme', (static / "app.js").read_text(encoding="utf-8"))
        self.assertIn('[data-theme="wasteland"]', (static / "styles.css").read_text(encoding="utf-8"))
        self.assertIn("wastelandStandbyTip", core_js)
        self.assertIn("wasteland-scout-float", (static / "styles.css").read_text(encoding="utf-8"))
        self.assertIn("wasteland-steam", (static / "styles.css").read_text(encoding="utf-8"))
        self.assertIn("mascot-steam mascot-steam-a", (static / "core.js").read_text(encoding="utf-8"))
        self.assertIn("decorateMascots();", (static / "app.js").read_text(encoding="utf-8"))
        self.assertIn('id="crt-background"', html)
        self.assertIn("crt-background.js?v=20260901-crt-shader", html)
        self.assertIn("Same vignette shape", (static / "crt-background.js").read_text(encoding="utf-8"))
        self.assertIn('powerPreference: "low-power"', (static / "crt-background.js").read_text(encoding="utf-8"))
        self.assertIn("float glass =", (static / "crt-background.js").read_text(encoding="utf-8"))
        self.assertIn("opacity: .92", (static / "styles.css").read_text(encoding="utf-8"))
        self.assertIn(".chat-stream { background-color: #07150c82", (static / "styles.css").read_text(encoding="utf-8"))
        self.assertIn(".conversation-shell { background: #07150ca8", (static / "styles.css").read_text(encoding="utf-8"))
        self.assertIn("Softer phosphor text", (static / "styles.css").read_text(encoding="utf-8"))
        self.assertIn(".message-content p { color: #9fd28e", (static / "styles.css").read_text(encoding="utf-8"))
        self.assertIn("Hard readability cap", (static / "styles.css").read_text(encoding="utf-8"))
        self.assertIn("Contrast correction", (static / "styles.css").read_text(encoding="utf-8"))
        self.assertIn(".message-content, [data-theme=\"wasteland\"] .message-content p { color: #a1d0a0", (static / "styles.css").read_text(encoding="utf-8"))
        self.assertIn(".message-content, [data-theme=\"wasteland\"] .message-content p { color: #91ba7a", (static / "styles.css").read_text(encoding="utf-8"))
        self.assertIn("mascotDanceSequence", (static / "app.js").read_text(encoding="utf-8"))
        self.assertIn("function triggerMascotDance", dance_js)
        self.assertIn("partner-game-entry", dance_css)
        self.assertIn("scout-game-entry", dance_css)
        self.assertIn('class="sprite-arm', dance_js)
        self.assertIn("10400", dance_js)
        self.assertIn("white-space: nowrap", (static / "styles.css").read_text(encoding="utf-8"))
        self.assertIn('class="session-card-time"', conversation_js)
        self.assertNotIn('class="session-card-icon"', conversation_js)
        self.assertNotIn('class="session-card-live"', conversation_js)
        self.assertIn("statusLabel(task.status)", conversation_js)
        styles = (static / "styles.css").read_text(encoding="utf-8")
        self.assertIn('.session-card.running::after', styles)
        self.assertIn('[data-theme="wasteland"] .session-card.running::after', styles)

    def test_workspace_inspector_close_can_collapse_desktop_column(self):
        static = Path(__file__).resolve().parents[1] / "static"
        app_js = (static / "app.js").read_text(encoding="utf-8")
        conversation_js = (static / "conversation.js").read_text(encoding="utf-8")
        styles = (static / "styles.css").read_text(encoding="utf-8")
        self.assertIn("state.inspectorClosed = true", app_js)
        self.assertIn("setInspectorOpen(false)", app_js)
        self.assertIn("setInspectorOpen(window.innerWidth >= 861", conversation_js)
        self.assertIn("grid-template-columns: 272px minmax(0, 1fr) auto", styles)
        self.assertIn(".inspector.closed", styles)

    def test_session_sidebar_can_toggle_and_persist_on_desktop(self):
        static = Path(__file__).resolve().parents[1] / "static"
        html = (static / "index.html").read_text(encoding="utf-8")
        core_js = (static / "core.js").read_text(encoding="utf-8")
        app_js = (static / "app.js").read_text(encoding="utf-8")
        styles = (static / "styles.css").read_text(encoding="utf-8")
        self.assertIn('initial-scale=0.8', html)
        self.assertIn('id="sidebar-toggle"', html)
        self.assertIn('id="sidebar-close"', html)
        self.assertIn("function setSessionSidebarOpen", core_js)
        self.assertIn('codex-partner-sidebar-collapsed', core_js)
        self.assertIn('button.id === "sidebar-close"', app_js)
        self.assertIn(".workspace-app.sidebar-collapsed", styles)
        self.assertIn("max-width: min(84%, calc(100% - 42px))", styles)

    def test_send_queues_behind_active_turn(self):
        static = Path(__file__).resolve().parents[1] / "static"
        app_js = (static / "app.js").read_text(encoding="utf-8")
        conversation_js = (static / "conversation.js").read_text(encoding="utf-8")
        core_js = (static / "core.js").read_text(encoding="utf-8")
        self.assertIn("button.dataset.queueDispatch", app_js)
        self.assertIn('button.dataset.dispatching === "1"', app_js)
        self.assertIn("result.dispatch_error", app_js)
        self.assertIn('const delivery = activeTurn && !takeoverTerminal ? "queue" : "auto"', app_js)
        self.assertIn('const steerCurrentTurn = mode === "steer"', app_js)
        self.assertIn('state.selectedTask?.execution_source === "dashboard"', app_js)
        self.assertIn('steerCurrentTurn ? `/tasks/${taskId}/steer` : `/tasks/${taskId}/messages`', app_js)
        self.assertIn('await submitMessage("steer")', app_js)
        self.assertNotIn('await submitMessage("queue")', app_js)
        self.assertIn('Alt+Enter 插入当前 turn', conversation_js)
        self.assertIn('toast(uiLabel("queuedAfterTurn"))', app_js)
        self.assertIn('uiLabel("terminalTakeoverConfirm")', app_js)
        self.assertIn('takeover_terminal: true', app_js)
        self.assertIn('error.code = detail.code', core_js)
        self.assertIn("data-queue-dispatch", conversation_js)
        self.assertIn("queued-error", conversation_js)
        self.assertIn("function taskIsRunningGroup", core_js)
        self.assertIn("function taskHasLiveExecution", core_js)
        self.assertIn("Boolean(task?.external_running)", core_js)
        self.assertIn("const active = taskIsRunningGroup(task)", conversation_js)
        self.assertIn('["running", "retrying", "queued"].includes', core_js)
        self.assertIn("taskSortName(a).localeCompare", core_js)
        self.assertIn("taskSortTime(b).localeCompare(taskSortTime(a))", core_js)

    def test_topbar_documents_non_conflicting_session_shortcuts(self):
        static = Path(__file__).resolve().parents[1] / "static"
        html = (static / "index.html").read_text(encoding="utf-8")
        app_js = (static / "app.js").read_text(encoding="utf-8")
        conversation_js = (static / "conversation.js").read_text(encoding="utf-8")
        worker_js = (static / "chat-worker.js").read_text(encoding="utf-8")
        styles = (static / "styles.css").read_text(encoding="utf-8")
        self.assertIn('class="key-backquote"', html)
        self.assertIn('<span aria-hidden="true">~</span><span aria-hidden="true">`</span>', html)
        self.assertIn('class="shortcut-label">终端', html)
        self.assertIn('class="combo-key" aria-label="Shift N"', html)
        self.assertIn('class="combo-key" aria-label="Shift P"', html)
        self.assertIn('class="combo-key" aria-label="Shift C"', html)
        self.assertIn('class="combo-key" aria-label="Control K"', html)
        self.assertIn('class="shortcut-label">输入框', html)
        self.assertIn('document.activeElement === input', app_js)
        self.assertIn('input.blur()', app_js)
        self.assertIn("toggleTerminalShortcut", app_js)
        self.assertIn('event.code !== "Backquote"', app_js)
        self.assertIn("event.stopImmediatePropagation()", app_js)
        self.assertIn("}, true);", app_js)
        self.assertIn('event.code === "KeyN"', app_js)
        self.assertIn('event.code === "KeyP"', app_js)
        self.assertIn('event.code === "KeyC"', app_js)
        self.assertIn("function scrollSessionIntoView(id)", conversation_js)
        self.assertIn("chatSnapToBottom", conversation_js)
        self.assertIn("captureChatViewport", conversation_js)
        self.assertIn("restoreChatViewport", conversation_js)
        self.assertIn("chatIsNearBottom(stream)", conversation_js)
        self.assertIn("data-chat-block-index", conversation_js)
        self.assertIn('/conversation.js?v=20260831-focus-wide', html)
        self.assertIn("state.selectedEvents = []; state.selectedMessages = []", conversation_js)
        self.assertIn("state.runtimeMetrics = { taskId: \"\", ttftMs: null", conversation_js)
        self.assertIn('/app.js?v=20260831-focus-wide', html)
        self.assertNotIn('$("#composer-goal-meta").textContent', conversation_js)
        self.assertIn('/styles.css?v=20260831-focus-wide', html)
        self.assertIn('/vendor/katex/katex.min.css', html)
        self.assertIn('<span id="goal-run-label">暂停</span>', html)
        self.assertNotIn('id="goal-run-label" class="sr-only"', html)
        self.assertIn(".session-card.selected::before", styles)
        self.assertIn(".exploration-node.dimmed { opacity: 1; filter: saturate(1.12) brightness(1.08)", styles)
        self.assertIn(".exploration-node.dimmed:hover { opacity: 1", styles)
        self.assertIn(".exploration-edge.muted { opacity: .72", styles)
        self.assertNotIn(".exploration-node.planned { opacity:", styles)
        self.assertIn(".exploration-node.selected .exploration-node-copy", styles)
        self.assertNotIn("renderSessionList(); renderConversation(); await loadWorkspace(\"\")", conversation_js)
        self.assertIn(".queued-messages { width: auto; height: auto; min-height: 0; max-height: none; align-self: stretch;", styles)
        self.assertIn('/chat-worker.js?v=20260831-focus-wide', conversation_js)
        self.assertIn('data-live="true" open', conversation_js)
        self.assertIn("activity-event.current::after", styles)
        self.assertIn("function renderActivityEvent", conversation_js)
        self.assertIn('class="activity-inline-subject"', conversation_js)
        self.assertIn('class="activity-output activity-output-long"', conversation_js)
        self.assertIn('… +${hidden} lines', conversation_js)
        self.assertIn("if (!activityEventIsVisible(item, active)) return", conversation_js)
        self.assertIn('isWait ? uiLabel("activityBackgroundTask")', conversation_js)
        self.assertIn('uiLabel("activityCompleted")', conversation_js)
        self.assertIn('const liveItemCandidate = liveActivity ?', conversation_js)
        self.assertIn('output && !isWait && !plan.length', conversation_js)
        self.assertIn('class="activity-plan"', conversation_js)
        self.assertIn('const protocolNoise = ["updated", "update", "diff", "output", "delta"', worker_js)
        self.assertIn('if (item.hidden) continue', worker_js)

    def test_chat_selection_is_stable_and_each_block_is_copyable(self):
        static = Path(__file__).resolve().parents[1] / "static"
        core_js = (static / "core.js").read_text(encoding="utf-8")
        conversation_js = (static / "conversation.js").read_text(encoding="utf-8")
        worker_js = (static / "chat-worker.js").read_text(encoding="utf-8")
        styles = (static / "styles.css").read_text(encoding="utf-8")
        self.assertIn("if (state.chatSelectionActive) { state.chatRenderDeferred = true; return; }", core_js)
        self.assertIn("function installChatSelectionGuard", conversation_js)
        self.assertIn("state.deferredChatBlocks = blocks", conversation_js)
        self.assertIn("requestAnimationFrame(scheduleRenderChat)", conversation_js)
        self.assertIn("function copyChatBlock", conversation_js)
        self.assertIn('data-copy-chat-block="${blockIndex}"', conversation_js)
        self.assertIn("data-copy-activity-item", conversation_js)
        self.assertIn("state.chatPointerDragged && event.target.closest", conversation_js)
        self.assertIn("Math.hypot(event.clientX - state.chatPointerStartX", conversation_js)
        self.assertNotIn("chatHasTextSelection() && event.target.closest", conversation_js)
        self.assertIn("user-select: text", styles)
        self.assertIn(".chat-copy-button", styles)

    def test_session_switch_cancels_copy_selection_before_rendering(self):
        static = Path(__file__).resolve().parents[1] / "static"
        conversation = (static / "conversation.js").read_text(encoding="utf-8")
        html = (static / "index.html").read_text(encoding="utf-8")
        function_start = conversation.index("function clearChatSelectionForSessionSwitch")
        function_end = conversation.index("\n}\n", function_start)
        reset = conversation[function_start:function_end]
        self.assertIn("removeAllRanges()", reset)
        self.assertIn("state.chatSelectionActive = false", reset)
        self.assertIn("state.chatRenderDeferred = false", reset)
        self.assertIn("state.deferredChatBlocks = null", reset)
        select_start = conversation.index("async function selectSession")
        request_start = conversation.index("const requestId", select_start)
        clear_start = conversation.index("clearChatSelectionForSessionSwitch()", select_start)
        self.assertLess(clear_start, request_start)
        self.assertIn("if (state.selectedId !== id)", conversation[select_start:request_start])
        self.assertIn('sessionList?.addEventListener("pointerdown"', conversation)
        self.assertIn('const sessionList = $("#task-list")', conversation)
        self.assertIn("void selectSession(pointerSelectedSessionId)", conversation)
        self.assertIn('aria-current="${selected ? "true" : "false"}"', conversation)
        self.assertIn('/conversation.js?v=20260831-focus-wide', html)

    def test_live_chat_rendering_coalesces_expensive_work(self):
        static = Path(__file__).resolve().parents[1] / "static"
        core = (static / "core.js").read_text(encoding="utf-8")
        conversation = (static / "conversation.js").read_text(encoding="utf-8")
        html = (static / "index.html").read_text(encoding="utf-8")
        self.assertIn("100 - (Date.now() - chatLastRenderAt)", core)
        self.assertIn("function scheduleRuntimeMetricsRefresh", core)
        self.assertIn("}, 250)", core)
        self.assertIn("if (chatBuildInFlight)", conversation)
        self.assertIn("chatBuildQueued = true", conversation)
        self.assertIn("const stale = state.selectedId !== taskId", conversation)
        self.assertIn("function isHiddenProtocolNoise", conversation)
        self.assertIn("if (!protocolNoise || tokenUsage) {", conversation)
        self.assertIn("state.explorationNeedsSync && (state.explorationOpen || !state.explorationNodes.length)", conversation)
        self.assertIn("if (current !== previous) scheduleRenderChat()", conversation)
        self.assertIn("renderConversation(false)", conversation)
        self.assertIn("const conversationSnapshots = new Map()", conversation)
        self.assertIn("conversationSnapshots.set(previousId", conversation)
        self.assertIn("const cached = conversationSnapshots.get(id)", conversation)
        self.assertIn("timeline = await timelinePromise", conversation)
        self.assertIn("const activityMapUpdate = activityMapPromise.then", conversation)
        select_start = conversation.index("async function selectSession")
        socket_start = conversation.index("if (openSocket) connectSocket(id)", select_start)
        timeline_wait = conversation.index("timeline = await timelinePromise", select_start)
        self.assertLess(socket_start, timeline_wait)
        self.assertIn('/core.js?v=20260831-focus-wide', html)
        self.assertIn('/conversation.js?v=20260831-focus-wide', html)
        styles = (static / "styles.css").read_text(encoding="utf-8")
        app_js = (static / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("content-visibility: auto", styles)
        self.assertIn("transform: translateX(470%)", styles)
        self.assertIn("min-width: min(180px, calc(100% - 49px))", styles)
        self.assertIn(".message.user .message-content { display: block; box-sizing: border-box; width: 100%", styles)
        self.assertIn(".message.assistant .message-body { flex: 1 1 auto; width: min(780px, calc(100% - 49px));", styles)
        self.assertIn("function chatStreamUsableWidth", conversation)
        self.assertIn("usableWidth > 0 && usableWidth < 240 && chatLayoutRetryCount < 5", conversation)
        self.assertIn("parseFloat(style.paddingLeft", conversation)
        self.assertIn('const painted = isDelta ? appendStreamingDelta(data.payload) : false;', conversation)
        self.assertIn('chatWorker.postMessage({ type: "previewMarkdown"', conversation)
        self.assertIn('if (!isDelta || !painted) scheduleRenderChat();', conversation)
        self.assertIn("}, 40);", conversation)
        self.assertIn(".message.assistant .message-content, .message.assistant .message-content > p", styles)
        self.assertIn("if (chatIsNearBottom(stream))", conversation)
        self.assertNotIn("stream.scrollTop = target * state.chatAverageHeight", conversation)
        self.assertIn("function syncPageVisibility", app_js)
        self.assertIn('/styles.css?v=20260831-focus-wide', html)
        self.assertIn('/app.js?v=20260831-focus-wide', html)

    def test_optimistic_queue_messages_survive_authoritative_refresh(self):
        static = Path(__file__).resolve().parents[1] / "static"
        core_js = (static / "core.js").read_text(encoding="utf-8")
        app_js = (static / "app.js").read_text(encoding="utf-8")
        self.assertIn("_optimistic: true", app_js)
        self.assertIn("message._optimistic === true", core_js)
        self.assertIn("const pending = state.selectedMessages.filter", core_js)
        self.assertIn("state.selectedMessages = [...authoritative, ...pending]", core_js)

    def test_task_rekey_event_replaces_provisional_sidebar_card(self):
        task_id = f"rekey-target-{time.time_ns()}"
        self.make_task(task_id, "available")

        class Socket:
            def __init__(self):
                self.payloads = []

            async def send_json(self, payload):
                self.payloads.append(payload)

        socket = Socket()
        self.app.overview_clients.add(socket)
        try:
            asyncio.run(self.app.broadcast_overview_rekeyed("provisional-id", task_id))
        finally:
            self.app.overview_clients.discard(socket)
        self.assertEqual("task_rekeyed", socket.payloads[0]["type"])
        self.assertEqual("provisional-id", socket.payloads[0]["old_task_id"])
        self.assertEqual(task_id, socket.payloads[0]["new_task_id"])

        static = Path(__file__).resolve().parents[1] / "static"
        conversation_js = (static / "conversation.js").read_text(encoding="utf-8")
        worker_js = (static / "chat-worker.js").read_text(encoding="utf-8")
        styles = (static / "styles.css").read_text(encoding="utf-8")
        self.assertIn('data.type === "task_rekeyed"', conversation_js)
        self.assertIn("task.id !== oldTaskId && task.id !== newTaskId", conversation_js)
        self.assertIn("connectSocket(newTaskId, true)", conversation_js)
        self.assertIn("native.aggregatedOutput", worker_js)
        self.assertIn('uiLabel("loadEarlierActivity"', conversation_js)
        self.assertIn("inactiveTrashReason", (static / "settings.js").read_text(encoding="utf-8"))
        self.assertIn("scroll-behavior: auto", styles)
        self.assertIn("grid-template-columns: auto minmax(0, 1fr) auto", styles)
        self.assertIn(".shortcut-hints { position: absolute; left: 50%", styles)

    def test_completed_queue_status_cannot_regress_to_queued_in_frontend(self):
        core_js = (Path(__file__).resolve().parents[1] / "static/core.js").read_text(encoding="utf-8")
        conversation_js = (Path(__file__).resolve().parents[1] / "static/conversation.js").read_text(encoding="utf-8")
        self.assertIn("incomingRank < currentRank", core_js)
        self.assertIn("function replaceTaskMessages", core_js)
        self.assertIn("replaceTaskMessages(messages)", conversation_js)
        self.assertIn("replaceTaskMessages(data.messages)", conversation_js)
        self.assertIn("function syncQueuedMessages", conversation_js)
        self.assertIn("queueSyncTimer = setInterval(syncQueuedMessages, 1500)", conversation_js)
        worker_js = (Path(__file__).resolve().parents[1] / "static/chat-worker.js").read_text(encoding="utf-8")
        self.assertIn('["running", "steering"].includes(message.status)', worker_js)
        self.assertIn('!["steered", "sent"].includes(message.status)', worker_js)

    def test_hiding_terminal_preserves_pty_and_xterm_history(self):
        static = Path(__file__).resolve().parents[1] / "static"
        html = (static / "index.html").read_text(encoding="utf-8")
        app_js = (static / "app.js").read_text(encoding="utf-8")
        conversation_js = (static / "conversation.js").read_text(encoding="utf-8")
        self.assertIn('id="terminal-close"', html)
        self.assertIn("function closeTerminal() { hideTerminal(); }", conversation_js)
        self.assertIn("function destroyTerminal()", conversation_js)
        self.assertIn("const preserveHistory = terminalTaskId === state.selectedId && terminalShouldReconnect", conversation_js)
        self.assertIn("destroyTerminal()", app_js)

    def test_frontend_modules_load_before_event_bootstrap(self):
        html = (Path(__file__).resolve().parents[1] / "static/index.html").read_text(encoding="utf-8")
        positions = [html.index(f'src="/{name}?') for name in ("core.js", "conversation.js", "settings.js", "app.js")]
        self.assertEqual(sorted(positions), positions)

    def test_unified_editor_is_loaded_and_used_for_all_text_editing(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "static/index.html").read_text(encoding="utf-8")
        editor = (root / "static/editor.js").read_text(encoding="utf-8")
        settings = (root / "static/settings.js").read_text(encoding="utf-8")
        conversation = (root / "static/conversation.js").read_text(encoding="utf-8")
        self.assertLess(html.index('src="/vendor/ace/ace.js"'), html.index('src="/editor.js?'))
        self.assertLess(html.index('src="/editor.js?'), html.index('src="/conversation.js?'))
        self.assertIn("function detectEditorMode", editor)
        self.assertIn("function textEditorMarkup", editor)
        self.assertIn('data-editor-view="split"', editor)
        self.assertIn('name: "saveDocument"', editor)
        self.assertGreaterEqual(settings.count("mountTextEditor("), 2)
        self.assertIn('mountTextEditor("#workspace-file-editor"', conversation)
        self.assertNotIn('textarea name="content"', settings)
        self.assertNotIn('id="workspace-file-content"', conversation)
        self.assertIn('JSON.stringify({ workspace: "", ssh_host: "" })', settings)

    def test_docker_image_copies_runtime_package(self):
        dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY codex_partner ./codex_partner", dockerfile)
        self.assertIn('npm install --global --prefix /home/codex/.local "@openai/codex@${CODEX_VERSION}"', dockerfile)
        self.assertIn("CODEX_BIN=/home/codex/.local/bin/codex", dockerfile)
        self.assertIn("USER codex", dockerfile)
        self.assertIn("bubblewrap", dockerfile)
        self.assertIn('ENTRYPOINT ["tini", "--"]', dockerfile)
        self.assertIn('CMD ["codex-partner"]', dockerfile)
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
        self.assertIn("--cap-add=SYS_ADMIN", readme)
        self.assertIn("--security-opt seccomp=unconfined", readme)
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
        self.assertIn("--cap-add=SYS_ADMIN", readme)
        self.assertIn("--security-opt seccomp=unconfined", readme)

    def test_codex_discovery_uses_path_then_vscode_then_official(self):
        cases = (
            ("/direct/codex", ["/vscode/codex"], ["/official/codex"], {"/direct/codex"}, "path", "/direct/codex"),
            (None, ["/vscode/codex"], ["/official/codex"], {"/vscode/codex"}, "vscode", "/vscode/codex"),
            (None, [], ["/official/codex"], {"/official/codex"}, "official", "/official/codex"),
            (None, [], [], set(), "missing", ""),
        )
        for direct, vscode, official, usable, source, expected in cases:
            if direct:
                resolved_direct = str(Path(direct).expanduser().resolve())
                usable = {resolved_direct if value == direct else value for value in usable}
                expected = resolved_direct if expected == direct else expected
            with self.subTest(source=source), \
                 mock.patch.dict(os.environ, {"CODEX_BIN": "codex"}), \
                 mock.patch.object(self.app.shutil, "which", return_value=direct), \
                 mock.patch.object(self.app, "vscode_codex_candidates", return_value=vscode), \
                 mock.patch.object(self.app, "official_codex_candidates", return_value=official), \
                 mock.patch.object(self.app, "codex_candidate_usable", side_effect=lambda path: path in usable):
                result = self.app.discover_codex()
                self.assertEqual(source, result["source"])
                self.assertEqual(expected, result["path"])
                self.assertEqual(source != "missing", result["available"])

    def test_install_plan_is_latest_user_scoped_and_shell_free(self):
        with mock.patch.object(self.app.platform, "system", return_value="Linux"), \
             mock.patch.object(self.app, "find_npm_executable", return_value="/usr/bin/npm"):
            plan = self.app.codex_install_plan()
        self.assertTrue(plan["supported"])
        self.assertEqual("@openai/codex@latest", plan["package"])
        self.assertEqual(str(Path.home() / ".local"), plan["prefix"])
        self.assertIn("@openai/codex@latest", plan["command"])
        self.assertNotIn("sudo", plan["command"])

    def test_missing_codex_returns_install_prompt(self):
        with mock.patch.object(self.app, "CODEX_AVAILABLE", False):
            with self.assertRaises(self.app.HTTPException) as raised:
                self.app.require_codex()
        self.assertEqual(503, raised.exception.status_code)
        self.assertIn("一键安装", raised.exception.detail)

    def test_install_endpoint_runs_fixed_latest_command_only_when_missing(self):
        prefix = Path(self.temp.name) / "codex-install"
        plan = {
            "supported": True,
            "os": "Linux",
            "package": "@openai/codex@latest",
            "prefix": str(prefix),
            "command": "npm install",
            "reason": "",
        }

        class FakeProcess:
            returncode = 0

            async def communicate(self):
                return b"installed", b""

        create = mock.AsyncMock(return_value=FakeProcess())
        sync = mock.AsyncMock(return_value={"available": True})
        with mock.patch.object(self.app, "CODEX_AVAILABLE", False), \
             mock.patch.object(self.app, "codex_install_plan", return_value=plan), \
             mock.patch.object(self.app, "find_npm_executable", return_value="/usr/bin/npm"), \
             mock.patch.object(self.app.asyncio, "create_subprocess_exec", create), \
             mock.patch.object(self.app, "refresh_codex_discovery", return_value={"available": True, "source": "official"}), \
             mock.patch.object(self.app, "sync_native_threads", sync):
            result = asyncio.run(self.app.install_codex())
        self.assertTrue(result["ok"])
        self.assertFalse(result["already_installed"])
        command = create.await_args.args
        self.assertEqual(
            ("/usr/bin/npm", "install", "--global", "--prefix", str(prefix), "@openai/codex@latest"),
            command,
        )

        create.reset_mock()
        with mock.patch.object(self.app, "CODEX_AVAILABLE", True), \
             mock.patch.object(self.app.asyncio, "create_subprocess_exec", create):
            result = asyncio.run(self.app.install_codex())
        self.assertTrue(result["already_installed"])
        create.assert_not_awaited()


    def test_legacy_manual_message_does_not_append_persistent_goal(self):
        task = {"goal": "old exploration objective", "prompt": "", "context": "", "workspace": "/tmp", "yolo": False, "model": "", "reasoning_effort": ""}
        with_goal = self.app.command_for(task, None, prompt_override="git clone the repository")
        without_goal = self.app.command_for(task, None, prompt_override="git clone the repository", include_goal=False)
        self.assertIn("old exploration objective", with_goal[-1])
        self.assertNotIn("old exploration objective", without_goal[-1])
        self.assertIn("git clone the repository", without_goal[-1])

    def test_composer_drafts_are_scoped_to_each_session(self):
        static = Path(__file__).resolve().parents[1] / "static"
        core = (static / "core.js").read_text(encoding="utf-8")
        app_js = (static / "app.js").read_text(encoding="utf-8")
        conversation = (static / "conversation.js").read_text(encoding="utf-8")
        self.assertIn("const composerDrafts = new Map()", core)
        self.assertIn("function saveComposerDraft", app_js)
        self.assertIn("function restoreComposerDraft", app_js)
        self.assertIn("clearComposerDraft(taskId)", app_js)
        self.assertIn("saveComposerDraft(previousId)", conversation)
        self.assertIn("restoreComposerDraft(id)", conversation)

    def test_large_text_and_markdown_viewer_exposes_complete_loading(self):
        conversation = (Path(__file__).resolve().parents[1] / "static" / "conversation.js").read_text(encoding="utf-8")
        styles = (Path(__file__).resolve().parents[1] / "static" / "styles.css").read_text(encoding="utf-8")
        app_source = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
        self.assertIn("const autoLoadLimit = 8 * 1024 * 1024", conversation)
        self.assertIn("media-viewer-progress", conversation)
        self.assertIn("media-viewer-load-all", conversation)
        self.assertIn("while (await appendNext())", conversation)
        self.assertIn("if (markdown && nextOffset == null)", conversation)
        self.assertNotIn("已显示前 1 MB", conversation)
        self.assertIn("place-items: start stretch", styles)
        self.assertIn(".media-viewer-image { place-self: center", styles)
        self.assertIn("white-space: pre; overflow-wrap: normal; word-break: normal", styles)
        self.assertIn(".media-viewer-markdown .toastui-editor-contents pre { max-width: 100%; overflow-x: auto", styles)
        self.assertIn("word-wrap: normal !important", styles)
        self.assertIn("id=\"conversation-searchbar\"", (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(encoding="utf-8"))
        self.assertIn("function refreshChatSearch", conversation)
        self.assertIn("scrollIntoView({ block: \"center\"", conversation)
        self.assertIn("conversation-search", (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8"))
        self.assertIn("def persist_native_thread_permissions", app_source)
        self.assertIn('args.append("never" if yolo else "on-request")', app_source)
        self.assertIn('persist_native_thread_permissions(result.get("codex_session_id") or task_id', app_source)
        self.assertIn("def apply_native_thread_settings", app_source)
        self.assertIn("task = apply_native_thread_settings(task, thread_id)", app_source)
        self.assertIn("setChatSearchOpen(!bar || bar.hidden)", (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8"))

if __name__ == "__main__":
    unittest.main()
