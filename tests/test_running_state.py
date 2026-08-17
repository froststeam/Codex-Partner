import asyncio
import base64
import importlib
import io
import json
import os
import re
import sqlite3
import sys
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


class RunningStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        os.environ["CODEX_DASHBOARD_DATA"] = str(root / "data")
        os.environ["CODEX_HOME"] = str(root / "codex-home")
        os.environ["CODEX_BIN"] = str(Path(__file__).parent / "fake_codex")
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

    def test_native_history_accepts_iso_timestamps(self):
        stamp = self.app.native_stamp("2026-08-16T01:27:10.093564Z", "fallback")
        self.assertEqual("2026-08-16T01:27:10.093564+00:00", stamp)

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
                self.assertEqual(str(root / thread_id), task["workspace"])
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

    def test_native_sync_preserves_dashboard_model_override(self):
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
            "INSERT INTO tasks (id,name,prompt,workspace,status,yolo,provider_id,model,codex_session_id,created_at,updated_at,native) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, task_id, "prompt", str(workspace), "available", 1, "dashboard-provider", "gpt-5.6", thread_id, stamp, stamp, 1),
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
        self.assertTrue(all(str(workspace) in item["path"] for item in inputs[1:]))

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
        path = Path(self.temp.name) / "writer.jsonl"
        path.write_text("{}\n", encoding="utf-8")
        with path.open("a", encoding="utf-8"):
            self.assertIn(os.getpid(), self.app.rollout_writer_pids(str(path), refresh=True))
        os.utime(path, (time.time() - 10, time.time() - 10))
        self.assertFalse(self.app.rollout_writer_pids(str(path), refresh=True))
        self.assertFalse(self.app.rollout_is_live(str(path)))

    def test_stale_started_turn_becomes_stopped(self):
        task_id = "stale-thread"
        self.make_task(task_id)
        path = Path(self.temp.name) / "stale.jsonl"
        record = {"timestamp": self.app.now(), "type": "event_msg", "payload": {"type": "task_started"}}
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        os.utime(path, (time.time() - 10, time.time() - 10))
        asyncio.run(self.app.reconcile_initial_rollout(task_id, task_id, str(path), record, ""))
        self.assertEqual("stopped", self.app.task_or_404(task_id)["status"])
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

    def test_goal_resume_adopts_active_terminal_turn(self):
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
            self.assertEqual("goal_resume", result["run_mode"])
            self.assertEqual("terminal", result["execution_source"])
            self.app.persist_external_task_status(task_id, dashboard_active=False)
            self.assertEqual("goal_resume", self.app.task_or_404(task_id)["run_mode"])
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
        launch.assert_awaited_once_with(task_id, "resume")
        self.assertEqual("queued", self.app.task_or_404(task_id)["status"])

    def test_stop_clears_unowned_running_state(self):
        task_id = "orphan-thread"
        self.make_task(task_id)
        self.app.db.execute("UPDATE tasks SET run_mode='goal_resume' WHERE id=?", (task_id,))
        result = asyncio.run(self.app.stop_task_run(task_id))
        self.assertEqual("stopped", result["status"])
        self.assertEqual("", result["run_mode"])

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
            result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
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
            result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
            self.assertEqual(["visible user message"], json.loads(result.stdout))
        finally:
            self.app.app_thread_bindings.pop(task_id, None)
            self.app.db.execute("DELETE FROM events WHERE session_id=?", (session_id,))
            self.app.db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            self.app.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

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

    def test_goal_retry_defaults_on_and_manual_override_is_preserved(self):
        task_id = "goal-retry-thread"
        self.make_task(task_id, "available")
        self.app.db.execute("UPDATE tasks SET codex_session_id='' WHERE id=?", (task_id,))
        result = asyncio.run(self.app.patch_goal(task_id, self.app.GoalPatch(objective="finish it")))
        self.assertTrue(result["retry_forever"])
        self.assertEqual(0, result["retry_explicit"])

        result = asyncio.run(self.app.patch_task(task_id, self.app.TaskPatch(retry_forever=False)))
        self.assertFalse(result["retry_forever"])
        self.assertEqual(1, result["retry_explicit"])

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

    def test_clearing_goal_uses_native_clear_and_disables_retry(self):
        task_id = "clear-native-goal-thread"
        self.make_task(task_id, "available")
        self.app.db.execute(
            "UPDATE tasks SET goal='old goal',goal_status='active',retry_forever=1,codex_session_id=? WHERE id=?",
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
                self.assertEqual(root, workspace)
                self.assertFalse((root / task["id"]).exists())
            self.app.db.execute("DELETE FROM tasks WHERE id IN (?,?,?)", (quick["id"], regular["id"], command["id"]))

    def test_default_workspace_aligns_to_codex_thread_id(self):
        with tempfile.TemporaryDirectory(dir=self.temp.name) as directory:
            root = Path(directory) / "codex_partner"
            with mock.patch.object(self.app, "SESSION_WORKSPACE_ROOT", root):
                task = asyncio.run(self.app.create_quick_task(self.app.QuickTaskCreate(), None))
                target = self.app.align_session_workspace(task["id"], "thread-123")
                self.assertEqual(root / "thread-123", target)
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
                self.assertEqual(target, aligned)
                self.assertEqual(str(target), self.app.task_or_404(task["id"])["workspace"])
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
                response = await self.app.download_workspace_file(task_id, f"{relative}/result.txt")
                self.assertEqual(str(target.resolve()), str(response.path))

                with self.assertRaises(self.app.HTTPException) as invalid_name:
                    await self.app.upload_workspace_file(
                        task_id, UploadRequest(b"bad"), path=relative, filename="../bad.txt"
                    )
                self.assertEqual(400, invalid_name.exception.status_code)

                secret = Path(directory) / ".env"
                secret.write_text("SECRET=value", encoding="utf-8")
                with self.assertRaises(self.app.HTTPException) as hidden:
                    await self.app.download_workspace_file(task_id, f"{relative}/.env")
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

    def test_ssh_config_discovery_follows_includes_and_ignores_patterns(self):
        with tempfile.TemporaryDirectory(dir=self.temp.name) as directory:
            root = Path(directory)
            included = root / "included.conf"
            included.write_text("Host build-box\n  HostName 10.0.0.8\nHost *.internal\n", encoding="utf-8")
            config = root / "config"
            config.write_text(f"Include {included}\nHost direct-box other-box\n", encoding="utf-8")
            self.assertEqual(["build-box", "direct-box", "other-box"], self.app.ssh_config_aliases(config))

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

    def test_remote_thread_fork_keeps_ssh_host_and_workspace(self):
        task_id = "remote-fork-source"
        self.make_task(task_id, "available")
        self.app.db.execute(
            "UPDATE tasks SET name=?,ssh_host=?,workspace=?,codex_session_id=? WHERE id=?",
            ("Remote source", "build-box", "/srv/project", "remote-thread", task_id),
        )

        class FakeClient:
            async def request(self, method, _params):
                if method == "thread/read":
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
            self.app.db.execute("DELETE FROM tasks WHERE id IN (?,?)", (task_id, forked["id"]))

        asyncio.run(exercise())

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
            for name in ("core.js", "conversation.js", "settings.js", "app.js")
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
        self.assertIn("payload.client_message_id || body || payload.item_id", worker_js)
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
        self.assertIn('const delivery = mode === "queue" || activeTurn ? "queue" : "auto"', app_js)
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
        self.assertIn('/app.js?v=20260816-structured-inputs', html)
        self.assertIn('/core.js?v=20260817-session-switch', html)
        self.assertIn('responseErrorMessage(response)', (static / "core.js").read_text(encoding="utf-8"))
        self.assertIn('/mascot-dance.js?v=20260816-game-sprites', html)
        self.assertIn("/timeline?limit=160", conversation_js)
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
        self.assertIn('await launch(task["id"], "resume", "", "", set())', (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8"))
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

    def test_goal_button_tracks_goal_resume_instead_of_any_active_turn(self):
        root = Path(__file__).resolve().parents[1]
        conversation_js = (root / "static" / "conversation.js").read_text(encoding="utf-8")
        app_js = (root / "static" / "app.js").read_text(encoding="utf-8")
        database_py = (root / "codex_partner" / "database.py").read_text(encoding="utf-8")
        expected = 'task.run_mode === "goal_resume"'
        self.assertIn(expected, conversation_js)
        self.assertIn(expected, app_js)
        self.assertIn('active: "enabled"', conversation_js)
        self.assertIn('goalProgress.textContent = goalStatusLabel', conversation_js)
        self.assertNotIn('active: "running"', conversation_js)
        self.assertIn("authoritative.run_mode !== state.selectedTask.run_mode", (root / "static" / "core.js").read_text(encoding="utf-8"))
        self.assertIn("run_mode TEXT DEFAULT ''", database_py)
        self.assertEqual("message", self.app.requested_run_mode({"goal": "ship it"}, "message", "message-1"))
        self.assertEqual("goal_resume", self.app.requested_run_mode({"goal": "ship it"}, "resume"))
        self.assertEqual("operation", self.app.requested_run_mode({"goal": ""}, "resume"))

    def test_browser_auth_uses_ssh_cookie_instead_of_access_tokens(self):
        static = Path(__file__).resolve().parents[1] / "static"
        core_js = (static / "core.js").read_text(encoding="utf-8")
        conversation_js = (static / "conversation.js").read_text(encoding="utf-8")
        html = (static / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="ssh-login-dialog"', html)
        self.assertIn("requestSSHLogin", core_js)
        self.assertIn('fetch("/api/auth/login"', core_js)
        self.assertNotIn("codex-dashboard-token", core_js + conversation_js)
        self.assertNotIn("?token=", conversation_js)

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
        self.assertIn("mascotDanceSequence", (static / "app.js").read_text(encoding="utf-8"))
        self.assertIn("function triggerMascotDance", dance_js)
        self.assertIn("partner-game-entry", dance_css)
        self.assertIn("scout-game-entry", dance_css)
        self.assertIn('class="sprite-arm', dance_js)
        self.assertIn("10400", dance_js)
        self.assertIn("white-space: nowrap", (static / "styles.css").read_text(encoding="utf-8"))
        self.assertIn('class="session-card-time"', conversation_js)
        self.assertNotIn('class="session-card-icon"', conversation_js)

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
        self.assertIn("button.dataset.queueDispatch", app_js)
        self.assertIn('const delivery = mode === "queue" || activeTurn ? "queue" : "auto"', app_js)
        self.assertIn('toast(uiLabel("queuedAfterTurn"))', app_js)
        self.assertNotIn('if (mode === "codex" && activeTurn)', app_js)
        self.assertIn("data-queue-dispatch", conversation_js)

    def test_topbar_documents_non_conflicting_session_shortcuts(self):
        static = Path(__file__).resolve().parents[1] / "static"
        html = (static / "index.html").read_text(encoding="utf-8")
        app_js = (static / "app.js").read_text(encoding="utf-8")
        conversation_js = (static / "conversation.js").read_text(encoding="utf-8")
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
        self.assertIn("scrollIntoView({ block: \"nearest\" })", conversation_js)
        self.assertIn("chatSnapToBottom", conversation_js)
        self.assertIn("captureChatViewport", conversation_js)
        self.assertIn("restoreChatViewport", conversation_js)
        self.assertIn("chatIsNearBottom(stream)", conversation_js)
        self.assertIn("data-chat-block-index", conversation_js)
        self.assertIn('/conversation.js?v=20260817-media-viewer', html)
        self.assertNotIn('$("#composer-goal-meta").textContent', conversation_js)
        self.assertIn('/styles.css?v=20260817-media-viewer', html)
        self.assertIn(".session-card.selected::before", styles)
        self.assertNotIn("renderSessionList(); renderConversation(); await loadWorkspace(\"\")", conversation_js)
        self.assertIn(".queued-messages { width: auto; height: auto; min-height: 0; max-height: none; align-self: stretch;", styles)
        self.assertIn('/chat-worker.js?v=20260817-message-dedupe', conversation_js)
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
        self.assertIn('["running", "steering", "steered", "sent"].includes(message.status)', worker_js)

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
        self.assertIn('ENTRYPOINT ["tini", "--"]', dockerfile)
        self.assertIn('CMD ["codex-partner"]', dockerfile)

    def test_codex_discovery_uses_path_then_vscode_then_official(self):
        cases = (
            ("/direct/codex", ["/vscode/codex"], ["/official/codex"], {"/direct/codex"}, "path", "/direct/codex"),
            (None, ["/vscode/codex"], ["/official/codex"], {"/vscode/codex"}, "vscode", "/vscode/codex"),
            (None, [], ["/official/codex"], {"/official/codex"}, "official", "/official/codex"),
            (None, [], [], set(), "missing", ""),
        )
        for direct, vscode, official, usable, source, expected in cases:
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


if __name__ == "__main__":
    unittest.main()
