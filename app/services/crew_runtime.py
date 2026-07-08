from __future__ import annotations

import os
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any

from pydantic import ConfigDict, create_model

from app.services.crew import CREW_ROLES, CREW_TASKS, MEMORY_ISOLATION
from app.services.crew_tool_executor import execute_crewai_tool
from app.services.crew_tools import build_crewai_tool_descriptors, crew_tool_adapter_status


@dataclass(frozen=True)
class CrewRuntimeBuild:
    crew: Any | None
    status: dict[str, Any]


@dataclass(frozen=True)
class CrewRuntimeToolHandle:
    name: str
    description: str
    args_schema: dict[str, Any]
    metadata: dict[str, Any]

    def run(self, **kwargs: Any) -> dict[str, Any]:
        return execute_crewai_tool(self.name, **kwargs)


def crew_runtime_factory_status() -> dict[str, Any]:
    tool_descriptors = build_crewai_tool_descriptors()
    return {
        "factory_ready": True,
        "instantiates_on_demand": True,
        "agent_count": len(CREW_ROLES),
        "task_count": len(CREW_TASKS),
        "tool_descriptor_count": len(tool_descriptors),
        "tool_handle_count": len(tool_descriptors),
        "tool_handles_bound": True,
    }


def instantiate_crewai_runtime(crewai_module: Any | None = None) -> CrewRuntimeBuild:
    module = crewai_module
    if module is None:
        try:
            _ensure_crewai_storage_dir()
            module = import_module("crewai")
        except ImportError:
            return CrewRuntimeBuild(
                crew=None,
                status={
                    **crew_runtime_factory_status(),
                    "installed": False,
                    "instantiated": False,
                    "fallback": "orchestrator",
                    "error": "",
                },
            )

    try:
        agent_cls = module.Agent
        task_cls = module.Task
        crew_cls = module.Crew
        process_class = getattr(module, "Process", None)
        process = getattr(process_class, "sequential", None)
        _ensure_crewai_storage_dir()
        tool_handles = _tool_handles_by_name()
        agents = {
            role.key: agent_cls(
                role=role.name,
                goal=role.responsibility,
                backstory=_agent_backstory(role.key, role.tools),
                tools=[
                    tool_handles[tool_name]
                    for tool_name in role.tools
                    if tool_name in tool_handles
                ],
                allow_delegation=False,
                verbose=False,
            )
            for role in CREW_ROLES
        }
        tasks = [
            task_cls(
                description=task.description,
                expected_output=_expected_output(task.stage),
                agent=agents[task.agent],
            )
            for task in CREW_TASKS
        ]
        kwargs = {"agents": list(agents.values()), "tasks": tasks, "verbose": False}
        if "memory" in getattr(crew_cls, "model_fields", {}):
            kwargs["memory"] = False
        if process is not None:
            kwargs["process"] = process
        crew = crew_cls(**kwargs)
    except Exception as exc:
        return CrewRuntimeBuild(
            crew=None,
            status={
                **crew_runtime_factory_status(),
                "installed": True,
                "instantiated": False,
                "fallback": "orchestrator",
                "error": _safe_error(exc),
            },
        )
    return CrewRuntimeBuild(
        crew=crew,
        status={
            **crew_runtime_factory_status(),
            "installed": True,
            "instantiated": True,
            "fallback": "",
            "error": "",
        },
    )


def crew_runtime_preflight() -> dict[str, Any]:
    build = instantiate_crewai_runtime()
    adapter = crew_tool_adapter_status()
    status = build.status
    can_exercise_workflow = bool(
        adapter["catalog_ready"]
        and adapter["descriptors_ready"]
        and (status.get("instantiated") or status.get("fallback") == "orchestrator")
    )
    return {
        "framework": "crewai",
        "factory_ready": status["factory_ready"],
        "catalog_ready": adapter["catalog_ready"],
        "descriptors_ready": adapter["descriptors_ready"],
        "installed": bool(status.get("installed")),
        "instantiated": bool(status.get("instantiated")),
        "fallback": status.get("fallback", ""),
        "agent_count": status["agent_count"],
        "task_count": status["task_count"],
        "tool_descriptor_count": status["tool_descriptor_count"],
        "tool_handle_count": status["tool_handle_count"],
        "tool_handles_bound": bool(status.get("tool_handles_bound")),
        "can_exercise_workflow": can_exercise_workflow,
        "message": (
            "CrewAI runtime instantiated; workflow can exercise CrewAI runtime boundaries."
            if status.get("instantiated")
            else "CrewAI is not active; workflow can be exercised through orchestrator fallback."
        ),
        "error": status.get("error", ""),
    }


def _tool_handles_by_name() -> dict[str, Any]:
    try:
        base_tool_cls = import_module("crewai.tools").BaseTool
    except (ImportError, AttributeError):
        base_tool_cls = None

    handles: dict[str, Any] = {}
    for descriptor in build_crewai_tool_descriptors():
        if base_tool_cls is None:
            handles[descriptor.name] = CrewRuntimeToolHandle(
                name=descriptor.name,
                description=descriptor.description,
                args_schema=descriptor.args_schema,
                metadata=descriptor.metadata,
            )
            continue
        handles[descriptor.name] = _build_crewai_base_tool(descriptor, base_tool_cls)
    return handles


def _ensure_crewai_storage_dir() -> None:
    storage_dir = Path("storage") / "crewai"
    storage_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("CREWAI_STORAGE_DIR", str(storage_dir.resolve()))


def _build_crewai_base_tool(descriptor: Any, base_tool_cls: type[Any]) -> Any:
    args_model = _args_model_from_json_schema(descriptor.name, descriptor.args_schema)

    class RegisteredCrewTool(base_tool_cls):
        def _run(self, **kwargs: Any) -> dict[str, Any]:
            return execute_crewai_tool(descriptor.name, **kwargs)

    RegisteredCrewTool.__name__ = f"{descriptor.name.title().replace('_', '')}Tool"
    return RegisteredCrewTool(
        name=descriptor.name,
        description=descriptor.description,
        args_schema=args_model,
    )


def _args_model_from_json_schema(tool_name: str, schema: dict[str, Any]) -> type[Any]:
    required = set(schema.get("required", []))
    fields = {
        name: (Any, ... if name in required else None)
        for name in schema.get("properties", {})
    }
    if not fields:
        fields = {"input": (str, None)}
    return create_model(
        f"{tool_name.title().replace('_', '')}Args",
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )


def _agent_backstory(agent_key: str, tools: tuple[str, ...]) -> str:
    isolation = MEMORY_ISOLATION.get(agent_key, {})
    return (
        "FilmAgent controlled CrewAI worker. "
        f"Allowed tools: {', '.join(tools)}. "
        f"Readable memory: {', '.join(isolation.get('read', []))}. "
        f"Writable memory: {', '.join(isolation.get('write', []))}. "
        "Never access project data outside registered tools."
    )


def _expected_output(stage: str) -> str:
    return (
        f"Structured result for workflow stage `{stage}`. "
        "Do not mutate business data directly; use registered internal tools only."
    )


def _safe_error(exc: Exception) -> str:
    message = str(exc)
    for marker in ("Authorization", "Bearer ", "api_key", "API key", "FILMAGENT_"):
        message = message.replace(marker, "[redacted]")
    return message[:500]
