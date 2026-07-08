from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.models import WorkflowTask
from app.services.checkpoint import append_tool_event
from app.services.crew_tools import require_registered_tool


@dataclass(frozen=True)
class CrewStageTool:
    name: str
    run: Callable[[], Any]


def run_stage_tools(
    task: WorkflowTask, db: Session, tools: Sequence[CrewStageTool]
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for tool in tools:
        require_registered_tool(tool.name)
        append_tool_event(
            task,
            event="tool_start",
            tool_name=tool.name,
            status="running",
        )
        db.commit()
        try:
            results[tool.name] = tool.run()
        except Exception:
            db.rollback()
            task = db.get(WorkflowTask, task.id)
            append_tool_event(
                task,
                event="tool_failed",
                tool_name=tool.name,
                status="failed",
            )
            db.commit()
            raise
        append_tool_event(
            task,
            event="tool_success",
            tool_name=tool.name,
            status="completed",
            detail=_summarize_tool_result(results[tool.name]),
        )
        db.commit()
    return results


def _summarize_tool_result(result: Any) -> dict[str, Any]:
    if result is None:
        return {}
    if isinstance(result, list):
        return {"count": len(result)}
    if isinstance(result, dict):
        return {key: value for key, value in result.items() if _is_safe_scalar(value)}
    return {"type": type(result).__name__}


def _is_safe_scalar(value: Any) -> bool:
    return value is None or isinstance(value, str | int | float | bool)
