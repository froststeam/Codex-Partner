"""Canonical interaction state for session, Goal, and composer controls."""

from __future__ import annotations

from typing import Any, Mapping


BUSY_SESSION_STATUSES = frozenset({"running", "queued", "retrying"})
RECOVERABLE_GOAL_STATUSES = frozenset({"paused", "blocked", "planned"})
TERMINAL_GOAL_STATUSES = frozenset({"complete"})
VALID_GOAL_STATUSES = frozenset({"none", "planned", "active", "paused", "blocked", "complete"})


def canonical_goal_status(task: Mapping[str, Any]) -> str:
    if not str(task.get("goal") or "").strip():
        return "none"
    status = str(task.get("goal_status") or "paused")
    if status == "active" and str(task.get("status") or "") == "stopped":
        return "paused"
    return status if status in VALID_GOAL_STATUSES else "paused"


def goal_should_run(task: Mapping[str, Any]) -> bool:
    status = canonical_goal_status(task)
    if status == "active":
        return True
    return bool(task.get("retry_forever") and status in RECOVERABLE_GOAL_STATUSES)


def goal_status_after_failure(task: Mapping[str, Any], *, user_stopped: bool = False) -> str:
    status = canonical_goal_status(task)
    if user_stopped:
        return "paused" if status == "active" else status
    if status == "active":
        return "blocked"
    return status


def derive_interaction_state(task: Mapping[str, Any]) -> dict[str, Any]:
    goal = str(task.get("goal") or "").strip()
    goal_status = canonical_goal_status(task)
    session_status = str(task.get("status") or "available")
    session_busy = session_status in BUSY_SESSION_STATUSES or bool(task.get("external_running"))
    retry_enabled = bool(goal and goal_status != "complete")
    retry_on = bool(retry_enabled and task.get("retry_forever"))
    run_mode = str(task.get("run_mode") or "")

    transition = ""
    if goal_status == "active" and not session_busy:
        transition = "starting"
    elif goal_status == "active" and session_status == "queued" and run_mode == "goal_resume":
        transition = "starting"
    elif goal_status in RECOVERABLE_GOAL_STATUSES and retry_on:
        transition = "recovering"

    if not goal:
        goal_action = "set"
    elif transition:
        goal_action = transition
    elif goal_status == "active":
        goal_action = "pause"
    elif goal_status == "blocked":
        goal_action = "continue"
    elif goal_status == "complete":
        goal_action = "restart"
    else:
        goal_action = "start"

    action_enabled = goal_action not in {"starting", "pausing", "recovering"}
    disabled_reason = ""
    if goal_action == "pause" and retry_on:
        action_enabled = False
        disabled_reason = "disable_retry"
    elif not action_enabled:
        disabled_reason = "transition"

    return {
        "mode": "plain" if not goal else ("goal_retry" if retry_on else "goal"),
        "sessionStatus": session_status,
        "sessionBusy": session_busy,
        "composerAction": "pause" if session_busy else "send",
        "goalStatus": goal_status,
        "goalAction": goal_action,
        "goalActionEnabled": action_enabled,
        "goalActionDisabledReason": disabled_reason,
        "goalEditEnabled": bool(goal and not transition),
        "goalClearEnabled": bool(goal and goal_status != "active" and not transition),
        "retryEnabled": retry_enabled and not bool(transition),
        "retryOn": retry_on,
        "goalShouldRun": goal_should_run(task),
        "ordinaryMessageGoalStatus": goal_status,
    }
