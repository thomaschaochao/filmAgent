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


def _crewai_installed() -> bool:
    return find_spec("crewai") is not None


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
    }


def crew_plan_metadata() -> dict[str, Any]:
    status = crew_status()
    return {
        "framework": "crewai" if status["requested"] else "orchestrator",
        "active": status["active"],
        "fallback": status["fallback"],
        "roles": [role.name for role in CREW_ROLES],
        "task_agents": {task.stage: task.agent for task in CREW_TASKS},
    }
