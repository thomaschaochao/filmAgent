import re
import time
import unicodedata
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, inspect, select, text
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.config import get_settings
from app.database import Base, engine, get_db
from app.models import (
    AgentRun,
    Asset,
    AssetImage,
    Project,
    ProjectStatus,
    PromptVersion,
    Scene,
    ScriptVersion,
    Shot,
    StoryboardSnapshot,
)
from app.schemas import (
    AgentMetricsRead,
    AssetCreate,
    AssetImageGenerateRequest,
    AssetImageRead,
    AssetRead,
    AssetUpdate,
    ImageProviderRead,
    ProjectCreate,
    ProjectRead,
    ProjectUpdate,
    PromptRead,
    SceneRead,
    ScriptCreate,
    ScriptGenerateRequest,
    ScriptRead,
    ShotRead,
    ShotUpdate,
    StoryboardDraft,
    StoryboardSnapshotRead,
)
from app.services.deepseek import DeepSeekClient, DeepSeekError
from app.services.image_generation import ImageGenerationError, generate_image
from app.services.workflow import (
    extract_assets,
    generate_asset_prompt,
    generate_prompt,
    generate_script,
    generate_storyboard,
    validate_storyboard,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    columns = {column["name"] for column in inspect(engine).get_columns("projects")}
    if "world_setting" not in columns:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE projects ADD COLUMN world_setting TEXT NOT NULL DEFAULT ''")
            )
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
    }
    with engine.begin() as connection:
        for name, definition in agent_run_migrations.items():
            if name not in agent_run_columns:
                connection.execute(text(f"ALTER TABLE agent_runs ADD COLUMN {name} {definition}"))
    yield


app = FastAPI(title="FilmAgent API", version="0.1.0", lifespan=lifespan)
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
        validation_results=validation_results or [],
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
    return {"status": "ok"}


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
def create_prompt(shot_id: str, db: DbSession) -> PromptVersion:
    shot = require_shot(db, shot_id)
    project = shot.scene.project
    client = DeepSeekClient()
    try:
        draft = generate_prompt(client, project, shot)
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
