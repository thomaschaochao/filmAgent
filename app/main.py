import hashlib
import json
import re
import time
import unicodedata
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete, func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.config import get_settings
from app.database import Base, engine, get_db
from app.models import (
    AgentMessage,
    AgentRun,
    AgentSession,
    AgentSessionStatus,
    Asset,
    AssetImage,
    ChangeProposal,
    ChatMessage,
    ChatMessageRole,
    ChatScope,
    ChatThread,
    ChatThreadStatus,
    CreativeMemory,
    EmbeddingJob,
    Project,
    ProjectStatus,
    PromptVersion,
    ProposalStatus,
    ResearchSource,
    Scene,
    ScriptChunk,
    ScriptDocument,
    ScriptSummary,
    ScriptVersion,
    Shot,
    StoryboardSnapshot,
    WorkflowPlan,
    WorkflowTask,
    WorkflowTaskStatus,
)
from app.schemas import (
    AgentMessageCreate,
    AgentMetricsRead,
    AgentResearchRequest,
    AgentRunDetailRead,
    AgentSessionCreate,
    AgentSessionRead,
    AssetCreate,
    AssetImageGenerateRequest,
    AssetImageRead,
    AssetRead,
    AssetUpdate,
    ChangeProposalRead,
    ChatMessageCreate,
    ChatThreadCreate,
    ChatThreadDetail,
    ChatThreadRead,
    CrewRuntimePreflightRead,
    CrewStatusRead,
    ImageProviderRead,
    PageFetchRequest,
    ProjectCreate,
    ProjectRead,
    ProjectUpdate,
    PromptGenerateRequest,
    PromptRead,
    ProposalOperation,
    ResearchAdoptRequest,
    ResearchSourceRead,
    RetrievalHit,
    RetrievalQuery,
    RetrievalRebuildRead,
    RetrievalRebuildRequest,
    RetrievalSelfTestRead,
    RetrievalStatusRead,
    SceneRead,
    ScriptCreate,
    ScriptGenerateRequest,
    ScriptIndexRead,
    ScriptRead,
    ShotRead,
    ShotUpdate,
    StoryboardDraft,
    StoryboardSnapshotRead,
    WebSearchRequest,
    WorkflowPlanCreate,
    WorkflowPlanRead,
    WorkflowTaskCheckpointRead,
    WorkflowTaskRead,
)
from app.services.agent_retrieval import get_retrieval_status_snapshot, retrieve_context
from app.services.chat import (
    ChatValidationError,
    generate_chat_draft,
    validate_base_version,
    validate_operation_scope,
    validate_shot_merge_operations,
)
from app.services.checkpoint import (
    build_checkpoint,
    mark_completed,
    mark_failed,
    mark_running,
    write_checkpoint,
)
from app.services.crew import crew_status
from app.services.crew_runner import CrewStageTool, run_stage_tools
from app.services.crew_runtime import crew_runtime_preflight
from app.services.crew_tool_executor import CrewToolExecutionError, execute_crewai_tool
from app.services.deepseek import DeepSeekClient, DeepSeekError
from app.services.image_generation import ImageGenerationError, generate_image
from app.services.orchestrator import continue_conversation, create_plan, index_script
from app.services.prompt_strategy import (
    build_director_overhead_prompt,
    classify_shot_prompt_strategy,
)
from app.services.rag import LocalRAG
from app.services.web_tools import WebToolError, fetch_page, search_web
from app.services.workflow import (
    extract_assets,
    generate_asset_prompt,
    generate_prompt,
    generate_script,
    generate_storyboard,
    improve_storyboard_locally,
    storyboard_frame_count_for_duration,
    validate_storyboard,
)
from app.version import __version__


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    columns = {column["name"] for column in inspect(engine).get_columns("projects")}
    if "world_setting" not in columns:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE projects ADD COLUMN world_setting TEXT NOT NULL DEFAULT ''")
            )
    shot_columns = {column["name"] for column in inspect(engine).get_columns("shots")}
    if "duration_seconds" not in shot_columns:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE shots ADD COLUMN duration_seconds FLOAT NOT NULL DEFAULT 4.0")
            )
    agent_session_columns = {
        column["name"] for column in inspect(engine).get_columns("agent_sessions")
    }
    if "archived_at" not in agent_session_columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE agent_sessions ADD COLUMN archived_at DATETIME"))
    agent_run_columns = {column["name"] for column in inspect(engine).get_columns("agent_runs")}
    agent_run_migrations = {
        "error_type": "VARCHAR(64)",
        "provider": "VARCHAR(32) NOT NULL DEFAULT 'deepseek'",
        "model": "VARCHAR(100)",
        "request_id": "VARCHAR(100)",
        "latency_ms": "INTEGER",
        "attempt_count": "INTEGER NOT NULL DEFAULT 1",
        "input_tokens": "INTEGER",
        "output_tokens": "INTEGER",
        "prompt_version": "VARCHAR(64) NOT NULL DEFAULT 'v1'",
        "rules_version": "VARCHAR(64)",
        "rules_snapshot": "JSON NOT NULL DEFAULT '{}'",
        "system_prompt": "TEXT NOT NULL DEFAULT ''",
        "user_prompt": "TEXT NOT NULL DEFAULT ''",
        "raw_response": "TEXT NOT NULL DEFAULT ''",
    }
    with engine.begin() as connection:
        for name, definition in agent_run_migrations.items():
            if name not in agent_run_columns:
                connection.execute(text(f"ALTER TABLE agent_runs ADD COLUMN {name} {definition}"))
    yield


app = FastAPI(title="FilmPilot API", version=__version__, lifespan=lifespan)
API = "/api/v1"
DbSession = Annotated[Session, Depends(get_db)]
STATIC_DIR = Path(__file__).parent / "static"
STORAGE_DIR = Path(__file__).parent.parent / "storage"
ASSET_STORAGE_DIR = STORAGE_DIR / "assets"
ASSET_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/storage", StaticFiles(directory=STORAGE_DIR), name="storage")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def require_project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def require_script(db: Session, script_id: str) -> ScriptVersion:
    script = db.get(ScriptVersion, script_id)
    if script is None:
        raise HTTPException(status_code=404, detail="Script not found")
    return script


def require_shot(db: Session, shot_id: str) -> Shot:
    shot = db.get(Shot, shot_id)
    if shot is None:
        raise HTTPException(status_code=404, detail="Shot not found")
    return shot


def require_asset(db: Session, asset_id: str) -> Asset:
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    return asset


