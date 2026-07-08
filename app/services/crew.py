from __future__ import annotations

from dataclasses import dataclass
from importlib.util import find_spec
from typing import Any

from app.config import get_settings


@dataclass(frozen=True)
class CrewRole:
    key: str
    name: str
    responsibility: str
    tools: tuple[str, ...]


@dataclass(frozen=True)
class CrewTaskBlueprint:
    stage: str
    agent: str
    description: str
    requires_approval: bool = True


@dataclass(frozen=True)
class CrewToolSpec:
    key: str
    owner_agent: str
    description: str
    scope: str
    mutates_state: bool
    requires_user_approval: bool
    checkpoint_event: str


CREW_ROLES = (
    CrewRole(
        key="producer",
        name="ProducerAgent",
        responsibility="Clarify intent, coordinate stages, and protect user approval gates.",
        tools=("retrieve_context", "create_project", "save_script"),
    ),
    CrewRole(
        key="script",
        name="ScriptAgent",
        responsibility="Develop, review, chunk, and summarize scripts without overwriting history.",
        tools=("retrieve_context", "save_script", "approve_script"),
    ),
    CrewRole(
        key="research",
        name="ResearchAgent",
        responsibility="Research external facts and only persist adopted summaries.",
        tools=("search_web", "fetch_page", "retrieve_context"),
    ),
    CrewRole(
        key="asset",
        name="AssetAgent",
        responsibility="Extract characters, locations, props, and generate asset prompts.",
        tools=("extract_assets", "generate_asset_prompts"),
    ),
    CrewRole(
        key="shot",
        name="ShotAgent",
        responsibility="Split scenes into shots with duration, position, space, and action limits.",
        tools=("retrieve_context", "generate_storyboard"),
    ),
    CrewRole(
        key="prompt",
        name="PromptAgent",
        responsibility="Generate initial-frame or storyboard prompts with project continuity.",
        tools=("retrieve_context", "generate_shot_prompts"),
    ),
    CrewRole(
        key="review",
        name="ReviewAgent",
        responsibility="Check consistency, missing context, unsafe edits, and scope violations.",
        tools=("retrieve_context", "get_workflow_status"),
    ),
)

CREW_TOOLS = (
    CrewToolSpec(
        key="retrieve_context",
        owner_agent="shared",
        description="Read scoped RAG context through the retrieval service only.",
        scope="read:rag",
        mutates_state=False,
        requires_user_approval=False,
        checkpoint_event="tool_success",
    ),
    CrewToolSpec(
        key="search_web",
        owner_agent="research",
        description="Search public web results for user-approved research tasks.",
        scope="read:web",
        mutates_state=False,
        requires_user_approval=False,
        checkpoint_event="tool_success",
    ),
    CrewToolSpec(
        key="fetch_page",
        owner_agent="research",
        description="Fetch selected web pages; only adopted summaries may be persisted.",
        scope="read:web",
        mutates_state=False,
        requires_user_approval=False,
        checkpoint_event="tool_success",
    ),
    CrewToolSpec(
        key="create_project",
        owner_agent="producer",
        description="Create an idempotent project after the user approves the plan.",
        scope="write:project",
        mutates_state=True,
        requires_user_approval=True,
        checkpoint_event="tool_success",
    ),
    CrewToolSpec(
        key="save_script",
        owner_agent="script",
        description="Save a versioned script document without overwriting history.",
        scope="write:script",
        mutates_state=True,
        requires_user_approval=True,
        checkpoint_event="tool_success",
    ),
    CrewToolSpec(
        key="approve_script",
        owner_agent="script",
        description="Move the current script into approved workflow state.",
        scope="write:script",
        mutates_state=True,
        requires_user_approval=True,
        checkpoint_event="tool_success",
    ),
    CrewToolSpec(
        key="extract_assets",
        owner_agent="asset",
        description="Extract characters, locations, and props idempotently.",
        scope="write:asset",
        mutates_state=True,
        requires_user_approval=True,
        checkpoint_event="tool_success",
    ),
    CrewToolSpec(
        key="generate_asset_prompts",
        owner_agent="asset",
        description="Generate versioned prompts for approved assets.",
        scope="write:asset_prompt",
        mutates_state=True,
        requires_user_approval=True,
        checkpoint_event="tool_success",
    ),
    CrewToolSpec(
        key="generate_storyboard",
        owner_agent="shot",
        description="Create duration-aware shot breakdowns with spatial constraints.",
        scope="write:shot",
        mutates_state=True,
        requires_user_approval=True,
        checkpoint_event="tool_success",
    ),
    CrewToolSpec(
        key="generate_shot_prompts",
        owner_agent="prompt",
        description="Generate initial-frame, storyboard, or Seedance prompt drafts.",
        scope="write:shot_prompt",
        mutates_state=True,
        requires_user_approval=True,
        checkpoint_event="tool_success",
    ),
    CrewToolSpec(
        key="get_workflow_status",
        owner_agent="review",
        description="Read workflow task state, checkpoints, and pending approvals.",
        scope="read:workflow",
        mutates_state=False,
        requires_user_approval=False,
        checkpoint_event="tool_success",
    ),
)

