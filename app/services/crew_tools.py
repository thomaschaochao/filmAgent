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


def crew_tool_adapter_status() -> dict[str, Any]:
    catalog = build_tool_catalog()
    writable = [tool.key for tool in catalog.values() if tool.mutates_state]
    read_only = [tool.key for tool in catalog.values() if not tool.mutates_state]
    return {
        "catalog_ready": True,
        "tool_count": len(catalog),
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
