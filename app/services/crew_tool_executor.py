from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import select

from app.database import SessionLocal
from app.models import WorkflowPlan
from app.schemas import RetrievalQuery
from app.services.agent_retrieval import retrieve_context
from app.services.checkpoint import sanitize_error_message
from app.services.crew_tools import require_registered_tool
from app.services.web_tools import fetch_page, search_web


class CrewToolExecutionError(RuntimeError):
    pass


def execute_crewai_tool(tool_name: str, **kwargs: Any) -> Any:
    adapter = require_registered_tool(tool_name)
    try:
        if adapter.mutates_state:
            return _execute_write_tool(tool_name, kwargs)
        return _execute_read_tool(tool_name, kwargs)
    except CrewToolExecutionError:
        raise
    except Exception as exc:
        raise CrewToolExecutionError(sanitize_error_message(str(exc))) from exc


def _execute_read_tool(tool_name: str, kwargs: dict[str, Any]) -> Any:
    if tool_name == "retrieve_context":
        return _retrieve_context(kwargs)
    if tool_name == "get_workflow_status":
        return _get_workflow_status(kwargs)
    if tool_name == "search_web":
        return asyncio.run(search_web(str(kwargs["query"])))
    if tool_name == "fetch_page":
        return asyncio.run(fetch_page(str(kwargs["url"])))
    raise CrewToolExecutionError(f"Unsupported read-only CrewAI tool: {tool_name}")


def _execute_write_tool(tool_name: str, kwargs: dict[str, Any]) -> Any:
    raise CrewToolExecutionError(
        f"CrewAI tool `{tool_name}` is write-scoped and must be executed through "
        "the stage approval API so checkpoint, idempotency, and user approval are preserved."
    )


def _retrieve_context(kwargs: dict[str, Any]) -> list[dict]:
    payload = RetrievalQuery(
        query=str(kwargs["query"]),
        project_id=_optional_string(kwargs.get("project_id")),
        script_id=_optional_string(kwargs.get("script_id")),
        limit=int(kwargs.get("limit") or 8),
    )
    with SessionLocal() as db:
        return retrieve_context(payload, db)


def _get_workflow_status(kwargs: dict[str, Any]) -> dict[str, Any]:
    plan_id = str(kwargs["plan_id"])
    with SessionLocal() as db:
        plan = db.scalar(select(WorkflowPlan).where(WorkflowPlan.id == plan_id))
        if plan is None:
            raise CrewToolExecutionError("Workflow plan not found")
        return {
            "plan_id": plan.id,
            "session_id": plan.session_id,
            "status": plan.status,
            "current_stage": plan.session.current_stage if plan.session else None,
            "tasks": [
                {
                    "id": task.id,
                    "stage": task.stage,
                    "status": (
                        task.status.value if hasattr(task.status, "value") else str(task.status)
                    ),
                    "retry_count": task.retry_count,
                    "checkpoint": (task.result_data or {}).get("checkpoint"),
                }
                for task in plan.tasks
            ],
        }


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
