"""SQLite persistence and forward-only schema migrations for Codex Partner."""

import sqlite3
import threading
from pathlib import Path
from typing import Callable, Optional


class Database:
    """Small thread-safe SQLite gateway used by async route handlers."""

    def __init__(self, path: Path, clock: Callable[[], str]):
        self.path = str(path)
        self.clock = clock
        self.lock = threading.RLock()
        self.initialize()
        try:
            Path(self.path).chmod(0o600)
        except OSError:
            pass

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def initialize(self) -> None:
        with self.lock, self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                  id TEXT PRIMARY KEY, name TEXT NOT NULL, prompt TEXT NOT NULL,
                  goal TEXT DEFAULT '', workspace TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'queued', yolo INTEGER NOT NULL DEFAULT 1,
                  max_retries INTEGER NOT NULL DEFAULT 3, retry_count INTEGER NOT NULL DEFAULT 0,
                  retry_forever INTEGER NOT NULL DEFAULT 0,
                  retry_explicit INTEGER NOT NULL DEFAULT 0,
                  provider_id TEXT, model TEXT DEFAULT '', context TEXT DEFAULT '',
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                  last_error TEXT DEFAULT '', active_session_id TEXT, codex_session_id TEXT DEFAULT '',
                  goal_status TEXT DEFAULT 'active', goal_tokens_used INTEGER DEFAULT 0,
                  native INTEGER NOT NULL DEFAULT 0,
                  archived INTEGER NOT NULL DEFAULT 0,
                  trashed INTEGER NOT NULL DEFAULT 0,
                  trashed_at TEXT,
                  trashed_reason TEXT DEFAULT '',
                  last_interaction_at TEXT,
                  memory_mode TEXT DEFAULT 'enabled',
                  execution_source TEXT DEFAULT '',
                  execution_turn_id TEXT DEFAULT '',
                  run_mode TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS sessions (
                  id TEXT PRIMARY KEY, task_id TEXT NOT NULL, status TEXT NOT NULL,
                  attempt INTEGER NOT NULL, provider_id TEXT, command TEXT NOT NULL,
                  started_at TEXT NOT NULL, finished_at TEXT, exit_code INTEGER,
                  summary TEXT DEFAULT '', codex_session_id TEXT DEFAULT '',
                  FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                  task_id TEXT, ts TEXT NOT NULL, stream TEXT NOT NULL, payload TEXT NOT NULL,
                  FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS skills (
                  id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, description TEXT DEFAULT '',
                  content TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS providers (
                  id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'codex',
                  model TEXT DEFAULT '', profile TEXT DEFAULT '', api_key_env TEXT DEFAULT '',
                  api_key TEXT DEFAULT '', base_url TEXT DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
                  priority INTEGER NOT NULL DEFAULT 100, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                  model_provider TEXT DEFAULT '', native INTEGER NOT NULL DEFAULT 0,
                  health_status TEXT DEFAULT 'unchecked', health_detail TEXT DEFAULT '',
                  health_checked_at TEXT, health_latency_ms INTEGER,
                  success_count INTEGER NOT NULL DEFAULT 0, failure_count INTEGER NOT NULL DEFAULT 0,
                  last_success_at TEXT, last_failure_at TEXT
                );
                CREATE TABLE IF NOT EXISTS task_messages (
                  id TEXT PRIMARY KEY, task_id TEXT NOT NULL, body TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'queued', created_at TEXT NOT NULL,
                  started_at TEXT, finished_at TEXT, session_id TEXT, error TEXT DEFAULT '',
                  FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS activity_graphs (
                  task_id TEXT PRIMARY KEY, projection_version INTEGER NOT NULL,
                  status TEXT NOT NULL DEFAULT 'pending', processed_events INTEGER NOT NULL DEFAULT 0,
                  event_count INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0,
                  started_at TEXT, updated_at TEXT NOT NULL, error TEXT DEFAULT '',
                  FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS activity_nodes (
                  task_id TEXT NOT NULL, node_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                  parent_id TEXT, kind TEXT NOT NULL, status TEXT NOT NULL, title TEXT NOT NULL,
                  summary TEXT DEFAULT '', evidence_json TEXT DEFAULT '[]', files_json TEXT DEFAULT '[]',
                  commands_json TEXT DEFAULT '[]', failures INTEGER NOT NULL DEFAULT 0,
                  score INTEGER NOT NULL DEFAULT 0, turn_id TEXT DEFAULT '', item_id TEXT DEFAULT '',
                  source_event_id TEXT DEFAULT '', event_time TEXT DEFAULT '',
                  PRIMARY KEY(task_id,node_id), FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS activity_graph_seen (
                  task_id TEXT NOT NULL, event_key TEXT NOT NULL,
                  PRIMARY KEY(task_id,event_key), FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS ssh_saved_hosts (
                  alias TEXT PRIMARY KEY, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_task_attempt ON sessions(task_id, attempt DESC);
                CREATE INDEX IF NOT EXISTS idx_sessions_task_id ON sessions(task_id, id);
                CREATE INDEX IF NOT EXISTS idx_events_session_id ON events(session_id, id);
                CREATE INDEX IF NOT EXISTS idx_events_session_ts_id ON events(session_id, ts DESC, id DESC);
                CREATE INDEX IF NOT EXISTS idx_events_stream_id_session ON events(stream, id DESC, session_id);
                CREATE INDEX IF NOT EXISTS idx_task_messages_task_status ON task_messages(task_id, status, created_at);
                CREATE INDEX IF NOT EXISTS idx_activity_nodes_task_sequence ON activity_nodes(task_id, sequence);
                """
            )
            self._migrate(connection)
            self._recover_interrupted_work(connection)
            connection.commit()

    def _migrate(self, connection: sqlite3.Connection) -> None:
        session_columns = {row[1] for row in connection.execute("PRAGMA table_info(sessions)")}
        if "codex_session_id" not in session_columns:
            connection.execute("ALTER TABLE sessions ADD COLUMN codex_session_id TEXT DEFAULT ''")

        event_columns = {row[1] for row in connection.execute("PRAGMA table_info(events)")}
        if "task_id" not in event_columns:
            connection.execute("ALTER TABLE events ADD COLUMN task_id TEXT")
        connection.execute(
            "UPDATE events SET task_id=(SELECT task_id FROM sessions WHERE sessions.id=events.session_id) "
            "WHERE task_id IS NULL"
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_events_task_ts_id ON events(task_id, ts DESC, id DESC)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_events_task_stream_id ON events(task_id, stream, id DESC)")
        connection.execute(
            "CREATE TRIGGER IF NOT EXISTS trg_events_fill_task_id AFTER INSERT ON events "
            "WHEN NEW.task_id IS NULL BEGIN "
            "UPDATE events SET task_id=(SELECT task_id FROM sessions WHERE id=NEW.session_id) WHERE id=NEW.id; END"
        )

        task_columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
        task_migrations = {
            "codex_session_id": "TEXT DEFAULT ''",
            "retry_forever": "INTEGER NOT NULL DEFAULT 0",
            "retry_explicit": "INTEGER NOT NULL DEFAULT 0",
            "goal_status": "TEXT DEFAULT 'active'",
            "goal_tokens_used": "INTEGER DEFAULT 0",
            "native": "INTEGER NOT NULL DEFAULT 0",
            "archived": "INTEGER NOT NULL DEFAULT 0",
            "trashed": "INTEGER NOT NULL DEFAULT 0",
            "trashed_at": "TEXT",
            "trashed_reason": "TEXT DEFAULT ''",
            "last_interaction_at": "TEXT",
            "memory_mode": "TEXT DEFAULT 'enabled'",
            "reasoning_effort": "TEXT DEFAULT ''",
            "service_tier": "TEXT DEFAULT ''",
            "personality": "TEXT DEFAULT ''",
            "collaboration_mode": "TEXT DEFAULT 'default'",
            "permission_profile": "TEXT DEFAULT ''",
            "execution_source": "TEXT DEFAULT ''",
            "execution_turn_id": "TEXT DEFAULT ''",
            "run_mode": "TEXT DEFAULT ''",
            "ssh_host": "TEXT DEFAULT ''",
        }
        for column, definition in task_migrations.items():
            if column not in task_columns:
                connection.execute(f"ALTER TABLE tasks ADD COLUMN {column} {definition}")
        connection.execute(
            "UPDATE tasks SET last_interaction_at=max(created_at, "
            "COALESCE((SELECT MAX(ts) FROM events WHERE events.task_id=tasks.id "
            "AND json_extract(events.payload, '$.type') IN ('userMessage','browserMessage','slashCommand','agentMessage','commandResult')), created_at), "
            "COALESCE((SELECT MAX(created_at) FROM task_messages WHERE task_messages.task_id=tasks.id), created_at)) "
            "WHERE COALESCE(last_interaction_at,'')=''"
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_tasks_inactive_trash ON tasks(trashed,status,last_interaction_at)")
        connection.execute(
            "CREATE TRIGGER IF NOT EXISTS trg_events_touch_interaction AFTER INSERT ON events "
            "WHEN json_extract(NEW.payload, '$.type') IN ('userMessage','browserMessage','slashCommand','agentMessage','commandResult') BEGIN "
            "UPDATE tasks SET last_interaction_at=CASE "
            "WHEN COALESCE(last_interaction_at,'')<NEW.ts THEN NEW.ts ELSE last_interaction_at END "
            "WHERE id=COALESCE(NEW.task_id,(SELECT task_id FROM sessions WHERE id=NEW.session_id)); END"
        )
        connection.execute(
            "CREATE TRIGGER IF NOT EXISTS trg_task_messages_touch_interaction AFTER INSERT ON task_messages BEGIN "
            "UPDATE tasks SET last_interaction_at=CASE "
            "WHEN COALESCE(last_interaction_at,'')<NEW.created_at THEN NEW.created_at ELSE last_interaction_at END "
            "WHERE id=NEW.task_id; END"
        )

        provider_columns = {row[1] for row in connection.execute("PRAGMA table_info(providers)")}
        provider_migrations = {
            "model_provider": "TEXT DEFAULT ''",
            "native": "INTEGER NOT NULL DEFAULT 0",
            "health_status": "TEXT DEFAULT 'unchecked'",
            "health_detail": "TEXT DEFAULT ''",
            "health_checked_at": "TEXT",
            "health_latency_ms": "INTEGER",
            "success_count": "INTEGER NOT NULL DEFAULT 0",
            "failure_count": "INTEGER NOT NULL DEFAULT 0",
            "last_success_at": "TEXT",
            "last_failure_at": "TEXT",
            "api_key": "TEXT DEFAULT ''",
        }
        for column, definition in provider_migrations.items():
            if column not in provider_columns:
                connection.execute(f"ALTER TABLE providers ADD COLUMN {column} {definition}")

    def _recover_interrupted_work(self, connection: sqlite3.Connection) -> None:
        """Recover only dashboard-owned turns; terminal turns are rediscovered separately."""
        stamp = self.clock()
        connection.execute(
            "UPDATE tasks SET execution_source='dashboard' "
            "WHERE status IN ('running','retrying') AND active_session_id IN "
            "(SELECT id FROM sessions WHERE status IN ('running','retrying')) "
            "AND COALESCE(execution_source,'')!='terminal'"
        )
        connection.execute(
            "UPDATE sessions SET status='interrupted', finished_at=?, "
            "summary='Dashboard restarted while session was running' WHERE status='running'",
            (stamp,),
        )
        connection.execute(
            "UPDATE task_messages SET status=CASE WHEN status='steering' THEN 'steered' ELSE 'sent' END, "
            "finished_at=COALESCE(finished_at, ?), error='' "
            "WHERE status IN ('running','dispatching','steering') AND EXISTS ("
            "SELECT 1 FROM events e WHERE e.task_id=task_messages.task_id "
            "AND json_extract(e.payload, '$.client_message_id')=task_messages.id "
            "AND json_extract(e.payload, '$.type') IN ('userMessage','browserMessage','slashCommand')"
            ")",
            (stamp,),
        )
        connection.execute(
            "UPDATE task_messages SET status='queued', started_at=NULL, session_id=NULL, "
            "finished_at=NULL, error='Dashboard restarted before delivery' "
            "WHERE status IN ('running','dispatching','steering')"
        )
        connection.execute(
            "UPDATE tasks SET status='queued', last_error='Dashboard restarted; task queued for resume', updated_at=? "
            "WHERE status IN ('running','retrying') AND execution_source='dashboard'",
            (stamp,),
        )
        connection.execute(
            "UPDATE tasks SET status=CASE WHEN archived=1 THEN 'archived' ELSE 'stopped' END, "
            "execution_source='',execution_turn_id='',last_error='',updated_at=? "
            "WHERE status IN ('running','retrying') AND COALESCE(execution_source,'')!='dashboard'",
            (stamp,),
        )

    def one(self, sql: str, args: tuple = ()) -> Optional[dict]:
        with self.lock, self.connect() as connection:
            row = connection.execute(sql, args).fetchone()
            return dict(row) if row else None

    def all(self, sql: str, args: tuple = ()) -> list[dict]:
        with self.lock, self.connect() as connection:
            return [dict(row) for row in connection.execute(sql, args).fetchall()]

    def execute(self, sql: str, args: tuple = ()) -> None:
        with self.lock, self.connect() as connection:
            connection.execute(sql, args)
            connection.commit()
