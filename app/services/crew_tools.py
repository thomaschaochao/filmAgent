from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from app.services.crew import CREW_ROLES, CREW_TOOLS


@dataclass(frozen=True)
class CrewToolAdapter:
    key: str
    owner_agent: str
    scope: str
    mutates_state: bool
    requires_user_approval: bool
    checkpoint_event: str
    exposed_to_crewai: bool

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CrewToolDescriptor:
    name: str
    description: str
    args_schema: dict[str, Any]
    metadata: dict[str, Any]

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


TOOL_ARGUMENT_SCHEMAS: dict[str, dict[str, Any]] = {
    "retrieve_context": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "project_id": {"type": "string"},
            "script_id": {"type": "string"},
            "content_type": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    "search_web": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    },
    "fetch_page": {
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
        "additionalProperties": False,
    },
    "create_project": {
        "type": "object",
        "properties": {"plan_id": {"type": "string"}},
        "required": ["plan_id"],
        "additionalProperties": False,
    },
    "save_script": {
        "type": "object",
        "properties": {
            "plan_id": {"type": "string"},
            "project_id": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["plan_id"],
        "additionalProperties": False,
    },
    "approve_script": {
        "type": "object",
        "properties": {"plan_id": {"type": "string"}, "script_id": {"type": "string"}},
        "required": ["plan_id"],
        "additionalProperties": False,
    },
    "extract_assets": {
        "type": "object",
        "properties": {"plan_id": {"type": "string"}, "project_id": {"type": "string"}},
        "required": ["plan_id"],
        "additionalProperties": False,
    },
    "generate_asset_prompts": {
        "type": "object",
        "properties": {
            "plan_id": {"type": "string"},
            "project_id": {"type": "string"},
            "asset_ids": {"type": "array"},
        },
        "required": ["plan_id"],
        "additionalProperties": False,
    },
    "generate_storyboard": {
        "type": "object",
        "properties": {"plan_id": {"type": "string"}, "script_id": {"type": "string"}},
        "required": ["plan_id"],
        "additionalProperties": False,
    },
    "generate_shot_prompts": {
        "type": "object",
        "properties": {
            "plan_id": {"type": "string"},
            "project_id": {"type": "string"},
            "mode": {"type": "string"},
        },
        "required": ["plan_id"],
        "additionalProperties": False,
    },
    "get_workflow_status": {
        "type": "object",
        "properties": {"plan_id": {"type": "string"}},
        "required": ["plan_id"],
        "additionalProperties": False,
    },
}


def build_tool_catalog() -> dict[str, CrewToolAdapter]:
    allowed_by_role = {
        tool_name for role in CREW_ROLES for tool_name in role.tools
    }
    return {
        tool.key: CrewToolAdapter(
            key=tool.key,
            owner_agent=tool.owner_agent,
            scope=tool.scope,
            mutates_state=tool.mutates_state,
            requires_user_approval=tool.requires_user_approval,
            checkpoint_event=tool.checkpoint_event,
            exposed_to_crewai=tool.key in allowed_by_role,
        )
        for tool in CREW_TOOLS
    }


def build_crewai_tool_descriptors() -> list[CrewToolDescriptor]:
    specs = {tool.key: tool for tool in CREW_TOOLS}
    catalog = build_tool_catalog()
    descriptors: list[CrewToolDescriptor] = []
    for key, adapter in catalog.items():
        if not adapter.exposed_to_crewai:
            continue
        descriptors.append(
            CrewToolDescriptor(
                name=key,
                description=specs[key].description,
                args_schema=TOOL_ARGUMENT_SCHEMAS[key],
                metadata={
                    "owner_agent": adapter.owner_agent,
                    "scope": adapter.scope,
                    "mutates_state": adapter.mutates_state,
                    "requires_user_approval": adapter.requires_user_approval,
                    "checkpoint_event": adapter.checkpoint_event,
                },
            )
        )
    return descriptors


def crew_tool_adapter_status() -> dict[str, Any]:
    catalog = build_tool_catalog()
    descriptors = build_crewai_tool_descriptors()
    writable = [tool.key for tool in catalog.values() if tool.mutates_state]
    read_only = [tool.key for tool in catalog.values() if not tool.mutates_state]
    return {
        "catalog_ready": True,
        "descriptors_ready": True,
        "tool_count": len(catalog),
        "descriptor_count": len(descriptors),
        "read_only_tools": read_only,
        "writable_tools": writable,
        "all_writes_require_approval": all(
            tool.requires_user_approval for tool in catalog.values() if tool.mutates_state
        ),
        "crewai_exposed_tools": [
            tool.key for tool in catalog.values() if tool.exposed_to_crewai
        ],
    }


def require_registered_tool(tool_name: str) -> CrewToolAdapter:
    catalog = build_tool_catalog()
    try:
        return catalog[tool_name]
    except KeyError as exc:
        raise ValueError(f"Unregistered CrewAI tool: {tool_name}") from exc