def require_chat_thread(db: Session, thread_id: str) -> ChatThread:
    thread = db.get(ChatThread, thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Chat thread not found")
    return thread


def require_proposal(db: Session, proposal_id: str) -> ChangeProposal:
    proposal = db.get(ChangeProposal, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="Change proposal not found")
    return proposal


def require_research_source(db: Session, source_id: str) -> ResearchSource:
    source = db.get(ResearchSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Research source not found")
    return source


def _record_agent_run(
    db: Session,
    client: DeepSeekClient,
    *,
    project_id: str,
    operation: str,
    target_type: str,
    target_id: str,
    status: str,
    validation_results: list[dict] | None = None,
    error: DeepSeekError | None = None,
    rules_snapshot: dict | None = None,
) -> AgentRun:
    metadata = getattr(client, "last_call", {}) or {}
    previous = db.scalar(
        select(func.count(AgentRun.id)).where(
            AgentRun.operation == operation,
            AgentRun.target_id == target_id,
        )
    )
    settings = get_settings()
    run = AgentRun(
        project_id=project_id,
        operation=operation,
        target_type=target_type,
        target_id=target_id,
        status=status,
        validation_passed=status == "passed" if status != "request_failed" else None,
        validation_results=(
            validation_results
            if validation_results is not None
            else getattr(error, "validation_results", [])
        ),
        is_regeneration=bool(previous),
        error_message=str(error) if error else None,
        error_type=getattr(error, "error_type", None),
        provider=metadata.get("provider", "deepseek"),
        model=metadata.get("model", settings.deepseek_model),
        request_id=metadata.get("request_id"),
        latency_ms=metadata.get("latency_ms"),
        attempt_count=metadata.get("attempt_count", getattr(client, "attempt_count", 1)),
        input_tokens=metadata.get("input_tokens"),
        output_tokens=metadata.get("output_tokens"),
        prompt_version=f"{operation}-v1",
        rules_version=settings.validation_rules_version,
        rules_snapshot=rules_snapshot or {},
        system_prompt=metadata.get("system_prompt", ""),
        user_prompt=metadata.get("user_prompt", ""),
        raw_response=metadata.get("raw_response", ""),
    )
    db.add(run)
    return run


def _failure_status(error: DeepSeekError) -> str:
    return (
        "validation_failed"
        if error.error_type in {"schema_validation", "output_validation"}
        else "request_failed"
    )


def _deepseek_http_status(error: DeepSeekError) -> int:
    return 422 if _failure_status(error) == "validation_failed" else 502


def _replace_storyboard(db: Session, project: Project, script: ScriptVersion, draft) -> None:
    existing = list(db.scalars(select(Scene).where(Scene.project_id == project.id)).all())
    for scene in existing:
        db.delete(scene)
    db.flush()
    for scene_draft in draft.scenes:
        scene_data = scene_draft.model_dump(exclude={"shots"})
        scene = Scene(project_id=project.id, script_version_id=script.id, **scene_data)
        for shot_draft in scene_draft.shots:
            scene.shots.append(Shot(**shot_draft.model_dump()))
        db.add(scene)


def require_asset_image(db: Session, asset_id: str, image_id: str) -> AssetImage:
    image = db.get(AssetImage, image_id)
    if image is None or image.asset_id != asset_id:
        raise HTTPException(status_code=404, detail="Asset image not found")
    return image


def _storage_file(relative_path: str | None) -> Path | None:
    if not relative_path:
        return None
    path = (STORAGE_DIR / relative_path).resolve()
    return path if path.is_relative_to(STORAGE_DIR.resolve()) else None


def _safe_path_name(value: str, fallback: str) -> str:
    cleaned = unicodedata.normalize("NFKC", value).strip()
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:80].strip(" .") or fallback


def _asset_image_relative_path(asset: Asset, image_id: str, extension: str) -> Path:
    type_folders = {"character": "人物", "location": "场景", "prop": "道具"}
    project_name = _safe_path_name(asset.project.name, "未命名项目")
    asset_name = _safe_path_name(asset.name, "未命名资产")
    project_folder = f"{project_name}-{asset.project_id[:8]}"
    type_folder = type_folders.get(asset.asset_type, "其他")
    filename = f"{asset_name}-{image_id[:8]}{extension}"
    return Path("projects") / project_folder / type_folder / asset_name / filename


def _ensure_legacy_asset_image(db: Session, asset: Asset) -> None:
    if not asset.image_path:
        return
    exists = db.scalar(
        select(AssetImage.id).where(
            AssetImage.asset_id == asset.id,
            AssetImage.image_path == asset.image_path,
        )
    )
    if exists:
        return
    db.add(
        AssetImage(
            asset_id=asset.id,
            source="upload",
            status="ready",
            image_path=asset.image_path,
            is_primary=True,
        )
    )
    db.flush()


def _run_asset_image_generation(image_id: str, bind) -> None:
    task_session = sessionmaker(bind=bind, autoflush=False, expire_on_commit=False)
    with task_session() as db:
        image = db.get(AssetImage, image_id)
        if image is None:
            return
        started = time.perf_counter()
        project_id = image.asset.project_id
        previous = db.scalar(
            select(func.count(AgentRun.id)).where(
                AgentRun.operation == "image_generation",
                AgentRun.target_id == image.asset_id,
            )
        )

        def record_run(status_value: str, *, model: str | None, error: str | None = None) -> None:
            db.add(
                AgentRun(
                    project_id=project_id,
                    operation="image_generation",
                    target_type="asset",
                    target_id=image.asset_id,
                    status=status_value,
                    validation_passed=True if status_value == "passed" else None,
                    is_regeneration=bool(previous),
                    error_message=error,
                    error_type="image_api_error" if error else None,
                    provider=image.provider or "unknown",
                    model=model,
                    latency_ms=round((time.perf_counter() - started) * 1000),
                    attempt_count=1,
                    prompt_version="image-generation-v1",
                    rules_version=get_settings().validation_rules_version,
                    user_prompt=image.prompt_snapshot,
                    raw_response=(
                        json.dumps(
                            {"image_path": image.image_path, "model": model},
                            ensure_ascii=False,
                        )
                        if not error
                        else error
                    ),
                )
            )

        image.status = "generating"
        db.commit()
        try:
            generated, model = generate_image(
                image.provider or "",
                image.prompt_snapshot,
                size=image.size,
                quality=image.quality,
            )
            relative_path = _asset_image_relative_path(image.asset, image.id, generated.extension)
            output_path = STORAGE_DIR / relative_path
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(generated.content)
            image.image_path = relative_path.as_posix()
            image.model = model
            image.status = "ready"
            image.error_message = None
            if not image.asset.image_path:
                db.query(AssetImage).filter(AssetImage.asset_id == image.asset_id).update(
                    {AssetImage.is_primary: False}
                )
                image.is_primary = True
                image.asset.image_path = image.image_path
            record_run("passed", model=model)
            db.commit()
        except (ImageGenerationError, RuntimeError) as exc:
            image.status = "failed"
            image.error_message = str(exc)
            record_run("request_failed", model=image.model, error=str(exc))
            db.commit()
        except Exception:
            image.status = "failed"
            image.error_message = "图片生成发生未知错误，请稍后重试。"
            record_run("request_failed", model=image.model, error="图片生成发生未知错误")
            db.commit()


def _clean_asset_name(asset_type: str, name: str) -> str:
    cleaned = unicodedata.normalize("NFKC", name).strip().lstrip("@").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if asset_type == "character":
        cleaned = re.sub(r"\s*[（(][^）)]*[）)]\s*$", "", cleaned).strip()
    return cleaned


def _asset_identity(asset_type: str, name: str) -> tuple[str, str]:
    normalized = re.sub(r"\s+", "", _clean_asset_name(asset_type, name)).casefold()
    return asset_type, normalized


@app.get(f"{API}/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.get(f"{API}/image-providers", response_model=list[ImageProviderRead])
def list_image_providers() -> list[dict]:
    settings = get_settings()
    return [
        {
            "id": "openai",
            "name": "GPT Image 2",
            "model": settings.openai_image_model,
            "configured": settings.provider_configured("openai"),
        },
        {
            "id": "seedream",
            "name": "Seedream 5.0 Lite",
            "model": settings.seedream_model,
            "configured": settings.provider_configured("seedream"),
        },
    ]


@app.post(f"{API}/projects", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
def create_project(payload: ProjectCreate, db: DbSession) -> Project:
    project = Project(**payload.model_dump())
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


@app.get(f"{API}/projects", response_model=list[ProjectRead])
def list_projects(db: DbSession) -> list[Project]:
    return list(db.scalars(select(Project).order_by(Project.updated_at.desc())).all())


@app.get(f"{API}/projects/{{project_id}}", response_model=ProjectRead)
def get_project(project_id: str, db: DbSession) -> Project:
    return require_project(db, project_id)


@app.patch(f"{API}/projects/{{project_id}}", response_model=ProjectRead)
def update_project(project_id: str, payload: ProjectUpdate, db: DbSession) -> Project:
    project = require_project(db, project_id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(project, key, value)
    db.commit()
    db.refresh(project)
    return project


@app.delete(f"{API}/projects/{{project_id}}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(project_id: str, db: DbSession) -> Response:
    project = require_project(db, project_id)
    if LocalRAG().status()["qdrant_local"]:
        try:
            LocalRAG().delete_project_vectors(project.id)
        except Exception:
            pass
    document_ids = list(
        db.scalars(select(ScriptDocument.id).where(ScriptDocument.project_id == project.id)).all()
    )
    if document_ids:
        db.execute(delete(EmbeddingJob).where(EmbeddingJob.document_id.in_(document_ids)))
        db.execute(delete(ScriptSummary).where(ScriptSummary.document_id.in_(document_ids)))
        db.execute(delete(ScriptChunk).where(ScriptChunk.document_id.in_(document_ids)))
        db.execute(delete(ScriptDocument).where(ScriptDocument.id.in_(document_ids)))
    db.query(AgentSession).filter(AgentSession.project_id == project.id).update(
        {AgentSession.project_id: None}
    )
    db.query(CreativeMemory).filter(CreativeMemory.project_id == project.id).update(
        {CreativeMemory.project_id: None}
    )
    db.delete(project)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _next_script_version(db: Session, project_id: str) -> int:
    latest = db.scalar(
        select(func.max(ScriptVersion.version)).where(ScriptVersion.project_id == project_id)
    )
    return (latest or 0) + 1


@app.post(
    f"{API}/projects/{{project_id}}/scripts",
    response_model=ScriptRead,
    status_code=status.HTTP_201_CREATED,
)
def create_script(project_id: str, payload: ScriptCreate, db: DbSession) -> ScriptVersion:
    project = require_project(db, project_id)
    script = ScriptVersion(
        project_id=project.id,
        version=_next_script_version(db, project.id),
        **payload.model_dump(),
    )
    project.status = ProjectStatus.script_review
    db.add(script)
    db.commit()
    db.refresh(script)
    return script


@app.get(f"{API}/projects/{{project_id}}/scripts", response_model=list[ScriptRead])
def list_scripts(project_id: str, db: DbSession) -> list[ScriptVersion]:
    require_project(db, project_id)
    statement = (
        select(ScriptVersion)
        .where(ScriptVersion.project_id == project_id)
        .order_by(ScriptVersion.version.desc())
    )
    return list(db.scalars(statement).all())


@app.get(f"{API}/scripts/{{script_id}}", response_model=ScriptRead)
def get_script(script_id: str, db: DbSession) -> ScriptVersion:
    return require_script(db, script_id)


@app.post(f"{API}/projects/{{project_id}}/scripts/generate", response_model=ScriptRead)
def create_ai_script(
    project_id: str,
    payload: ScriptGenerateRequest,
    db: DbSession,
) -> ScriptVersion:
    project = require_project(db, project_id)
    client = DeepSeekClient()
    try:
        content = generate_script(
            client,
            brief=payload.brief,
            title=payload.title,
            instructions=payload.instructions,
            language=project.language,
            world_setting=project.world_setting,
        )
    except DeepSeekError as exc:
        _record_agent_run(
            db,
            client,
            project_id=project.id,
            operation="script_generation",
            target_type="project",
            target_id=project.id,
            status=_failure_status(exc),
            error=exc,
        )
        db.commit()
        raise HTTPException(status_code=_deepseek_http_status(exc), detail=str(exc)) from exc
    script = ScriptVersion(
        project_id=project.id,
        version=_next_script_version(db, project.id),
        title=payload.title,
        content=content,
        source_type="ai",
    )
    project.status = ProjectStatus.script_review
    db.add(script)
    _record_agent_run(
        db,
        client,
        project_id=project.id,
        operation="script_generation",
        target_type="project",
        target_id=project.id,
        status="passed",
    )
    db.commit()
    db.refresh(script)
    return script


@app.post(f"{API}/scripts/{{script_id}}/approve", response_model=ScriptRead)
def approve_script(script_id: str, db: DbSession) -> ScriptVersion:
    script = require_script(db, script_id)
    db.query(ScriptVersion).filter(ScriptVersion.project_id == script.project_id).update(
        {ScriptVersion.is_approved: False}
    )
    script.is_approved = True
    script.project.status = ProjectStatus.asset_review
    db.commit()
    db.refresh(script)
    return script


@app.get(f"{API}/projects/{{project_id}}/assets", response_model=list[AssetRead])
def list_assets(project_id: str, db: DbSession) -> list[Asset]:
    require_project(db, project_id)
    statement = (
        select(Asset).where(Asset.project_id == project_id).order_by(Asset.asset_type, Asset.name)
    )
    return list(db.scalars(statement).all())


@app.post(
    f"{API}/projects/{{project_id}}/assets",
    response_model=AssetRead,
    status_code=status.HTTP_201_CREATED,
)
def create_asset(project_id: str, payload: AssetCreate, db: DbSession) -> Asset:
    project = require_project(db, project_id)
    name = _clean_asset_name(payload.asset_type, payload.name)
    identity = _asset_identity(payload.asset_type, name)
    duplicate = next(
        (
            asset
            for asset in project.assets
            if asset.name == name or _asset_identity(asset.asset_type, asset.name) == identity
        ),
        None,
    )
    if duplicate:
        raise HTTPException(status_code=409, detail="An asset with this name already exists")
    asset = Asset(project_id=project.id, **payload.model_dump(exclude={"name"}), name=name)
    db.add(asset)
    db.commit()
    db.refresh(asset)
    return asset


@app.patch(f"{API}/assets/{{asset_id}}", response_model=AssetRead)
def update_asset(asset_id: str, payload: AssetUpdate, db: DbSession) -> Asset:
    asset = require_asset(db, asset_id)
    asset_type = payload.asset_type or asset.asset_type
    name = _clean_asset_name(asset_type, payload.name or asset.name)
    if name != asset.name or asset_type != asset.asset_type:
        identity = _asset_identity(asset_type, name)
        duplicate = next(
            (
                candidate
                for candidate in asset.project.assets
                if candidate.id != asset.id
                and (
                    candidate.name == name
                    or _asset_identity(candidate.asset_type, candidate.name) == identity
                )
            ),
            None,
        )
        if duplicate:
            raise HTTPException(status_code=409, detail="An asset with this name already exists")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(asset, key, value)
    asset.name = name
    db.commit()
    db.refresh(asset)
    return asset


@app.delete(f"{API}/assets/{{asset_id}}", status_code=status.HTTP_204_NO_CONTENT)
def delete_asset(asset_id: str, db: DbSession) -> Response:
    asset = require_asset(db, asset_id)
    paths = {asset.image_path, *(image.image_path for image in asset.images)}
    for relative_path in paths:
        image_path = _storage_file(relative_path)
        if image_path and image_path.exists():
            image_path.unlink()
    db.delete(asset)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post(f"{API}/projects/{{project_id}}/assets/extract", response_model=list[AssetRead])
def extract_project_assets(project_id: str, db: DbSession) -> list[Asset]:
    project = require_project(db, project_id)
    script = db.scalar(
        select(ScriptVersion)
        .where(
            ScriptVersion.project_id == project.id,
            ScriptVersion.is_approved.is_(True),
        )
        .order_by(ScriptVersion.version.desc())
    )
    if script is None:
        raise HTTPException(status_code=409, detail="Approve a script before extracting assets")
    client = DeepSeekClient()
    try:
        draft = extract_assets(client, project, script.content)
    except DeepSeekError as exc:
        _record_agent_run(
            db,
            client,
            project_id=project.id,
            operation="asset_extraction",
            target_type="script",
            target_id=script.id,
            status=_failure_status(exc),
            error=exc,
        )
        db.commit()
        raise HTTPException(status_code=_deepseek_http_status(exc), detail=str(exc)) from exc

    existing: dict[tuple[str, str], Asset] = {}
    exact_names = {asset.name for asset in project.assets}
    for asset in project.assets:
        existing.setdefault(_asset_identity(asset.asset_type, asset.name), asset)
    for item in draft.assets:
        name = _clean_asset_name(item.asset_type, item.name)
        identity = _asset_identity(item.asset_type, name)
        asset = existing.get(identity)
        if asset is None:
            if name in exact_names:
                continue
            asset = Asset(
                project_id=project.id,
                **item.model_dump(exclude={"name"}),
                name=name,
            )
            db.add(asset)
            existing[identity] = asset
            exact_names.add(name)
        elif not asset.description:
            asset.description = item.description
    _record_agent_run(
        db,
        client,
        project_id=project.id,
        operation="asset_extraction",
        target_type="script",
        target_id=script.id,
        status="passed",
    )
    db.commit()
    statement = (
        select(Asset).where(Asset.project_id == project.id).order_by(Asset.asset_type, Asset.name)
    )
    return list(db.scalars(statement).all())


@app.post(f"{API}/assets/{{asset_id}}/prompt/generate", response_model=AssetRead)
def create_asset_prompt(asset_id: str, db: DbSession) -> Asset:
    asset = require_asset(db, asset_id)
    client = DeepSeekClient()
    try:
        asset.prompt = generate_asset_prompt(client, asset.project, asset)
    except DeepSeekError as exc:
        db.rollback()
        _record_agent_run(
            db,
            client,
            project_id=asset.project_id,
            operation="asset_prompt_generation",
            target_type="asset",
            target_id=asset.id,
            status=_failure_status(exc),
            error=exc,
        )
        db.commit()
        raise HTTPException(status_code=_deepseek_http_status(exc), detail=str(exc)) from exc
    _record_agent_run(
        db,
        client,
        project_id=asset.project_id,
        operation="asset_prompt_generation",
        target_type="asset",
        target_id=asset.id,
        status="passed",
    )
    db.commit()
    db.refresh(asset)
    return asset


@app.put(f"{API}/assets/{{asset_id}}/image", response_model=AssetRead)
async def upload_asset_image(asset_id: str, request: Request, db: DbSession) -> Asset:
    asset = require_asset(db, asset_id)
    content_type = request.headers.get("content-type", "").split(";", 1)[0]
    extensions = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }
    extension = extensions.get(content_type)
    if extension is None:
        raise HTTPException(status_code=415, detail="Upload a JPEG, PNG, WebP, or GIF image")
    content = await request.body()
    if not content or len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Image must be between 1 byte and 10 MB")
    _ensure_legacy_asset_image(db, asset)
    db.query(AssetImage).filter(AssetImage.asset_id == asset.id).update(
        {AssetImage.is_primary: False}
    )
    image = AssetImage(
        asset_id=asset.id,
        source="upload",
        status="ready",
        size="original",
        quality="original",
        is_primary=True,
    )
    db.add(image)
    db.flush()
    relative_path = _asset_image_relative_path(asset, image.id, extension)
    output_path = STORAGE_DIR / relative_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(content)
    image.image_path = relative_path.as_posix()
    asset.image_path = relative_path.as_posix()
    db.commit()
    db.refresh(asset)
    return asset


@app.get(f"{API}/assets/{{asset_id}}/images", response_model=list[AssetImageRead])
def list_asset_images(asset_id: str, db: DbSession) -> list[AssetImage]:
    asset = require_asset(db, asset_id)
    _ensure_legacy_asset_image(db, asset)
    db.commit()
    statement = (
        select(AssetImage)
        .where(AssetImage.asset_id == asset.id)
        .order_by(AssetImage.created_at.desc())
    )
    return list(db.scalars(statement).all())


@app.post(
    f"{API}/assets/{{asset_id}}/images/generate",
    response_model=AssetImageRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_asset_image_generation(
    asset_id: str,
    payload: AssetImageGenerateRequest,
    background_tasks: BackgroundTasks,
    db: DbSession,
) -> AssetImage:
    asset = require_asset(db, asset_id)
    if not asset.prompt.strip():
        raise HTTPException(status_code=409, detail="请先生成或填写资产提示词")
    settings = get_settings()
    if not settings.provider_configured(payload.provider):
        raise HTTPException(
            status_code=409,
            detail="该图片供应商尚未配置，请填写 config.local.env 后重启服务",
        )
    model = settings.openai_image_model if payload.provider == "openai" else settings.seedream_model
    image = AssetImage(
        asset_id=asset.id,
        provider=payload.provider,
        model=model,
        prompt_snapshot=asset.prompt.strip(),
        size="1536x1024",
        quality="high",
        status="pending",
    )
    db.add(image)
    db.commit()
    db.refresh(image)
    background_tasks.add_task(_run_asset_image_generation, image.id, db.get_bind())
    return image


@app.patch(
    f"{API}/assets/{{asset_id}}/images/{{image_id}}/primary",
    response_model=AssetRead,
)
def set_primary_asset_image(asset_id: str, image_id: str, db: DbSession) -> Asset:
    asset = require_asset(db, asset_id)
    image = require_asset_image(db, asset_id, image_id)
    if image.status != "ready" or not image.image_path:
        raise HTTPException(status_code=409, detail="只有生成完成的图片才能设为主参考图")
    db.query(AssetImage).filter(AssetImage.asset_id == asset.id).update(
        {AssetImage.is_primary: False}
    )
    image.is_primary = True
    asset.image_path = image.image_path
    db.commit()
    db.refresh(asset)
    return asset


@app.delete(
    f"{API}/assets/{{asset_id}}/images/{{image_id}}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_asset_image(asset_id: str, image_id: str, db: DbSession) -> Response:
    asset = require_asset(db, asset_id)
    image = require_asset_image(db, asset_id, image_id)
    was_primary = image.is_primary or asset.image_path == image.image_path
    image_path = _storage_file(image.image_path)
    db.delete(image)
    db.flush()
    if was_primary:
        replacement = db.scalar(
            select(AssetImage)
            .where(
                AssetImage.asset_id == asset.id,
                AssetImage.id != image_id,
                AssetImage.status == "ready",
                AssetImage.image_path.is_not(None),
            )
            .order_by(AssetImage.created_at.desc())
        )
        asset.image_path = replacement.image_path if replacement else None
        if replacement:
            replacement.is_primary = True
    db.commit()
    if image_path and image_path.exists():
        image_path.unlink()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post(f"{API}/scripts/{{script_id}}/shots/generate", response_model=list[SceneRead])
def create_shots(script_id: str, db: DbSession) -> list[Scene]:
    script = require_script(db, script_id)
    if not script.is_approved:
        raise HTTPException(status_code=409, detail="Approve the script before generating shots")
    project = script.project
    client = DeepSeekClient()
    try:
        draft = generate_storyboard(client, project, script.content)
    except DeepSeekError as exc:
        _record_agent_run(
            db,
            client,
            project_id=project.id,
            operation="storyboard_generation",
            target_type="script",
            target_id=script.id,
            status=_failure_status(exc),
            error=exc,
        )
        db.commit()
        raise HTTPException(status_code=_deepseek_http_status(exc), detail=str(exc)) from exc

    settings = get_settings()
    draft = improve_storyboard_locally(draft, script.content)
    rules_snapshot = {
        "reference_coverage_threshold": settings.storyboard_reference_coverage_threshold,
        "reference_match_threshold": settings.storyboard_reference_match_threshold,
    }
    validation_results = validate_storyboard(
        draft,
        script.content,
        reference_coverage_threshold=settings.storyboard_reference_coverage_threshold,
        reference_match_threshold=settings.storyboard_reference_match_threshold,
    )
    validation_passed = all(item["passed"] for item in validation_results)
    run = _record_agent_run(
        db,
        client,
        project_id=project.id,
        operation="storyboard_generation",
        target_type="script",
        target_id=script.id,
        status="passed" if validation_passed else "validation_failed",
        validation_results=validation_results,
        rules_snapshot=rules_snapshot,
    )
    if not validation_passed:
        failed_labels = [item["label"] for item in validation_results if not item["passed"]]
        run.error_message = "、".join(failed_labels)
        db.commit()
        raise HTTPException(
            status_code=422,
            detail=f"分镜结果未通过校验：{'、'.join(failed_labels)}。旧分镜已保留。",
        )

    latest_snapshot = db.scalar(
        select(func.max(StoryboardSnapshot.version)).where(
            StoryboardSnapshot.project_id == project.id
        )
    )
    db.add(
        StoryboardSnapshot(
            project_id=project.id,
            script_version_id=script.id,
            version=(latest_snapshot or 0) + 1,
            payload=draft.model_dump(mode="json"),
        )
    )
    _replace_storyboard(db, project, script, draft)
    project.status = ProjectStatus.shot_list_review
    db.commit()

    statement = (
        select(Scene)
        .where(Scene.project_id == project.id)
        .options(selectinload(Scene.shots))
        .order_by(Scene.sequence)
    )
    return list(db.scalars(statement).all())


@app.get(
    f"{API}/projects/{{project_id}}/storyboard-snapshots",
    response_model=list[StoryboardSnapshotRead],
)
def list_storyboard_snapshots(project_id: str, db: DbSession) -> list[StoryboardSnapshot]:
    require_project(db, project_id)
    return list(
        db.scalars(
            select(StoryboardSnapshot)
            .where(StoryboardSnapshot.project_id == project_id)
            .order_by(StoryboardSnapshot.version.desc())
        ).all()
    )


@app.post(
    f"{API}/storyboard-snapshots/{{snapshot_id}}/restore",
    response_model=list[SceneRead],
)
def restore_storyboard_snapshot(snapshot_id: str, db: DbSession) -> list[Scene]:
    snapshot = db.get(StoryboardSnapshot, snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Storyboard snapshot not found")
    project = require_project(db, snapshot.project_id)
    script = require_script(db, snapshot.script_version_id)
    draft = StoryboardDraft.model_validate(snapshot.payload)
    try:
        _replace_storyboard(db, project, script, draft)
        project.status = ProjectStatus.shot_list_review
        db.commit()
    except Exception:
        db.rollback()
        raise
    return list(
        db.scalars(
            select(Scene)
            .where(Scene.project_id == project.id)
            .options(selectinload(Scene.shots))
            .order_by(Scene.sequence)
        ).all()
    )


@app.get(
    f"{API}/projects/{{project_id}}/agent-metrics",
    response_model=AgentMetricsRead,
)
def get_agent_metrics(project_id: str, db: DbSession) -> dict:
    require_project(db, project_id)
    runs = list(
        db.scalars(
            select(AgentRun)
            .where(AgentRun.project_id == project_id)
            .order_by(AgentRun.created_at.desc())
        ).all()
    )
    passed_runs = sum(run.status == "passed" for run in runs)
    failed_runs = len(runs) - passed_runs
    validation_totals: dict[str, dict] = {}
    for run in runs:
        for check in run.validation_results or []:
            metric = validation_totals.setdefault(
                check["key"],
                {"key": check["key"], "label": check["label"], "total": 0, "passed": 0},
            )
            metric["total"] += 1
            metric["passed"] += int(check["passed"])
    validations = [
        {
            **metric,
            "pass_rate": round(metric["passed"] / metric["total"] * 100, 1),
        }
        for metric in validation_totals.values()
    ]
    measured_latencies = [run.latency_ms for run in runs if run.latency_ms is not None]
    return {
        "total_runs": len(runs),
        "passed_runs": passed_runs,
        "failed_runs": failed_runs,
        "pass_rate": round(passed_runs / len(runs) * 100, 1) if runs else 0.0,
        "regeneration_count": sum(run.is_regeneration for run in runs),
        "validation_failed_count": sum(run.status == "validation_failed" for run in runs),
        "request_failed_count": sum(run.status == "request_failed" for run in runs),
        "total_input_tokens": sum(run.input_tokens or 0 for run in runs),
        "total_output_tokens": sum(run.output_tokens or 0 for run in runs),
        "average_latency_ms": (
            round(sum(measured_latencies) / len(measured_latencies), 1)
            if measured_latencies
            else 0.0
        ),
        "validations": validations,
        "recent_runs": runs[:20],
    }


@app.get(f"{API}/agent-runs/{{run_id}}", response_model=AgentRunDetailRead)
def get_agent_run_detail(run_id: str, db: DbSession) -> AgentRun:
    run = db.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Agent run not found")
    return run


@app.get(f"{API}/projects/{{project_id}}/scenes", response_model=list[SceneRead])
def list_scenes(project_id: str, db: DbSession) -> list[Scene]:
    require_project(db, project_id)
    statement = (
        select(Scene)
        .where(Scene.project_id == project_id)
        .options(selectinload(Scene.shots))
        .order_by(Scene.sequence)
    )
    return list(db.scalars(statement).all())


@app.patch(f"{API}/shots/{{shot_id}}", response_model=ShotRead)
def update_shot(shot_id: str, payload: ShotUpdate, db: DbSession) -> Shot:
    shot = require_shot(db, shot_id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(shot, key, value)
    db.commit()
    db.refresh(shot)
    return shot


@app.delete(f"{API}/shots/{{shot_id}}", status_code=status.HTTP_204_NO_CONTENT)
def delete_shot(shot_id: str, db: DbSession) -> Response:
    shot = require_shot(db, shot_id)
    scene_id = shot.scene_id
    db.delete(shot)
    db.flush()
    remaining = list(
        db.scalars(select(Shot).where(Shot.scene_id == scene_id).order_by(Shot.sequence)).all()
    )
    # Move through a collision-free temporary range before assigning final values.
    # SQLite can otherwise update rows out of order and violate (scene_id, sequence).
    for temporary_sequence, remaining_shot in enumerate(remaining, start=1):
        remaining_shot.sequence = -temporary_sequence
    db.flush()
    for sequence, remaining_shot in enumerate(remaining, start=1):
        remaining_shot.sequence = sequence
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post(f"{API}/shots/{{shot_id}}/prompts/generate", response_model=PromptRead)
def create_prompt(
    shot_id: str,
    db: DbSession,
    payload: PromptGenerateRequest | None = None,
) -> PromptVersion:
    shot = require_shot(db, shot_id)
    project = shot.scene.project
    request = payload or PromptGenerateRequest()
    prompt_strategy = classify_shot_prompt_strategy(shot)
    director_overhead = build_director_overhead_prompt(shot, prompt_strategy)
    resolved_frame_count = (
        request.frame_count
        if request.frame_count is not None
        else storyboard_frame_count_for_duration(shot.duration_seconds)
    )
    client = DeepSeekClient()
    try:
        draft = generate_prompt(
            client,
            project,
            shot,
            mode=request.mode,
            frame_count=resolved_frame_count,
        )
    except DeepSeekError as exc:
        _record_agent_run(
            db,
            client,
            project_id=project.id,
            operation="shot_prompt_generation",
            target_type="shot",
            target_id=shot.id,
            status=_failure_status(exc),
            error=exc,
        )
        db.commit()
        raise HTTPException(status_code=_deepseek_http_status(exc), detail=str(exc)) from exc
    latest = db.scalar(
        select(func.max(PromptVersion.version)).where(PromptVersion.shot_id == shot.id)
    )
    prompt = PromptVersion(
        shot_id=shot.id,
        version=(latest or 0) + 1,
        positive_prompt=draft.positive_prompt,
        negative_prompt=draft.negative_prompt,
        model_target=draft.model_target,
        prompt_metadata={
            "components": draft.components,
            "asset_references": draft.asset_references,
            "subject_position": draft.subject_position,
            "action_constraints": draft.action_constraints,
            "spatial_constraints": draft.spatial_constraints,
            "camera_strategy": draft.camera_strategy,
            "mode": request.mode,
            "frame_count": resolved_frame_count if request.mode == "storyboard" else None,
            "frame_count_source": (
                "manual" if request.frame_count is not None else "duration_auto"
            ) if request.mode == "storyboard" else None,
            "shot_duration_seconds": shot.duration_seconds,
            "layout": (
                {4: "2x2", 6: "2x3", 9: "3x3"}[resolved_frame_count]
                if request.mode == "storyboard"
                else None
            ),
            "strategy": prompt_strategy.model_dump(),
            "director_overhead": director_overhead,
            "mode_source": (
                "user_selected"
                if request.mode != prompt_strategy.recommended_mode
                else "agent_recommended"
            ),
            "frames": [frame.model_dump() for frame in draft.frames],
        },
    )
    project.status = ProjectStatus.prompt_review
    db.add(prompt)
    _record_agent_run(
        db,
        client,
        project_id=project.id,
        operation="shot_prompt_generation",
        target_type="shot",
        target_id=shot.id,
        status="passed",
    )
    db.commit()
    db.refresh(prompt)
    return prompt


@app.get(f"{API}/shots/{{shot_id}}/prompts", response_model=list[PromptRead])
def list_prompts(shot_id: str, db: DbSession) -> list[PromptVersion]:
    require_shot(db, shot_id)
    statement = (
        select(PromptVersion)
        .where(PromptVersion.shot_id == shot_id)
        .order_by(PromptVersion.version.desc())
    )
    return list(db.scalars(statement).all())


def _chat_target_project_id(db: Session, resource: str, target_id: str) -> str | None:
    if resource == "asset":
        target = db.get(Asset, target_id)
        return target.project_id if target else None
    if resource == "shot":
        target = db.get(Shot, target_id)
        return target.scene.project_id if target else None
    if resource == "script":
        target = db.get(ScriptVersion, target_id)
        return target.project_id if target else None
    if resource == "prompt":
        target = db.get(PromptVersion, target_id)
        return target.shot.scene.project_id if target else None
    return None


def _editable_snapshot(db: Session, resource: str, target_id: str) -> dict:
    target = {
        "asset": lambda: db.get(Asset, target_id),
        "shot": lambda: db.get(Shot, target_id),
        "script": lambda: db.get(ScriptVersion, target_id),
        "prompt": lambda: db.get(PromptVersion, target_id),
    }[resource]()
    if target is None:
        raise HTTPException(status_code=404, detail=f"{resource.title()} not found")
    fields = {
        "asset": ("project_id", "asset_type", "name", "description", "prompt"),
        "shot": (
            "scene_id", "script_reference", "subject", "action", "environment", "shot_size",
            "camera_angle", "camera_motion", "duration_seconds", "emotion", "lighting",
            "dialogue", "narrative_purpose", "continuity", "is_locked", "sequence",
        ),
        "script": ("project_id", "title", "content", "source_type", "version"),
        "prompt": (
            "shot_id", "positive_prompt", "negative_prompt", "model_target", "prompt_metadata",
            "version",
        ),
    }[resource]
    return {field: getattr(target, field) for field in fields}


def _chat_context(db: Session, thread: ChatThread) -> dict:
    project = thread.project
    context = {
        "project": {
            "id": project.id,
            "name": project.name,
            "visual_style": project.visual_style,
            "world_setting": project.world_setting,
        },
        "page": thread.page,
        "scope": thread.scope,
        "target_type": thread.target_type,
        "target_id": thread.target_id,
        "scope_rules": (
            "Only modify the single target_id."
            if thread.scope == ChatScope.object
            else (
                "This is a global page scope. You may propose operations for "
                "multiple listed items."
            )
        ),
    }
    if thread.scope == ChatScope.object:
        context["target"] = _editable_snapshot(db, thread.target_type, thread.target_id)
        return context
    if thread.page == "assets":
        context["items"] = [
            {"id": item.id, **_editable_snapshot(db, "asset", item.id)}
            for item in project.assets
        ]
    elif thread.page == "script":
        context["items"] = [
            {"id": item.id, **_editable_snapshot(db, "script", item.id)}
            for item in project.scripts
        ]
    elif thread.page == "shots":
        context["items"] = [
            {
                "id": shot.id,
                "scene_heading": scene.heading,
                **_editable_snapshot(db, "shot", shot.id),
            }
            for scene in project.scenes
            for shot in scene.shots
        ]
    elif thread.page == "prompts":
        context["items"] = [
            {"id": prompt.id, **_editable_snapshot(db, "prompt", prompt.id)}
            for scene in project.scenes
            for shot in scene.shots
            for prompt in (
                [max(shot.prompts, key=lambda item: item.version)]
                if shot.prompts
                else []
            )
        ]
    return context


@app.post(
    f"{API}/projects/{{project_id}}/chat/threads",
    response_model=ChatThreadRead,
    status_code=status.HTTP_201_CREATED,
)
def create_chat_thread(project_id: str, payload: ChatThreadCreate, db: DbSession) -> ChatThread:
    require_project(db, project_id)
    if payload.scope == ChatScope.object:
        target_project_id = _chat_target_project_id(db, payload.target_type, payload.target_id)
        if target_project_id is None:
            raise HTTPException(status_code=404, detail="Chat target not found")
        if target_project_id != project_id:
            raise HTTPException(status_code=403, detail="Chat target belongs to another project")
    thread = ChatThread(project_id=project_id, **payload.model_dump())
    db.add(thread)
    db.commit()
    db.refresh(thread)
    return thread


@app.get(f"{API}/projects/{{project_id}}/chat/threads", response_model=list[ChatThreadRead])
def list_chat_threads(
    project_id: str,
    db: DbSession,
    page: str | None = None,
    scope: ChatScope | None = None,
    target_id: str | None = None,
    thread_status: ChatThreadStatus | None = None,
) -> list[ChatThread]:
    require_project(db, project_id)
    statement = select(ChatThread).where(ChatThread.project_id == project_id)
    if page:
        statement = statement.where(ChatThread.page == page)
    if scope:
        statement = statement.where(ChatThread.scope == scope)
    if target_id:
        statement = statement.where(ChatThread.target_id == target_id)
    if thread_status:
        statement = statement.where(ChatThread.status == thread_status)
    return list(db.scalars(statement.order_by(ChatThread.updated_at.desc())).all())


@app.get(f"{API}/chat/threads/{{thread_id}}", response_model=ChatThreadDetail)
def get_chat_thread(thread_id: str, db: DbSession) -> ChatThread:
    statement = (
        select(ChatThread)
        .where(ChatThread.id == thread_id)
        .options(selectinload(ChatThread.messages), selectinload(ChatThread.proposals))
    )
    thread = db.scalar(statement)
    if thread is None:
        raise HTTPException(status_code=404, detail="Chat thread not found")
    return thread


@app.post(f"{API}/chat/threads/{{thread_id}}/messages", response_model=ChatThreadDetail)
def create_chat_message(thread_id: str, payload: ChatMessageCreate, db: DbSession) -> ChatThread:
    thread = require_chat_thread(db, thread_id)
    proposal_draft = payload.proposal
    assistant_content = "已生成结构化修改提案。" if proposal_draft else ""
    if proposal_draft is None:
        history = [
            {"role": message.role, "content": message.content}
            for message in thread.messages[-10:]
        ]
        client = DeepSeekClient()
        try:
            draft = generate_chat_draft(
                client,
                context=_chat_context(db, thread),
                history=history,
                instruction=payload.content,
            )
        except (DeepSeekError, RuntimeError) as exc:
            error = (
                exc
                if isinstance(exc, DeepSeekError)
                else DeepSeekError(str(exc), error_type="chat_generation_error")
            )
            _record_agent_run(
                db,
                client,
                project_id=thread.project_id,
                operation="chat_edit_generation",
                target_type="chat_thread",
                target_id=thread.id,
                status=_failure_status(error),
                error=error,
            )
            db.commit()
            raise HTTPException(
                status_code=_deepseek_http_status(error), detail=str(error)
            ) from exc
        _record_agent_run(
            db,
            client,
            project_id=thread.project_id,
            operation="chat_edit_generation",
            target_type="chat_thread",
            target_id=thread.id,
            status="passed",
        )
        proposal_draft = draft.proposal
        assistant_content = draft.reply
    user_message = ChatMessage(
        thread_id=thread.id,
        role=ChatMessageRole.user,
        content=payload.content,
        context_summary={"page": thread.page, "scope": thread.scope, "target_id": thread.target_id},
    )
    db.add(user_message)
    db.flush()
    assistant = ChatMessage(
        thread_id=thread.id,
        role=ChatMessageRole.assistant,
        content=assistant_content,
        context_summary={"deterministic": payload.proposal is not None},
    )
    db.add(assistant)
    db.flush()
    if proposal_draft:
        before: dict[str, dict] = {}
        base: dict[str, dict] = {}
        for operation in proposal_draft.operations:
            try:
                validate_operation_scope(thread, operation)
            except ChatValidationError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            if operation.target_id:
                target_project_id = _chat_target_project_id(
                    db, operation.resource, operation.target_id
                )
                if target_project_id != thread.project_id:
                    raise HTTPException(
                        status_code=403,
                        detail="Proposal target is outside the project",
                    )
                snapshot = _editable_snapshot(db, operation.resource, operation.target_id)
                key = f"{operation.resource}:{operation.target_id}"
                before[key] = snapshot
                base[key] = snapshot
        try:
            validate_shot_merge_operations(proposal_draft.operations, before)
        except ChatValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        db.add(
            ChangeProposal(
                thread_id=thread.id,
                trigger_message_id=assistant.id,
                target_scope={
                    "page": thread.page,
                    "scope": thread.scope,
                    "target_type": thread.target_type,
                    "target_id": thread.target_id,
                },
                operations=[operation.model_dump() for operation in proposal_draft.operations],
                before_snapshot=before,
                after_preview={"summary": proposal_draft.summary},
                base_version=base,
            )
        )
    thread.updated_at = datetime.now(UTC)
    db.commit()
    return get_chat_thread(thread.id, db)


@app.post(f"{API}/chat/proposals/{{proposal_id}}/reject", response_model=ChangeProposalRead)
def reject_chat_proposal(proposal_id: str, db: DbSession) -> ChangeProposal:
    proposal = require_proposal(db, proposal_id)
    if proposal.status != ProposalStatus.draft:
        raise HTTPException(status_code=409, detail="Only draft proposals can be rejected")
    proposal.status = ProposalStatus.rejected
    db.commit()
    db.refresh(proposal)
    return proposal


@app.post(f"{API}/chat/proposals/{{proposal_id}}/apply", response_model=ChangeProposalRead)
def apply_chat_proposal(proposal_id: str, db: DbSession) -> ChangeProposal:
    proposal = require_proposal(db, proposal_id)
    if proposal.status != ProposalStatus.draft:
        raise HTTPException(status_code=409, detail="Proposal has already been handled")
    thread = proposal.thread
    proposal.status = ProposalStatus.applying
    try:
        after: dict[str, dict] = {}
        results: list[dict] = []
        before_snapshots = dict(proposal.before_snapshot)
        model_by_resource = {"asset": Asset, "shot": Shot}
        for raw_operation in proposal.operations:
            operation = ProposalOperation.model_validate(raw_operation)
            validate_operation_scope(thread, operation)
            key = f"{operation.resource}:{operation.target_id}" if operation.target_id else None
            if operation.action in {"update", "delete", "reorder", "create_version"}:
                current = _editable_snapshot(db, operation.resource, operation.target_id)
                validate_base_version(proposal.base_version[key], current)
            if operation.action == "update" and operation.resource in model_by_resource:
                target = db.get(model_by_resource[operation.resource], operation.target_id)
                if isinstance(target, Shot) and target.is_locked:
                    raise ChatValidationError("Locked shots cannot be changed by AI")
                protected = {"project_id", "scene_id", "sequence"}
                unknown = set(operation.values) - (set(current) - protected)
                if unknown:
                    raise ChatValidationError(
                        f"Unknown editable fields: {', '.join(sorted(unknown))}"
                    )
                for field, value in operation.values.items():
                    setattr(target, field, value)
                db.flush()
                after[key] = _editable_snapshot(db, operation.resource, operation.target_id)
                results.append(
                    {
                        "action": "update",
                        "resource": operation.resource,
                        "id": operation.target_id,
                    }
                )
            elif operation.action == "create" and operation.resource == "asset":
                allowed = {"asset_type", "name", "description", "prompt"}
                has_required = {"asset_type", "name"} <= set(operation.values)
                if not has_required or set(operation.values) - allowed:
                    raise ChatValidationError("Asset creation requires valid type and name")
                target = Asset(project_id=thread.project_id, **operation.values)
                db.add(target)
                db.flush()
                created_key = f"asset:{target.id}"
                after[created_key] = _editable_snapshot(db, "asset", target.id)
                results.append({"action": "create", "resource": "asset", "id": target.id})
            elif operation.action == "create" and operation.resource == "shot":
                scene_id = operation.values.get("scene_id")
                scene = db.get(Scene, scene_id)
                if scene is None or scene.project_id != thread.project_id:
                    raise ChatValidationError("Shot scene is outside the project")
                values = {**operation.values}
                values.pop("scene_id", None)
                allowed = {
                    "sequence", "script_reference", "subject", "action", "environment",
                    "shot_size", "camera_angle", "camera_motion", "duration_seconds",
                    "emotion", "lighting", "dialogue", "narrative_purpose", "continuity",
                }
                required = {"subject", "action", "environment"}
                if set(values) - allowed or not required <= set(values):
                    raise ChatValidationError("Shot creation contains invalid or missing fields")
                values.setdefault(
                    "sequence",
                    (
                        db.scalar(
                            select(func.max(Shot.sequence)).where(Shot.scene_id == scene.id)
                        )
                        or 0
                    ) + 1,
                )
                target = Shot(scene_id=scene.id, **values)
                db.add(target)
                db.flush()
                created_key = f"shot:{target.id}"
                after[created_key] = _editable_snapshot(db, "shot", target.id)
                results.append({"action": "create", "resource": "shot", "id": target.id})
            elif operation.action == "delete" and operation.resource in model_by_resource:
                target = db.get(model_by_resource[operation.resource], operation.target_id)
                if isinstance(target, Shot) and target.is_locked:
                    raise ChatValidationError("Locked shots cannot be deleted by AI")
                if isinstance(target, Shot) and target.prompts:
                    raise ChatValidationError("Shots with prompt history cannot be safely deleted")
                if isinstance(target, Asset) and target.images:
                    raise ChatValidationError("Assets with image history cannot be safely deleted")
                db.delete(target)
                db.flush()
                after[key] = {"deleted": True}
                results.append(
                    {
                        "action": "delete",
                        "resource": operation.resource,
                        "id": operation.target_id,
                    }
                )
            elif operation.action == "reorder" and operation.resource == "shot":
                target = db.get(Shot, operation.target_id)
                if target.is_locked:
                    raise ChatValidationError("Locked shots cannot be reordered by AI")
                new_sequence = operation.values.get("sequence")
                if not isinstance(new_sequence, int) or new_sequence < 1:
                    raise ChatValidationError("Shot reorder requires a positive sequence")
                other = db.scalar(
                    select(Shot).where(
                        Shot.scene_id == target.scene_id,
                        Shot.sequence == new_sequence,
                        Shot.id != target.id,
                    )
                )
                old_sequence = target.sequence
                other_id = other.id if other else None
                if other:
                    other_key = f"shot:{other.id}"
                    before_snapshots[other_key] = _editable_snapshot(db, "shot", other.id)
                target.sequence = -1
                db.flush()
                if other:
                    other.sequence = old_sequence
                target.sequence = new_sequence
                db.flush()
                after[key] = _editable_snapshot(db, "shot", target.id)
                if other:
                    after[other_key] = _editable_snapshot(db, "shot", other.id)
                results.append(
                    {
                        "action": "reorder",
                        "resource": "shot",
                        "id": target.id,
                        "other_id": other_id,
                    }
                )
            elif operation.action == "create_version" and operation.resource == "script":
                source = db.get(ScriptVersion, operation.target_id)
                allowed = {"title", "content"}
                if set(operation.values) - allowed or not operation.values.get("content"):
                    raise ChatValidationError("Script versions require valid title/content values")
                latest = db.scalar(
                    select(func.max(ScriptVersion.version)).where(
                        ScriptVersion.project_id == source.project_id
                    )
                )
                target = ScriptVersion(
                    project_id=source.project_id,
                    version=(latest or 0) + 1,
                    title=operation.values.get("title", source.title),
                    content=operation.values["content"],
                    source_type="ai_chat",
                )
                db.add(target)
                db.flush()
                created_key = f"script:{target.id}"
                after[created_key] = _editable_snapshot(db, "script", target.id)
                results.append({"action": "create_version", "resource": "script", "id": target.id})
            elif operation.action == "create_version" and operation.resource == "prompt":
                source = db.get(PromptVersion, operation.target_id)
                if source.shot.is_locked:
                    raise ChatValidationError("Locked shots cannot receive AI prompt versions")
                allowed = {"positive_prompt", "negative_prompt", "model_target", "prompt_metadata"}
                if set(operation.values) - allowed or not operation.values.get("positive_prompt"):
                    raise ChatValidationError("Prompt versions require a positive prompt")
                latest = db.scalar(
                    select(func.max(PromptVersion.version)).where(
                        PromptVersion.shot_id == source.shot_id
                    )
                )
                target = PromptVersion(
                    shot_id=source.shot_id,
                    version=(latest or 0) + 1,
                    positive_prompt=operation.values["positive_prompt"],
                    negative_prompt=operation.values.get("negative_prompt", source.negative_prompt),
                    model_target=operation.values.get("model_target", source.model_target),
                    prompt_metadata=operation.values.get("prompt_metadata", source.prompt_metadata),
                )
                db.add(target)
                db.flush()
                created_key = f"prompt:{target.id}"
                after[created_key] = _editable_snapshot(db, "prompt", target.id)
                results.append({"action": "create_version", "resource": "prompt", "id": target.id})
            else:
                raise ChatValidationError("Unsupported proposal operation")
        proposal.before_snapshot = before_snapshots
        proposal.after_preview = {
            **proposal.after_preview,
            "snapshots": after,
            "results": results,
        }
        proposal.status = ProposalStatus.applied
        proposal.applied_at = datetime.now(UTC)
        db.commit()
    except (ChatValidationError, KeyError, TypeError, ValueError) as exc:
        db.rollback()
        proposal = require_proposal(db, proposal_id)
        proposal.status = ProposalStatus.failed
        proposal.error_message = str(exc)
        db.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        proposal = require_proposal(db, proposal_id)
        proposal.status = ProposalStatus.failed
        proposal.error_message = "Proposal violates a data uniqueness constraint"
        db.commit()
        raise HTTPException(status_code=409, detail=proposal.error_message) from exc
    db.refresh(proposal)
    return proposal


@app.post(f"{API}/chat/proposals/{{proposal_id}}/revert", response_model=ChangeProposalRead)
def revert_chat_proposal(proposal_id: str, db: DbSession) -> ChangeProposal:
    proposal = require_proposal(db, proposal_id)
    if proposal.status != ProposalStatus.applied:
        raise HTTPException(status_code=409, detail="Only applied proposals can be reverted")
    latest = db.scalar(
        select(ChangeProposal)
        .where(
            ChangeProposal.thread_id == proposal.thread_id,
            ChangeProposal.status == ProposalStatus.applied,
        )
        .order_by(ChangeProposal.applied_at.desc())
    )
    if latest is None or latest.id != proposal.id:
        raise HTTPException(
            status_code=409,
            detail="Only the latest applied proposal can be reverted",
        )
    try:
        after = proposal.after_preview.get("snapshots", {})
        results = proposal.after_preview.get("results", [])
        model_by_resource = {"asset": Asset, "shot": Shot}
        for result in reversed(results):
            resource = result["resource"]
            target_id = result["id"]
            key = f"{resource}:{target_id}"
            action = result["action"]
            if action in {"create", "create_version"}:
                validate_base_version(after[key], _editable_snapshot(db, resource, target_id))
                model = {
                    "asset": Asset,
                    "shot": Shot,
                    "script": ScriptVersion,
                    "prompt": PromptVersion,
                }[resource]
                db.delete(db.get(model, target_id))
                db.flush()
                continue
            before_snapshot = proposal.before_snapshot[key]
            if action == "delete":
                if _chat_target_project_id(db, resource, target_id) is not None:
                    raise ChatValidationError("Deleted object ID is already in use")
                target = model_by_resource[resource](id=target_id, **before_snapshot)
                db.add(target)
                db.flush()
                continue
            validate_base_version(after[key], _editable_snapshot(db, resource, target_id))
            target = db.get(model_by_resource[resource], target_id)
            if action == "reorder":
                other_id = result.get("other_id")
                if other_id:
                    other_key = f"shot:{other_id}"
                    validate_base_version(
                        after[other_key], _editable_snapshot(db, "shot", other_id)
                    )
                    other = db.get(Shot, other_id)
                    other.sequence = -2
                target.sequence = -1
                db.flush()
                if other_id:
                    for field, value in proposal.before_snapshot[other_key].items():
                        if field not in {"scene_id"}:
                            setattr(other, field, value)
            for field, value in before_snapshot.items():
                if field not in {"project_id", "scene_id"}:
                    setattr(target, field, value)
            db.flush()
        proposal.status = ProposalStatus.reverted
        proposal.reverted_at = datetime.now(UTC)
        db.commit()
    except (ChatValidationError, KeyError) as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=f"Cannot safely revert: {exc}") from exc
    db.refresh(proposal)
    return proposal


def _require_agent_session(db: Session, session_id: str) -> AgentSession:
    session = db.scalar(
        select(AgentSession)
        .where(AgentSession.id == session_id)
        .options(
            selectinload(AgentSession.messages),
            selectinload(AgentSession.memories),
            selectinload(AgentSession.plans).selectinload(WorkflowPlan.tasks),
        )
        .execution_options(populate_existing=True)
    )
    if session is None:
        raise HTTPException(status_code=404, detail="Agent session not found")
    return session


def _require_workflow_plan(db: Session, plan_id: str) -> WorkflowPlan:
    plan = db.scalar(
        select(WorkflowPlan)
        .where(WorkflowPlan.id == plan_id)
        .options(selectinload(WorkflowPlan.tasks), selectinload(WorkflowPlan.session))
    )
    if plan is None:
        raise HTTPException(status_code=404, detail="Workflow plan not found")
    return plan


def _require_workflow_task(db: Session, task_id: str) -> WorkflowTask:
    task = db.get(WorkflowTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Workflow task not found")
    return task


def _agent_stage_option(label: str, plan_id: str, stage: str, description: str) -> dict:
    return {
        "label": label,
        "action": "approve_stage",
        "plan_id": plan_id,
        "stage": stage,
        "description": description,
    }


def _agent_defer_option() -> dict:
    return {
        "label": "稍后执行",
        "action": "defer_plan",
        "description": "保留当前进度，之后可回到这个对话继续执行。",
    }


def _append_agent_assistant_message(
    db: Session,
    session: AgentSession,
    content: str,
    *,
    option_mode: str,
    reply_options: list[dict] | None = None,
    extra_metadata: dict | None = None,
) -> None:
    metadata = {
        "option_mode": option_mode,
        "reply_options": reply_options or [],
        **(extra_metadata or {}),
    }
    db.add(
        AgentMessage(
            session_id=session.id,
            role=ChatMessageRole.assistant,
            content=content,
            metadata_json=metadata,
        )
    )


def _append_project_created_message(db: Session, plan: WorkflowPlan, project: Project) -> None:
    _append_agent_assistant_message(
        db,
        plan.session,
        (
            f"项目《{project.name}》和第一版剧本已创建。"
            "接下来可以执行资产提取与资产提示词生成，我会从剧本里整理人物、场景和道具。"
        ),
        option_mode="stage_approval",
        reply_options=[
            _agent_stage_option(
                "执行资产提取与提示词",
                plan.id,
                "assets",
                "提取人物、场景和道具，并为每个资产生成提示词。",
            ),
            _agent_defer_option(),
        ],
        extra_metadata={"plan_id": plan.id, "stage": "assets"},
    )


def _append_stage_completed_message(db: Session, plan: WorkflowPlan, task: WorkflowTask) -> None:
    result = task.result_data or {}
    if task.stage == "assets":
        asset_count = result.get("asset_count", 0)
        content = (
            f"资产提取与资产提示词已完成，共整理出 {asset_count} 个资产。"
            "你可以点击页面顶部工作流里的“资产管理”查看、编辑、上传参考图或生成图片。"
            "要继续拆分分镜吗？"
        )
        options = [
            _agent_stage_option(
                "继续拆分分镜",
                plan.id,
                "shots",
                "根据当前剧本生成场景与镜头列表。",
            ),
            _agent_defer_option(),
        ]
        next_stage = "shots"
    elif task.stage == "shots":
        scene_count = result.get("scene_count", 0)
        content = (
            f"分镜拆分已完成，共生成 {scene_count} 个场景。"
            "你可以点击顶部“分镜设计”查看并调整镜头。"
            "要继续生成镜头首帧/故事板提示词吗？"
        )
        options = [
            _agent_stage_option(
                "生成镜头提示词",
                plan.id,
                "prompts",
                "按每个镜头动作复杂度自动选择首帧或故事板提示词。",
            ),
            _agent_defer_option(),
        ]
        next_stage = "prompts"
    elif task.stage == "prompts":
        prompt_count = result.get("prompt_count", 0)
        content = (
            f"镜头提示词已生成，共 {prompt_count} 条。"
            "你可以点击顶部“镜头提示词”查看、复制和重新生成。"
            "如果需要，下一步可以再规划图片生成批次。"
        )
        options = [
            _agent_stage_option(
                "提议图片生成批次",
                plan.id,
                "images",
                "先给出可执行的资产图或镜头图生成批次建议。",
            ),
            _agent_defer_option(),
        ]
        next_stage = "images"
    elif task.stage == "images":
        content = "图片生成批次建议已准备好。本阶段到这里结束，你可以继续在各工作流页面检查结果。"
        options = []
        next_stage = "completed"
    else:
        return
    _append_agent_assistant_message(
        db,
        plan.session,
        content,
        option_mode="stage_approval" if options else "stage_completed",
        reply_options=options,
        extra_metadata={"plan_id": plan.id, "stage": next_stage},
    )


@app.post(
    f"{API}/agent/sessions",
    response_model=AgentSessionRead,
    status_code=status.HTTP_201_CREATED,
)
def create_agent_session(payload: AgentSessionCreate, db: DbSession) -> AgentSession:
    session = AgentSession(
        title=payload.title,
        original_input=payload.initial_input,
    )
    db.add(session)
    db.commit()
    if payload.initial_input.strip():
        continue_conversation(db, session, payload.initial_input.strip(), {})
    return _require_agent_session(db, session.id)


@app.get(f"{API}/agent/sessions", response_model=list[AgentSessionRead])
def list_agent_sessions(
    db: DbSession,
    archived: bool = Query(False),
    include_archived: bool = Query(False),
) -> list[AgentSession]:
    statement = select(AgentSession.id)
    if not include_archived:
        statement = statement.where(
            AgentSession.archived_at.is_not(None)
            if archived
            else AgentSession.archived_at.is_(None)
        )
    ids = list(db.scalars(statement.order_by(AgentSession.updated_at.desc()).limit(30)).all())
    return [_require_agent_session(db, session_id) for session_id in ids]


@app.get(f"{API}/agent/crew/status", response_model=CrewStatusRead)
def get_agent_crew_status() -> dict:
    return crew_status()


@app.post(f"{API}/agent/crew/preflight", response_model=CrewRuntimePreflightRead)
def preflight_agent_crew_runtime() -> dict:
    return crew_runtime_preflight()


@app.post(f"{API}/agent/crew/tools/{{tool_name}}/execute")
def execute_agent_crew_tool(tool_name: str, payload: dict[str, Any], db: DbSession) -> dict:
    try:
        result = execute_crewai_tool(tool_name, db=db, **payload)
    except CrewToolExecutionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"tool": tool_name, "result": result}


@app.get(f"{API}/agent/sessions/{{session_id}}", response_model=AgentSessionRead)
def get_agent_session(session_id: str, db: DbSession) -> AgentSession:
    return _require_agent_session(db, session_id)


@app.post(f"{API}/agent/sessions/{{session_id}}/research", response_model=AgentSessionRead)
async def research_agent_session(
    session_id: str, payload: AgentResearchRequest, db: DbSession
) -> AgentSession:
    session = _require_agent_session(db, session_id)
    db.add(
        AgentMessage(
            session_id=session.id,
            role=ChatMessageRole.user,
            content=f"请联网搜索资料：{payload.query}",
        )
    )
    try:
        result = await search_web(payload.query)
    except (WebToolError, RuntimeError) as exc:
        _append_agent_assistant_message(
            db,
            session,
            f"联网搜索暂时不可用：{exc}",
            option_mode="research_failed",
            reply_options=[
                {
                    "label": "先不用联网，按剧本继续",
                    "content": "先不用联网，按当前剧本和已确认设定继续。",
                    "facts": {},
                    "description": "跳过外部资料，不影响后续制作流程。",
                }
            ],
        )
        db.commit()
        return _require_agent_session(db, session.id)

    persisted_sources = []
    for source in result.get("sources", []):
        url = source.get("url")
        if not url:
            continue
        research = ResearchSource(
            session_id=session.id,
            query=payload.query,
            title=source.get("title", url),
            url=url,
            summary=result.get("summary", "")[:4000],
            fetch_method=result.get("provider", "web_search"),
        )
        db.add(research)
        db.flush()
        persisted_sources.append(
            {"id": research.id, "title": research.title, "url": research.url}
        )
    source_lines = "\n".join(
        f"- {item['title']}: {item['url']}" for item in persisted_sources[:5]
    )
    content = (
        "联网搜索完成。下面是可用于创作参考的摘要：\n\n"
        f"{result.get('summary', '').strip() or '搜索未返回可用摘要。'}"
    )
    if source_lines:
        content += f"\n\n来源：\n{source_lines}"
    _append_agent_assistant_message(
        db,
        session,
        content,
        option_mode="research_result",
        reply_options=[
            {
                "label": "采用这些资料继续",
                "content": "采用这些联网资料作为创作参考，继续完善制作计划。",
                "facts": {},
                "description": "仅将摘要作为当前对话参考；需要入库时再单独采纳来源。",
            },
            {
                "label": "先不用联网，按剧本继续",
                "content": "先不用联网，按当前剧本和已确认设定继续。",
                "facts": {},
                "description": "不采用这次搜索结果。",
            },
        ],
        extra_metadata={
            "query": payload.query,
            "sources": persisted_sources,
            "provider": result.get("provider"),
        },
    )
    db.commit()
    return _require_agent_session(db, session.id)


@app.post(f"{API}/agent/sessions/{{session_id}}/archive", response_model=AgentSessionRead)
def archive_agent_session(session_id: str, db: DbSession) -> AgentSession:
    session = _require_agent_session(db, session_id)
    session.archived_at = datetime.now(UTC)
    db.commit()
    return _require_agent_session(db, session.id)


@app.post(f"{API}/agent/sessions/{{session_id}}/unarchive", response_model=AgentSessionRead)
def unarchive_agent_session(session_id: str, db: DbSession) -> AgentSession:
    session = _require_agent_session(db, session_id)
    session.archived_at = None
    db.commit()
    return _require_agent_session(db, session.id)


@app.delete(f"{API}/agent/sessions/{{session_id}}", status_code=status.HTTP_204_NO_CONTENT)
def delete_agent_session(session_id: str, db: DbSession) -> Response:
    session = _require_agent_session(db, session_id)
    db.delete(session)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post(f"{API}/agent/sessions/{{session_id}}/messages", response_model=AgentSessionRead)
def send_agent_message(
    session_id: str, payload: AgentMessageCreate, db: DbSession
) -> AgentSession:
    session = _require_agent_session(db, session_id)
    if session.status in {AgentSessionStatus.cancelled, AgentSessionStatus.completed}:
        raise HTTPException(status_code=409, detail="This agent session is no longer active")
    continue_conversation(db, session, payload.content, payload.facts)
    return _require_agent_session(db, session.id)


@app.get(
    f"{API}/agent/sessions/{{session_id}}/memory",
    response_model=list[dict],
)
def list_agent_memory(session_id: str, db: DbSession) -> list[dict]:
    session = _require_agent_session(db, session_id)
    return [
        {
            "id": item.id,
            "category": item.category,
            "key": item.key,
            "value": item.value,
            "status": item.status,
            "source_type": item.source_type,
        }
        for item in session.memories
    ]


@app.post(
    f"{API}/agent/sessions/{{session_id}}/plan",
    response_model=WorkflowPlanRead,
    status_code=status.HTTP_201_CREATED,
)
def generate_agent_plan(
    session_id: str, payload: WorkflowPlanCreate, db: DbSession
) -> WorkflowPlan:
    session = _require_agent_session(db, session_id)
    return create_plan(db, session, payload.project_spec, payload.assumptions)


@app.post(f"{API}/agent/plans/{{plan_id}}/approve", response_model=AgentSessionRead)
def approve_agent_plan(
    plan_id: str, background_tasks: BackgroundTasks, db: DbSession
) -> AgentSession:
    plan = _require_workflow_plan(db, plan_id)
    session = plan.session
    if plan.missing_information:
        raise HTTPException(status_code=409, detail="Resolve missing information before approval")
    if session.project_id:
        return _require_agent_session(db, session.id)
    spec = plan.project_spec
    project = Project(
        name=spec["name"],
        description=spec.get("description", ""),
        visual_style=spec.get("visual_style", "电影感写实"),
        world_setting=spec.get("world_setting", ""),
        aspect_ratio=spec.get("aspect_ratio", "16:9"),
        language=spec.get("language", "zh-CN"),
        status=ProjectStatus.script_review,
    )
    db.add(project)
    db.flush()
    script = ScriptVersion(
        project_id=project.id,
        version=1,
        title=spec.get("script_title") or project.name,
        content=spec.get("script_content") or session.original_input,
        source_type="user",
    )
    db.add(script)
    session.project_id = project.id
    session.status = AgentSessionStatus.awaiting_stage_approval
    session.current_stage = "assets"
    plan.status = "approved"
    plan.approved_at = datetime.now(UTC)
    first_task = next(task for task in plan.tasks if task.stage == "project_script")
    first_task.status = WorkflowTaskStatus.completed
    first_task.result_data = {"project_id": project.id, "script_id": script.id}
    _append_project_created_message(db, plan, project)
    db.commit()
    document, embedding_job = index_script(db, script)
    if not LocalRAG().status()["available"]:
        embedding_job.status = "keyword_ready"
        document.status = "ready"
        _mark_document_current(db, document)
        db.commit()
    elif embedding_job.status in {"pending", "failed"}:
        background_tasks.add_task(_run_embedding_job, embedding_job.id, db.get_bind())
    return _require_agent_session(db, session.id)


@app.post(
    f"{API}/agent/plans/{{plan_id}}/stages/{{stage_name}}/approve",
    response_model=WorkflowTaskRead,
)
def approve_agent_stage(plan_id: str, stage_name: str, db: DbSession) -> WorkflowTask:
    plan = _require_workflow_plan(db, plan_id)
    task = next((item for item in plan.tasks if item.stage == stage_name), None)
    if task is None:
        raise HTTPException(status_code=404, detail="Workflow stage not found")
    if task.status == WorkflowTaskStatus.completed:
        return task
    if not plan.session.project_id:
        raise HTTPException(status_code=409, detail="Approve the project plan first")
    project = require_project(db, plan.session.project_id)
    script = db.scalar(
        select(ScriptVersion)
        .where(ScriptVersion.project_id == project.id)
        .order_by(ScriptVersion.version.desc())
    )
    if script is None:
        raise HTTPException(status_code=409, detail="The project does not have a script")
    mark_running(
        task,
        input_snapshot={
            "project_id": project.id,
            "script_id": script.id,
            "stage": stage_name,
        },
    )
    db.commit()
    try:
        if stage_name == "assets":
            def extract_and_prompt_assets() -> dict:
                assets = extract_project_assets(project.id, db)
                for asset in assets:
                    create_asset_prompt(asset.id, db)
                return {"asset_count": len(assets)}

            results = run_stage_tools(
                task,
                db,
                [
                    CrewStageTool("approve_script", lambda: approve_script(script.id, db)),
                    CrewStageTool("extract_assets", extract_and_prompt_assets),
                ],
            )
            task.result_data = {
                **(task.result_data or {}),
                "asset_count": results["extract_assets"]["asset_count"],
            }
            next_stage = "shots"
        elif stage_name == "shots":
            results = run_stage_tools(
                task,
                db,
                [CrewStageTool("generate_storyboard", lambda: create_shots(script.id, db))],
            )
            task.result_data = {
                **(task.result_data or {}),
                "scene_count": len(results["generate_storyboard"]),
            }
            next_stage = "prompts"
        elif stage_name == "prompts":
            def generate_shot_prompts() -> dict:
                scenes = list_scenes(project.id, db)
                count = 0
                mode_counts = {"initial_frame": 0, "storyboard": 0}
                for scene in scenes:
                    for shot in scene.shots:
                        mode = classify_shot_prompt_strategy(shot).recommended_mode
                        create_prompt(shot.id, db, PromptGenerateRequest(mode=mode))
                        mode_counts[mode] += 1
                        count += 1
                return {
                    "prompt_count": count,
                    "mode": "per_shot_strategy",
                    "mode_counts": mode_counts,
                }

            results = run_stage_tools(
                task,
                db,
                [CrewStageTool("generate_shot_prompts", generate_shot_prompts)],
            )
            task.result_data = {**(task.result_data or {}), **results["generate_shot_prompts"]}
            next_stage = "images"
        elif stage_name == "images":
            suggestion = {"suggestion": "Select key assets before confirming image batches."}
            run_stage_tools(
                task,
                db,
                [CrewStageTool("get_workflow_status", lambda: suggestion)],
            )
            task.result_data = {**(task.result_data or {}), **suggestion}
            next_stage = "completed"
        else:
            raise HTTPException(status_code=409, detail="This stage cannot be executed directly")
        mark_completed(task, output_snapshot=task.result_data)
        plan.session.current_stage = next_stage
        plan.session.status = (
            AgentSessionStatus.completed
            if next_stage == "completed"
            else AgentSessionStatus.awaiting_stage_approval
        )
        _append_stage_completed_message(db, plan, task)
        db.commit()
    except Exception as exc:
        db.rollback()
        task = db.get(WorkflowTask, task.id)
        mark_failed(task, exc, last_safe_step="stage_started")
        db.commit()
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    db.refresh(task)
    return task


@app.get(
    f"{API}/agent/tasks/{{task_id}}/checkpoint",
    response_model=WorkflowTaskCheckpointRead,
)
def get_agent_task_checkpoint(task_id: str, db: DbSession) -> dict:
    task = _require_workflow_task(db, task_id)
    checkpoint = (task.result_data or {}).get("checkpoint")
    return checkpoint or build_checkpoint(
        task,
        status=task.status.value if hasattr(task.status, "value") else str(task.status),
        last_safe_step="not_started",
        input_snapshot=task.input_data,
        output_snapshot=task.result_data,
    )


@app.post(f"{API}/agent/tasks/{{task_id}}/cancel", response_model=WorkflowTaskRead)
def cancel_agent_task(task_id: str, db: DbSession) -> WorkflowTask:
    task = _require_workflow_task(db, task_id)
    if task.status == WorkflowTaskStatus.completed:
        raise HTTPException(status_code=409, detail="Completed tasks cannot be cancelled")
    task.status = WorkflowTaskStatus.cancelled
    task.error_message = "User interrupted the workflow task."
    write_checkpoint(
        task,
        status="failed",
        last_safe_step="user_interrupted",
        error={
            "type": "user_interrupted",
            "message": "User interrupted the workflow task.",
            "retryable": False,
        },
    )
    db.commit()
    db.refresh(task)
    return task


@app.post(f"{API}/agent/tasks/{{task_id}}/resume", response_model=WorkflowTaskRead)
def resume_agent_task(task_id: str, db: DbSession) -> WorkflowTask:
    task = _require_workflow_task(db, task_id)
    recovery = (task.result_data or {}).get("recovery", {})
    if task.status == WorkflowTaskStatus.completed:
        return task
    if task.status == WorkflowTaskStatus.cancelled:
        raise HTTPException(status_code=409, detail="Cancelled tasks cannot be resumed")
    if recovery and not recovery.get("retryable", True):
        raise HTTPException(status_code=409, detail="Task is not retryable")
    task.status = WorkflowTaskStatus.awaiting_approval
    write_checkpoint(task, status="waiting_user", last_safe_step="resume_requested")
    db.commit()
    db.refresh(task)
    return task


def _run_embedding_job(job_id: str, bind=engine) -> None:
    worker_session = sessionmaker(bind=bind, autoflush=False, expire_on_commit=False)()
    try:
        job = worker_session.get(EmbeddingJob, job_id)
        if job is None:
            return
        document = worker_session.get(ScriptDocument, job.document_id)
        chunks = list(
            worker_session.scalars(
                select(ScriptChunk)
                .where(ScriptChunk.document_id == document.id)
                .order_by(ScriptChunk.sequence)
            ).all()
        )
        rag = LocalRAG()
        rag_status = rag.status()
        if not rag_status["available"]:
            job.status = "keyword_ready"
            document.status = "ready"
        else:
            job.status = "embedding"
            worker_session.commit()
            rag.index_chunks(
                [
                    {
                        "id": chunk.id,
                        "content": chunk.content,
                        "chunk_id": chunk.id,
                        "project_id": document.project_id,
                        "script_id": document.script_version_id,
                        "script_version": document.version,
                        "content_type": "script_chunk",
                        "chapter": chunk.chapter,
                        "scene": chunk.scene,
                        "characters": chunk.characters,
                        "locations": chunk.locations,
                        "content_hash": chunk.content_hash,
                        "is_current": True,
                    }
                    for chunk in chunks
                ]
            )
            job.status = "ready"
            job.processed_count = len(chunks)
            document.status = "ready"
        worker_session.query(ScriptDocument).filter(
            ScriptDocument.project_id == document.project_id,
            ScriptDocument.id != document.id,
        ).update({ScriptDocument.is_current: False})
        document.is_current = True
        worker_session.commit()
    except Exception:
        worker_session.rollback()
        job = worker_session.get(EmbeddingJob, job_id)
        if job:
            job.status = "failed"
            job.error_message = "Local embedding or vector indexing failed"
            worker_session.commit()
    finally:
        worker_session.close()


def _mark_document_current(db: Session, document: ScriptDocument) -> None:
    db.query(ScriptDocument).filter(
        ScriptDocument.project_id == document.project_id,
        ScriptDocument.id != document.id,
    ).update({ScriptDocument.is_current: False})
    document.is_current = True


def _script_index_response(db: Session, script_id: str) -> dict:
    document = db.scalar(
        select(ScriptDocument).where(ScriptDocument.script_version_id == script_id)
    )
    if document is None:
        raise HTTPException(status_code=404, detail="Script index not found")
    job = db.scalar(
        select(EmbeddingJob)
        .where(EmbeddingJob.document_id == document.id)
        .order_by(EmbeddingJob.created_at.desc())
    )
    count = db.scalar(
        select(func.count(ScriptChunk.id)).where(ScriptChunk.document_id == document.id)
    )
    return {
        "script_id": script_id,
        "document_id": document.id,
        "status": document.status,
        "is_current": document.is_current,
        "chunk_count": count or 0,
        "embedding_status": job.status if job else "not_started",
        "embedding_model": job.model if job else get_settings().embedding_model,
    }


def _delete_script_index(db: Session, script_id: str, *, delete_vectors: bool = True) -> None:
    existing = db.scalar(
        select(ScriptDocument).where(ScriptDocument.script_version_id == script_id)
    )
    if existing is None:
        return
    if delete_vectors and LocalRAG().status()["qdrant_local"]:
        try:
            LocalRAG().delete_script_vectors(script_id)
        except Exception:
            pass
    db.execute(delete(EmbeddingJob).where(EmbeddingJob.document_id == existing.id))
    db.execute(delete(ScriptSummary).where(ScriptSummary.document_id == existing.id))
    db.execute(delete(ScriptChunk).where(ScriptChunk.document_id == existing.id))
    db.delete(existing)


@app.post(f"{API}/scripts/{{script_id}}/index", response_model=ScriptIndexRead)
def create_script_index(
    script_id: str, background_tasks: BackgroundTasks, db: DbSession
) -> dict:
    script = require_script(db, script_id)
    document, job = index_script(db, script)
    if not LocalRAG().status()["available"]:
        job.status = "keyword_ready"
        document.status = "ready"
        _mark_document_current(db, document)
        db.commit()
    elif job.status in {"pending", "failed"}:
        background_tasks.add_task(_run_embedding_job, job.id, db.get_bind())
    return _script_index_response(db, script_id)


@app.post(f"{API}/scripts/{{script_id}}/index/rebuild", response_model=ScriptIndexRead)
def rebuild_script_index(
    script_id: str, background_tasks: BackgroundTasks, db: DbSession
) -> dict:
    script = require_script(db, script_id)
    _delete_script_index(db, script_id)
    db.commit()
    document, job = index_script(db, script)
    if not LocalRAG().status()["available"]:
        job.status = "keyword_ready"
        document.status = "ready"
        _mark_document_current(db, document)
        db.commit()
    else:
        background_tasks.add_task(_run_embedding_job, job.id, db.get_bind())
    return _script_index_response(db, script_id)


@app.get(f"{API}/scripts/{{script_id}}/index", response_model=ScriptIndexRead)
def get_script_index(script_id: str, db: DbSession) -> dict:
    require_script(db, script_id)
    return _script_index_response(db, script_id)


@app.post(f"{API}/agent/retrieval/rebuild", response_model=RetrievalRebuildRead)
def rebuild_retrieval_index(
    payload: RetrievalRebuildRequest, background_tasks: BackgroundTasks, db: DbSession
) -> dict:
    if payload.project_id:
        require_project(db, payload.project_id)
    statement = select(ScriptVersion)
    if payload.project_id:
        statement = statement.where(ScriptVersion.project_id == payload.project_id)
    scripts = list(
        db.scalars(
            statement.order_by(ScriptVersion.project_id, ScriptVersion.version)
        ).all()
    )
    rag_status = LocalRAG().status()
    if rag_status["qdrant_local"]:
        project_ids = sorted({script.project_id for script in scripts})
        for project_id in project_ids:
            try:
                LocalRAG().delete_project_vectors(project_id)
            except Exception:
                pass
    for script in scripts:
        _delete_script_index(db, script.id, delete_vectors=False)
    db.commit()

    rebuilt_count = 0
    queued_embedding_jobs = 0
    fallback_count = 0
    for script in scripts:
        document, job = index_script(db, script)
        rebuilt_count += 1
        if not rag_status["available"]:
            job.status = "keyword_ready"
            document.status = "ready"
            _mark_document_current(db, document)
            fallback_count += 1
            db.commit()
        else:
            queued_embedding_jobs += 1
            background_tasks.add_task(_run_embedding_job, job.id, db.get_bind())
    return {
        "project_id": payload.project_id,
        "script_count": len(scripts),
        "rebuilt_count": rebuilt_count,
        "queued_embedding_jobs": queued_embedding_jobs,
        "fallback_count": fallback_count,
    }


@app.get(f"{API}/agent/retrieval/status", response_model=RetrievalStatusRead)
def get_retrieval_status(db: DbSession) -> dict:
    settings = get_settings()
    return {
        **get_retrieval_status_snapshot(db),
        "deepseek_thinking_enabled": settings.deepseek_thinking_enabled,
        "deepseek_reasoning_effort": settings.deepseek_reasoning_effort,
        "web_search_configured": bool(settings.ark_search_model.strip()),
    }


@app.post(f"{API}/agent/retrieval/self-test", response_model=RetrievalSelfTestRead)
def run_retrieval_self_test() -> dict:
    return LocalRAG().self_test()


@app.post(f"{API}/agent/retrieve", response_model=list[RetrievalHit])
def retrieve_agent_context(payload: RetrievalQuery, db: DbSession) -> list[dict]:
    return retrieve_context(payload, db)


@app.post(f"{API}/agent/tools/search")
async def agent_search_web(payload: WebSearchRequest, db: DbSession) -> dict:
    if payload.session_id:
        _require_agent_session(db, payload.session_id)
    try:
        result = await search_web(payload.query)
    except (WebToolError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if payload.session_id:
        persisted_sources = []
        for source in result["sources"]:
            research = ResearchSource(
                session_id=payload.session_id,
                query=payload.query,
                title=source.get("title", source["url"]),
                url=source["url"],
                summary=result["summary"][:4000],
                fetch_method="volcengine",
            )
            db.add(research)
            db.flush()
            persisted_sources.append(
                {
                    "id": research.id,
                    "title": research.title,
                    "url": research.url,
                    "adopted": research.adopted,
                }
            )
        db.commit()
        result["persisted_sources"] = persisted_sources
    return result


@app.post(
    f"{API}/agent/research/{{source_id}}/adopt",
    response_model=ResearchSourceRead,
)
def adopt_research_source(
    source_id: str, payload: ResearchAdoptRequest, db: DbSession
) -> ResearchSource:
    source = require_research_source(db, source_id)
    source.adopted = True
    source.adoption_reason = payload.adoption_reason.strip()
    db.commit()
    db.refresh(source)
    session = _require_agent_session(db, source.session_id)
    if source.summary.strip() and LocalRAG().status()["available"]:
        try:
            digest = hashlib.sha256(source.summary.encode("utf-8")).hexdigest()
            LocalRAG().index_chunks(
                [
                    {
                        "id": source.id,
                        "content": source.summary,
                        "chunk_id": source.id,
                        "source_id": source.id,
                        "session_id": source.session_id,
                        "project_id": session.project_id,
                        "script_id": "",
                        "script_version": 0,
                        "content_type": "research_source",
                        "chapter": "research",
                        "scene": source.title,
                        "characters": [],
                        "locations": [],
                        "memory_status": "researched",
                        "content_hash": digest,
                        "is_current": True,
                    }
                ]
            )
        except Exception:
            pass
    return source


@app.post(f"{API}/agent/tools/fetch")
async def agent_fetch_page(payload: PageFetchRequest, db: DbSession) -> dict:
    if payload.session_id:
        _require_agent_session(db, payload.session_id)
    try:
        result = await fetch_page(payload.url)
    except (WebToolError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {**result, "content": result["content"][: get_settings().web_max_bytes]}