CREW_TASKS = (
    CrewTaskBlueprint(
        stage="discovery",
        agent="producer",
        description=(
            "Collect concept, style, world setting, aspect ratio, language, and prompt mode."
        ),
        requires_approval=False,
    ),
    CrewTaskBlueprint(
        stage="project_script",
        agent="script",
        description=(
            "Create the project and preserve the initial script or concept as versioned text."
        ),
    ),
    CrewTaskBlueprint(
        stage="research",
        agent="research",
        description="Run optional web research and store only adopted summaries.",
    ),
    CrewTaskBlueprint(
        stage="assets",
        agent="asset",
        description="Extract project assets and generate role/location/prop prompts.",
    ),
    CrewTaskBlueprint(
        stage="shots",
        agent="shot",
        description="Create duration-aware shot breakdowns with spatial constraints.",
    ),
    CrewTaskBlueprint(
        stage="prompts",
        agent="prompt",
        description="Generate initial-frame or storyboard prompt versions.",
    ),
    CrewTaskBlueprint(
        stage="review",
        agent="review",
        description="Review proposed changes before user approval.",
        requires_approval=False,
    ),
)

MEMORY_ISOLATION = {
    "producer": {
        "read": ["confirmed_memory", "recent_messages", "workflow_status"],
        "write": ["workflow_plan", "coordination_decision"],
        "forbidden": ["secrets", "unadopted_web_pages", "image_binary"],
    },
    "script": {
        "read": ["confirmed_memory", "script_current_version", "script_summaries"],
        "write": ["script_versions", "script_summaries"],
        "forbidden": ["asset_private_drafts", "raw_tool_logs"],
    },
    "research": {
        "read": ["confirmed_memory", "research_requests"],
        "write": ["research_sources_adopted_summary_only"],
        "forbidden": ["unadopted_web_pages", "secrets"],
    },
    "asset": {
        "read": ["confirmed_memory", "script_chunks", "asset_versions"],
        "write": ["asset_records", "asset_prompt_versions"],
        "forbidden": ["other_project_assets", "secrets"],
    },
    "shot": {
        "read": ["confirmed_memory", "script_chunks", "asset_references"],
        "write": ["scene_records", "shot_records"],
        "forbidden": ["locked_shot_mutations", "other_project_shots"],
    },
    "prompt": {
        "read": ["confirmed_memory", "shot_records", "asset_references", "retrieved_context"],
        "write": ["prompt_versions", "prompt_strategy_snapshot"],
        "forbidden": ["overwriting_prompt_history", "other_project_prompts"],
    },
    "review": {
        "read": ["workflow_status", "checkpoint", "proposed_changes"],
        "write": ["review_notes", "validation_results"],
        "forbidden": ["business_mutations"],
    },
}

CHECKPOINT_EVENTS = (
    "tool_start",
    "tool_success",
    "tool_failed",
    "agent_handoff",
    "waiting_user",
    "task_completed",
)


def _crewai_installed() -> bool:
    return find_spec("crewai") is not None


def _tool_adapter_status() -> dict[str, Any]:
    from app.services.crew_tools import crew_tool_adapter_status

    return crew_tool_adapter_status()


def _tool_descriptors() -> list[dict[str, Any]]:
    from app.services.crew_tools import build_crewai_tool_descriptors

    return [descriptor.model_dump() for descriptor in build_crewai_tool_descriptors()]


def _runtime_factory_status() -> dict[str, Any]:
    from app.services.crew_runtime import crew_runtime_factory_status

    return crew_runtime_factory_status()


def crew_status() -> dict[str, Any]:
    settings = get_settings()
    installed = _crewai_installed()
    requested = settings.agent_framework.casefold() == "crewai" and settings.crewai_enabled
    return {
        "framework": "crewai",
        "requested": requested,
        "installed": installed,
        "active": requested and installed,
        "fallback": "orchestrator" if not (requested and installed) else "",
        "roles": [role.__dict__ for role in CREW_ROLES],
        "tasks": [task.__dict__ for task in CREW_TASKS],
        "tools": [tool.__dict__ for tool in CREW_TOOLS],
        "tool_adapter": _tool_adapter_status(),
        "tool_descriptors": _tool_descriptors(),
        "runtime_factory": _runtime_factory_status(),
        "memory_isolation": MEMORY_ISOLATION,
        "checkpoint_events": list(CHECKPOINT_EVENTS),
    }


def crew_plan_metadata() -> dict[str, Any]:
    status = crew_status()
    return {
        "framework": "crewai" if status["requested"] else "orchestrator",
        "active": status["active"],
        "fallback": status["fallback"],
        "roles": [role.name for role in CREW_ROLES],
        "task_agents": {task.stage: task.agent for task in CREW_TASKS},
        "tool_registry": {
            tool.key: {
                "owner_agent": tool.owner_agent,
                "scope": tool.scope,
                "mutates_state": tool.mutates_state,
                "requires_user_approval": tool.requires_user_approval,
            }
            for tool in CREW_TOOLS
        },
        "tool_adapter": _tool_adapter_status(),
        "tool_descriptors": _tool_descriptors(),
        "runtime_factory": _runtime_factory_status(),
        "memory_isolation": MEMORY_ISOLATION,
        "checkpoint_events": list(CHECKPOINT_EVENTS),
    }
