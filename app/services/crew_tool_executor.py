from __future__ import annotations

import asyncio
from typing import Any

from fastapi import BackgroundTasks
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
    plan_id = _optional_string(kwargs.get("plan_id"))
    if not plan_id:
        raise CrewToolExecutionError(
            f"CrewAI tool `{tool_name}` is write-scoped and requires an approved workflow plan_id."
        )
    if tool_name == "create_project":
        return _approve_plan(plan_id)
    stage = _stage_for_write_tool(tool_name)
    if stage is None:
        raise CrewToolExecutionError(f"Unsupported write-scoped CrewAI tool: {tool_name}")
    return _approve_stage(plan_id, stage)


def _stage_for_write_tool(tool_name: str) -> str | None:
    return {
        "save_script": "project_script",
        "approve_script": "assets",
        "extract_assets": "assets",
        "generate_asset_prompts": "assets",
        "generate_storyboard": "shots",
        "generate_shot_prompts": "prompts",
    }.get(tool_name)


def _approve_plan(plan_id: str) -> dict[str, Any]:
    from app.main import approve_agent_plan

    with SessionLocal() as db:
        session = approve_agent_plan(plan_id, BackgroundTasks(), db)
        return {
            "session_id": session.id,
            "project_id": session.project_id,
            "status": (
                session.status.value if hasattr(session.status, "value") else str(session.status)
            ),
            "current_stage": session.current_stage,
        }


def _approve_stage(plan_id: str, stage: str) -> dict[str, Any]:
    from app.main import approve_agent_stage

    with SessionLocal() as db:
        task = approve_agent_stage(plan_id, stage, db)
        return {
            "task_id": task.id,
            "plan_id": task.plan_id,
            "stage": task.stage,
            "status": task.status.value if hasattr(task.status, "value") else str(task.status),
            "result": task.result_data or {},
        }


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
