from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.models import WorkflowTask, WorkflowTaskStatus

RESUMABLE_ERROR_TYPES = {"network_error", "provider_timeout", "rate_limited"}
NON_RETRYABLE_ERROR_TYPES = {"quota_exceeded", "user_interrupted"}

AGENT_BY_STAGE = {
    "project_script": "script",
    "assets": "asset",
    "shots": "shot",
    "prompts": "prompt",
    "images": "producer",
}


def classify_interruption(exc: Exception | str) -> dict[str, Any]:
    message = str(exc)
    lowered = message.casefold()
    if any(token in lowered for token in ("quota", "insufficient", "balance", "\u4f59\u989d")):
        error_type = "quota_exceeded"
        retryable = False
    elif any(
        token in lowered
        for token in ("rate limit", "429", "too many requests", "\u9650\u6d41")
    ):
        error_type = "rate_limited"
        retryable = True
    elif any(token in lowered for token in ("timeout", "timed out", "\u8d85\u65f6")):
        error_type = "provider_timeout"
        retryable = True
    elif any(token in lowered for token in ("network", "dns", "connection", "\u65ad\u7f51")):
        error_type = "network_error"
        retryable = True
    elif any(token in lowered for token in ("validation", "schema", "\u6821\u9a8c")):
        error_type = "validation_failed"
        retryable = True
    else:
        error_type = "unknown"
        retryable = True
    return {
        "type": error_type,
        "message": sanitize_error_message(message),
        "retryable": retryable,
    }


def sanitize_error_message(message: str) -> str:
    redacted = message
    for marker in ("Authorization", "Bearer ", "api_key", "API key", "FILMAGENT_"):
        if marker in redacted:
            redacted = redacted.replace(marker, "[redacted]")
    return redacted[:1000]


def build_checkpoint(
    task: WorkflowTask,
    *,
    status: str,
    last_safe_step: str,
    input_snapshot: dict | None = None,
    output_snapshot: dict | None = None,
    error: dict | None = None,
) -> dict[str, Any]:
    return {
        "plan_id": task.plan_id,
        "task_id": task.id,
        "agent_key": AGENT_BY_STAGE.get(task.stage, "producer"),
        "stage": task.stage,
        "status": status,
        "last_safe_step": last_safe_step,
        "input_snapshot": input_snapshot or {},
        "output_snapshot": output_snapshot or {},
        "tool_call_history": (task.result_data or {}).get("tool_call_history", []),
        "error": error,
        "updated_at": datetime.now(UTC).isoformat(),
    }


def write_checkpoint(
    task: WorkflowTask,
    *,
    status: str,
    last_safe_step: str,
    input_snapshot: dict | None = None,
    output_snapshot: dict | None = None,
    error: dict | None = None,
) -> dict[str, Any]:
    checkpoint = build_checkpoint(
        task,
        status=status,
        last_safe_step=last_safe_step,
        input_snapshot=input_snapshot,
        output_snapshot=output_snapshot,
        error=error,
    )
    result_data = dict(task.result_data or {})
    result_data["checkpoint"] = checkpoint
    if error:
        result_data["recovery"] = {
            "status": "resumable" if error.get("retryable") else "failed",
            "retryable": bool(error.get("retryable")),
            "error_type": error.get("type"),
            "last_safe_step": last_safe_step,
        }
    task.result_data = result_data
    return checkpoint


def append_tool_event(
    task: WorkflowTask,
    *,
    event: str,
    tool_name: str,
    status: str,
    detail: dict | None = None,
) -> dict[str, Any]:
    result_data = dict(task.result_data or {})
    history = list(result_data.get("tool_call_history", []))
    entry = {
        "event": event,
        "tool_name": tool_name,
        "status": status,
        "detail": detail or {},
        "timestamp": datetime.now(UTC).isoformat(),
    }
    history.append(entry)
    result_data["tool_call_history"] = history
    task.result_data = result_data
    return entry


def mark_running(task: WorkflowTask, *, input_snapshot: dict | None = None) -> None:
    task.status = WorkflowTaskStatus.running
    write_checkpoint(
        task,
        status="running",
        last_safe_step="stage_started",
        input_snapshot=input_snapshot,
    )


def mark_completed(task: WorkflowTask, *, output_snapshot: dict | None = None) -> None:
    task.status = WorkflowTaskStatus.completed
    write_checkpoint(
        task,
        status="completed",
        last_safe_step="task_completed",
        output_snapshot=output_snapshot,
    )


def mark_failed(task: WorkflowTask, exc: Exception | str, *, last_safe_step: str) -> dict[str, Any]:
    error = classify_interruption(exc)
    task.status = WorkflowTaskStatus.failed
    task.error_message = error["message"]
    task.retry_count += 1
    return write_checkpoint(
        task,
        status="resumable" if error["retryable"] else "failed",
        last_safe_step=last_safe_step,
        error=error,
    )
