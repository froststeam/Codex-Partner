"""Durable projection of conversation events into a compact activity graph."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable, Optional

from codex_partner.database import Database


PROJECTION_VERSION = 4
ACTIVE_STATUSES = {"active", "planned"}
TERMINAL_NODE_STATUSES = {"failed", "rolledback", "abandoned"}
CORRECTION_RE = re.compile(r"改成|换成|重新|不是|只要|不要再|范围|回退|撤销|instead|rather|revert|rollback", re.I)
ROLLBACK_RE = re.compile(r"回退|撤销|恢复到|revert|rollback", re.I)
DIRECTION_RE = re.compile(
    r"帮|修|改|增加|添加|删除|实现|排查|检查|优化|设计|支持|恢复|回退|提交|推送|重启|需要|不要|必须|应该|"
    r"为什么|为啥|异常|失败|卡|问题|bug|please|fix|add|remove|implement|investigate|debug|optimi[sz]e|design|support|error|fail",
    re.I,
)
DECISION_RE = re.compile(r"决定|确认|根因|证明|改用|转向|放弃|不可行|关键是|结论|选择|root cause|decid|confirmed|switch(?:ing)? to|not viable", re.I)
SWITCH_RE = re.compile(r"改用|转向|放弃|不可行|switch(?:ing)? to|not viable", re.I)
CONTINUE_RE = re.compile(r"^\s*(继续|接着|然后|下一步|再|also\b|continue\b|next\b)", re.I)
SEMANTIC_STOP_WORDS = {
    "这个", "那个", "我们", "你们", "帮我", "看看", "一下", "需要", "应该", "可以", "还是", "现在", "之前",
    "问题", "实现", "修复", "修改", "增加", "添加", "优化", "检查", "继续", "支持", "功能", "相关", "进行",
    "the", "and", "for", "with", "this", "that", "from", "into", "please", "fix", "add", "check",
}
HIDDEN_CONTEXT_RE = re.compile(
    r"<(environment_context|codex_internal_context|skills_instructions|plugins_instructions|system_reminder|memory_context|turn_context|oai-mem-citation)\b[^>]*>[\s\S]*?</\1\s*>",
    re.I,
)


def payload_of(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    if isinstance(payload, dict):
        return payload
    try:
        decoded = json.loads(payload or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def clean_text(value: Any) -> str:
    text = HIDDEN_CONTEXT_RE.sub("", str(value or ""))
    text = re.sub(r"<(?:image|audio|video)\b[^>]*>[\s\S]*?</(?:image|audio|video)>", " ", text, flags=re.I)
    text = re.sub(r"\[\[(?:codex-input|codex-file):[^\]]+\]\]", " ", text)
    text = re.sub(r"```[\s\S]*?```", " ", text)
    text = re.sub(r"[`*_#>\[\]()]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def short_title(value: Any, fallback: str = "关键活动") -> str:
    text = clean_text(value)
    if not text:
        return fallback
    first = re.split(r"(?<=[。！？!?])\s+|\n", text, maxsplit=1)[0].strip()
    return first if len(first) <= 58 else f"{first[:57]}…"


def fingerprint(value: Any) -> str:
    return hashlib.sha1(str(value or "").encode("utf-8", "replace")).hexdigest()[:14]


def substantive_direction(value: Any) -> bool:
    text = short_title(value, "")
    if not text or text.startswith("/"):
        return False
    if re.fullmatch(r"\s*(继续|继续吧|好|好的|可以|行|嗯|收到|重试|再试一次|实现了吧|ok|okay|yes|go on)[！!。.？?\s]*", text, re.I):
        return False
    score = 2 if len(text) >= 18 else 0
    if DIRECTION_RE.search(text):
        score += 5
    if CORRECTION_RE.search(text):
        score += 3
    return score >= 5


def semantic_terms(value: Any) -> set[str]:
    text = clean_text(value).lower()
    terms = {
        token for token in re.findall(r"[a-z][a-z0-9_.+-]{2,}", text)
        if token not in SEMANTIC_STOP_WORDS
    }
    for phrase in re.findall(r"[\u3400-\u9fff]+", text):
        for stop in SEMANTIC_STOP_WORDS:
            phrase = phrase.replace(stop, " ")
        for segment in phrase.split():
            terms.update(segment[index:index + 2] for index in range(len(segment) - 1))
    return terms


def related_direction_parent(text: Any, directions: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not directions:
        return None
    previous = directions[-1]
    if CONTINUE_RE.search(clean_text(text)):
        return previous
    terms = semantic_terms(text)
    if not terms:
        return previous

    ranked: list[tuple[float, int, dict[str, Any]]] = []
    for index, candidate in enumerate(directions):
        candidate_terms = semantic_terms(candidate.get("title"))
        overlap = terms & candidate_terms
        if not overlap:
            score = 0.0
        else:
            score = (2 * len(overlap)) / max(3, len(terms) + len(candidate_terms))
        ranked.append((score, index, candidate))
    best_score, _, best = max(ranked, key=lambda item: (item[0], item[1]))
    previous_score = ranked[-1][0]
    if best_score >= 0.16 and (best is previous or best_score >= previous_score + 0.08):
        return best
    if previous_score >= 0.12:
        return previous
    return directions[0]


def event_key(event: dict[str, Any]) -> str:
    payload = payload_of(event)
    event_id = event.get("id")
    if event_id not in (None, ""):
        return f"{event.get('stream', 'event')}:{event_id}"
    identity = ":".join(
        str(payload.get(key) or "")
        for key in ("type", "turn_id", "turnId", "item_id", "id", "status", "phase")
    )
    body = payload.get("text") or payload.get("command") or payload.get("tool") or ""
    source = f"{event.get('ts', '')}:{identity}:{body}"
    return f"live:{fingerprint(source)}"


def semantic_event(event: dict[str, Any]) -> bool:
    payload = payload_of(event)
    kind = str(payload.get("type") or "").lower()
    return kind in {"usermessage", "browsermessage", "agentmessage"} or bool(payload.get("plan")) or any(
        marker in kind for marker in ("plan", "reason", "command", "file", "tool", "mcp", "search")
    )


def project_events(
    events: Iterable[dict[str, Any]],
    *,
    initial_nodes: Optional[list[dict[str, Any]]] = None,
    task_running: bool = False,
    task_status: str = "",
    finalize: bool = True,
) -> tuple[list[dict[str, Any]], list[str]]:
    nodes = [dict(node) for node in (initial_nodes or [])]
    for node in nodes:
        for field in ("evidence", "files", "commands"):
            node[field] = list(node.get(field) or [])
    by_id = {node["id"]: node for node in nodes}
    consumed: list[str] = []
    latest_node = nodes[-1] if nodes else None
    latest_direction = next((node for node in reversed(nodes) if node.get("kind") in {"direction", "steering", "rollback"}), None)
    phases_by_turn = {node.get("turnId"): node for node in nodes if node.get("kind") == "phase" and node.get("turnId")}

    def add_node(node: dict[str, Any]) -> dict[str, Any]:
        nonlocal latest_node
        existing = by_id.get(node["id"])
        if existing:
            return existing
        node = {"summary": "", "evidence": [], "files": [], "commands": [], "failures": 0, **node}
        node["sequence"] = len(nodes)
        nodes.append(node)
        by_id[node["id"]] = node
        latest_node = node
        return node

    def add_unique(node: Optional[dict[str, Any]], field: str, value: Any, limit: int) -> None:
        text = short_title(value, "") if field == "evidence" else str(value or "").strip()
        if node and text and text not in node[field] and len(node[field]) < limit:
            node[field].append(text)

    for event in events:
        if not semantic_event(event):
            continue
        payload = payload_of(event)
        kind = str(payload.get("type") or "").lower()
        key = event_key(event)
        consumed.append(key)
        turn_id = str(payload.get("turn_id") or payload.get("turnId") or "")
        item_id = str(payload.get("item_id") or payload.get("id") or "")
        event_time = event.get("ts") or event.get("created_at") or ""
        source_id = str(event.get("id") or key)

        if kind in {"usermessage", "browsermessage"}:
            text = payload.get("text") or "\n".join(
                str(part.get("text") or "") for part in (payload.get("content") or []) if isinstance(part, dict)
            )
            if not substantive_direction(text):
                continue
            correction = bool(CORRECTION_RE.search(str(text)))
            rollback = bool(ROLLBACK_RE.search(str(text)))
            previous = latest_direction
            directions = [node for node in nodes if node.get("kind") in {"direction", "steering", "rollback"}]
            if correction and previous and previous.get("status") in ACTIVE_STATUSES:
                previous["status"] = "rolledback" if rollback else "abandoned"
            parent = previous if correction else related_direction_parent(text, directions)
            parent_id = previous.get("parentId") if correction and previous else (parent or {}).get("id")
            source = item_id or source_id or f"{turn_id}:{event_time}"
            latest_direction = add_node({
                "id": f"direction-{fingerprint(source or text)}",
                "parentId": parent_id,
                "kind": "rollback" if rollback else "steering" if correction else "direction",
                "title": short_title(text, "调整目标"),
                "status": "active",
                "turnId": turn_id,
                "itemId": item_id,
                "sourceEventId": source_id,
                "time": event_time,
                "score": 8 if correction else 6,
            })
            latest_node = latest_direction
            continue

        detail = str(payload.get("text") or payload.get("summary") or "").strip()
        if "reason" in kind and DECISION_RE.search(detail):
            title = short_title(detail, "形成关键结论")
            node_id = f"decision-{fingerprint(f'{turn_id or item_id or source_id}:decision:{title}')}"
            switches = bool(SWITCH_RE.search(detail))
            active_plan = next((
                node for node in reversed(nodes)
                if node.get("kind") == "plan" and node.get("status") == "active"
                and (not turn_id or node.get("turnId") == turn_id)
            ), None)
            previous = active_plan or latest_direction or latest_node
            if switches and previous and previous.get("status") in ACTIVE_STATUSES:
                previous["status"] = "abandoned"
            decision = add_node({
                "id": node_id,
                "parentId": previous.get("parentId") if switches and previous else (previous or {}).get("id"),
                "kind": "decision",
                "title": title,
                "status": "completed",
                "turnId": turn_id,
                "itemId": item_id,
                "sourceEventId": source_id,
                "time": event_time,
                "score": 8,
            })
            add_unique(decision, "evidence", detail, 4)

        plans = payload.get("plan") if isinstance(payload.get("plan"), list) else []
        phase = None
        valid_plans = [plan for plan in plans if isinstance(plan, dict)]
        if valid_plans:
            phase_key = turn_id or item_id or source_id
            phase = phases_by_turn.get(phase_key)
            active_step = next((plan for plan in valid_plans if plan.get("status") == "in_progress"), None)
            representative = active_step or next((plan for plan in valid_plans if plan.get("status") != "completed"), None) or valid_plans[-1]
            phase_title = short_title(representative.get("step") or representative.get("text"), "执行计划")
            plan_states = {str(plan.get("status") or "pending").lower() for plan in valid_plans}
            phase_status = "failed" if "failed" in plan_states else "active" if "in_progress" in plan_states else "planned" if "pending" in plan_states else "completed"
            if not phase:
                phase = add_node({
                    "id": f"phase-{fingerprint(phase_key)}",
                    "parentId": (latest_direction or {}).get("id"),
                    "kind": "phase",
                    "title": f"执行阶段：{phase_title}",
                    "status": phase_status,
                    "turnId": turn_id,
                    "itemId": item_id,
                    "sourceEventId": source_id,
                    "time": event_time,
                    "score": 7,
                })
                phases_by_turn[phase_key] = phase
            else:
                phase["title"] = f"执行阶段：{phase_title}"
                phase["status"] = phase_status
                phase["time"] = event_time or phase.get("time", "")
        for plan in plans:
            if not isinstance(plan, dict):
                continue
            title = short_title(plan.get("step") or plan.get("text"), "计划步骤")
            if not title:
                continue
            node_id = f"plan-{fingerprint(f'{turn_id or item_id or source_id}:{title}')}"
            plan_status = str(plan.get("status") or "pending").lower()
            status = "completed" if plan_status == "completed" else "active" if plan_status == "in_progress" else "failed" if plan_status == "failed" else "planned"
            node = by_id.get(node_id)
            if not node:
                node = add_node({
                    "id": node_id,
                    "parentId": (phase or latest_direction or {}).get("id"),
                    "kind": "plan",
                    "title": title,
                    "status": status,
                    "turnId": turn_id,
                    "itemId": item_id,
                    "sourceEventId": source_id,
                    "time": event_time,
                    "score": 7,
                })
            else:
                node["status"] = status
                node["time"] = event_time or node.get("time", "")

        target = next((node for node in reversed(nodes) if node.get("status") == "active"), None)
        target = target or next((node for node in reversed(nodes) if node.get("status") == "planned"), None) or latest_node
        if target:
            command = payload.get("command")
            if command:
                add_unique(target, "commands", command, 5)
            for change in payload.get("changes") or []:
                path = change.get("path") if isinstance(change, dict) else change
                add_unique(target, "files", path, 8)
            if detail and ("reason" in kind or "search" in kind):
                add_unique(target, "evidence", detail, 4)
            status = str(payload.get("status") or "").lower()
            exit_code = payload.get("exit_code", payload.get("exitCode"))
            if status == "failed" or (exit_code is not None and str(exit_code) not in {"", "0"}):
                target["failures"] = int(target.get("failures") or 0) + 1

        if kind == "agentmessage" and latest_node:
            latest_node["summary"] = short_title(payload.get("text"), latest_node.get("summary") or "")
            if not task_running and latest_node.get("status") == "active":
                latest_node["status"] = "completed"

    if finalize:
        if task_running:
            active = next((node for node in reversed(nodes) if node.get("status") == "active"), None)
            active = active or next((node for node in reversed(nodes) if node.get("status") == "planned"), None) or latest_node
            for node in nodes:
                if node is not active and node.get("status") == "active":
                    node["status"] = "completed"
            if active and active.get("status") not in TERMINAL_NODE_STATUSES:
                active["status"] = "active"
        elif task_status == "failed":
            failed = next((node for node in reversed(nodes) if node.get("status") not in {"rolledback", "abandoned"}), None)
            if failed:
                failed["status"] = "failed"
        elif task_status == "stopped":
            stopped = next((node for node in reversed(nodes) if node.get("status") == "active"), None)
            if stopped:
                stopped["status"] = "planned"
        else:
            for node in nodes:
                if node.get("status") == "active":
                    node["status"] = "completed"
    return nodes, consumed


class ActivityGraphStore:
    def __init__(self, database: Database):
        self.db = database

    def snapshot(self, task_id: str) -> dict[str, Any]:
        meta = self.db.one("SELECT * FROM activity_graphs WHERE task_id=?", (task_id,)) or {
            "task_id": task_id, "status": "pending", "projection_version": PROJECTION_VERSION,
            "processed_events": 0, "event_count": 0, "revision": 0, "error": "",
        }
        rows = self.db.all("SELECT * FROM activity_nodes WHERE task_id=? ORDER BY sequence", (task_id,))
        nodes = []
        for row in rows:
            nodes.append({
                "id": row["node_id"], "parentId": row.get("parent_id"), "kind": row["kind"],
                "title": row["title"], "status": row["status"], "summary": row.get("summary") or "",
                "evidence": json.loads(row.get("evidence_json") or "[]"),
                "files": json.loads(row.get("files_json") or "[]"),
                "commands": json.loads(row.get("commands_json") or "[]"),
                "failures": row.get("failures") or 0, "score": row.get("score") or 0,
                "turnId": row.get("turn_id") or "", "itemId": row.get("item_id") or "",
                "sourceEventId": row.get("source_event_id") or "", "time": row.get("event_time") or "",
                "sequence": row["sequence"],
            })
        return {**meta, "nodes": nodes}

    def mark_building(self, task_id: str, stamp: str) -> None:
        self.db.execute(
            "INSERT INTO activity_graphs (task_id,projection_version,status,processed_events,event_count,revision,started_at,updated_at,error) "
            "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET projection_version=excluded.projection_version,status='building',"
            "processed_events=0,started_at=excluded.started_at,updated_at=excluded.updated_at,error=''",
            (task_id, PROJECTION_VERSION, "building", 0, 0, 0, stamp, stamp, ""),
        )

    def replace(self, task_id: str, nodes: list[dict[str, Any]], seen: Iterable[str], event_count: int, stamp: str) -> int:
        with self.db.lock, self.db.connect() as connection:
            revision_row = connection.execute("SELECT revision FROM activity_graphs WHERE task_id=?", (task_id,)).fetchone()
            revision = int(revision_row[0] if revision_row else 0) + 1
            connection.execute("DELETE FROM activity_nodes WHERE task_id=?", (task_id,))
            connection.execute("DELETE FROM activity_graph_seen WHERE task_id=?", (task_id,))
            connection.executemany(
                "INSERT INTO activity_nodes (task_id,node_id,sequence,parent_id,kind,status,title,summary,evidence_json,files_json,commands_json,failures,score,turn_id,item_id,source_event_id,event_time) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [self._node_row(task_id, node, index) for index, node in enumerate(nodes)],
            )
            connection.executemany(
                "INSERT OR IGNORE INTO activity_graph_seen (task_id,event_key) VALUES (?,?)",
                [(task_id, key) for key in seen],
            )
            connection.execute(
                "INSERT INTO activity_graphs (task_id,projection_version,status,processed_events,event_count,revision,started_at,updated_at,error) "
                "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET projection_version=excluded.projection_version,status='ready',"
                "processed_events=excluded.processed_events,event_count=excluded.event_count,revision=excluded.revision,updated_at=excluded.updated_at,error=''",
                (task_id, PROJECTION_VERSION, "ready", event_count, event_count, revision, stamp, stamp, ""),
            )
            connection.commit()
        return revision

    def apply_event(self, task_id: str, event: dict[str, Any], task_running: bool, task_status: str, stamp: str) -> Optional[dict[str, Any]]:
        if not semantic_event(event):
            return None
        key = event_key(event)
        with self.db.lock, self.db.connect() as connection:
            meta = connection.execute("SELECT status,revision,event_count FROM activity_graphs WHERE task_id=?", (task_id,)).fetchone()
            if not meta or meta[0] != "ready" or connection.execute(
                "SELECT 1 FROM activity_graph_seen WHERE task_id=? AND event_key=?", (task_id, key)
            ).fetchone():
                return None
        before = self.snapshot(task_id)["nodes"]
        nodes, _ = project_events([event], initial_nodes=before, task_running=task_running, task_status=task_status, finalize=True)
        before_by_id = {node["id"]: node for node in before}
        changed = [node for node in nodes if before_by_id.get(node["id"]) != node]
        with self.db.lock, self.db.connect() as connection:
            if connection.execute("SELECT 1 FROM activity_graph_seen WHERE task_id=? AND event_key=?", (task_id, key)).fetchone():
                return None
            for node in changed:
                connection.execute(
                    "INSERT INTO activity_nodes (task_id,node_id,sequence,parent_id,kind,status,title,summary,evidence_json,files_json,commands_json,failures,score,turn_id,item_id,source_event_id,event_time) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(task_id,node_id) DO UPDATE SET sequence=excluded.sequence,parent_id=excluded.parent_id,"
                    "kind=excluded.kind,status=excluded.status,title=excluded.title,summary=excluded.summary,evidence_json=excluded.evidence_json,files_json=excluded.files_json,"
                    "commands_json=excluded.commands_json,failures=excluded.failures,score=excluded.score,turn_id=excluded.turn_id,item_id=excluded.item_id,"
                    "source_event_id=excluded.source_event_id,event_time=excluded.event_time",
                    self._node_row(task_id, node, int(node.get("sequence") or 0)),
                )
            connection.execute("INSERT INTO activity_graph_seen (task_id,event_key) VALUES (?,?)", (task_id, key))
            revision = int(meta[1]) + 1
            connection.execute(
                "UPDATE activity_graphs SET processed_events=processed_events+1,event_count=event_count+1,revision=?,updated_at=? WHERE task_id=?",
                (revision, stamp, task_id),
            )
            connection.commit()
        return {"revision": revision, "upsert_nodes": changed, "remove_node_ids": []}

    def apply_status(self, task_id: str, task_running: bool, task_status: str, stamp: str) -> Optional[dict[str, Any]]:
        snapshot = self.snapshot(task_id)
        if snapshot.get("status") != "ready":
            return None
        before = snapshot["nodes"]
        nodes, _ = project_events([], initial_nodes=before, task_running=task_running, task_status=task_status, finalize=True)
        before_by_id = {node["id"]: node for node in before}
        changed = [node for node in nodes if before_by_id.get(node["id"]) != node]
        if not changed:
            return None
        revision = int(snapshot.get("revision") or 0) + 1
        with self.db.lock, self.db.connect() as connection:
            for node in changed:
                connection.execute(
                    "UPDATE activity_nodes SET status=? WHERE task_id=? AND node_id=?",
                    (node.get("status", "planned"), task_id, node["id"]),
                )
            connection.execute("UPDATE activity_graphs SET revision=?,updated_at=? WHERE task_id=?", (revision, stamp, task_id))
            connection.commit()
        return {"revision": revision, "upsert_nodes": changed, "remove_node_ids": []}

    @staticmethod
    def _node_row(task_id: str, node: dict[str, Any], sequence: int) -> tuple[Any, ...]:
        return (
            task_id, node["id"], sequence, node.get("parentId"), node.get("kind", "direction"), node.get("status", "planned"),
            node.get("title", "关键活动"), node.get("summary", ""), json.dumps(node.get("evidence") or [], ensure_ascii=False),
            json.dumps(node.get("files") or [], ensure_ascii=False), json.dumps(node.get("commands") or [], ensure_ascii=False),
            int(node.get("failures") or 0), int(node.get("score") or 0), node.get("turnId", ""), node.get("itemId", ""),
            node.get("sourceEventId", ""), node.get("time", ""),
        )
