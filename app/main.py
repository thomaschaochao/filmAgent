import re
import unicodedata
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, inspect, select, text
from sqlalchemy.orm import Session, selectinload

from app.database import Base, engine, get_db
from app.models import Asset, Project, ProjectStatus, PromptVersion, Scene, ScriptVersion, Shot
from app.schemas import (
    AssetCreate,
    AssetRead,
    AssetUpdate,
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
)
from app.services.deepseek import DeepSeekClient, DeepSeekError
from app.services.workflow import (
    extract_assets,
    generate_asset_prompt,
    generate_prompt,
    generate_script,
    generate_storyboard,
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
    try:
        content = generate_script(
            DeepSeekClient(),
            brief=payload.brief,
            title=payload.title,
            instructions=payload.instructions,
            language=project.language,
            world_setting=project.world_setting,
        )
    except DeepSeekError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    script = ScriptVersion(
        project_id=project.id,
        version=_next_script_version(db, project.id),
        title=payload.title,
        content=content,
        source_type="ai",
    )
    project.status = ProjectStatus.script_review
    db.add(script)
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
        select(Asset)
        .where(Asset.project_id == project_id)
        .order_by(Asset.asset_type, Asset.name)
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
    if asset.image_path:
        image_path = (STORAGE_DIR / asset.image_path).resolve()
        if image_path.is_relative_to(STORAGE_DIR.resolve()) and image_path.exists():
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
    try:
        draft = extract_assets(DeepSeekClient(), project, script.content)
    except DeepSeekError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

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
    db.commit()
    statement = (
        select(Asset)
        .where(Asset.project_id == project.id)
        .order_by(Asset.asset_type, Asset.name)
    )
    return list(db.scalars(statement).all())


@app.post(f"{API}/assets/{{asset_id}}/prompt/generate", response_model=AssetRead)
def create_asset_prompt(asset_id: str, db: DbSession) -> Asset:
    asset = require_asset(db, asset_id)
    try:
        asset.prompt = generate_asset_prompt(DeepSeekClient(), asset.project, asset)
    except DeepSeekError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
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
    if asset.image_path:
        previous = (STORAGE_DIR / asset.image_path).resolve()
        if previous.is_relative_to(STORAGE_DIR.resolve()) and previous.exists():
            previous.unlink()
    relative_path = Path("assets") / f"{asset.id}{extension}"
    (STORAGE_DIR / relative_path).write_bytes(content)
    asset.image_path = relative_path.as_posix()
    db.commit()
    db.refresh(asset)
    return asset


@app.post(f"{API}/scripts/{{script_id}}/shots/generate", response_model=list[SceneRead])
def create_shots(script_id: str, db: DbSession) -> list[Scene]:
    script = require_script(db, script_id)
    if not script.is_approved:
        raise HTTPException(status_code=409, detail="Approve the script before generating shots")
    project = script.project
    try:
        draft = generate_storyboard(DeepSeekClient(), project, script.content)
    except DeepSeekError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    existing = list(db.scalars(select(Scene).where(Scene.project_id == project.id)).all())
    for scene in existing:
        db.delete(scene)
    db.flush()

    created: list[Scene] = []
    for scene_draft in draft.scenes:
        scene_data = scene_draft.model_dump(exclude={"shots"})
        scene = Scene(project_id=project.id, script_version_id=script.id, **scene_data)
        for shot_draft in scene_draft.shots:
            scene.shots.append(Shot(**shot_draft.model_dump()))
        db.add(scene)
        created.append(scene)
    project.status = ProjectStatus.shot_list_review
    db.commit()

    statement = (
        select(Scene)
        .where(Scene.project_id == project.id)
        .options(selectinload(Scene.shots))
        .order_by(Scene.sequence)
    )
    return list(db.scalars(statement).all())


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


@app.post(f"{API}/shots/{{shot_id}}/prompts/generate", response_model=PromptRead)
def create_prompt(shot_id: str, db: DbSession) -> PromptVersion:
    shot = require_shot(db, shot_id)
    project = shot.scene.project
    try:
        draft = generate_prompt(DeepSeekClient(), project, shot)
    except DeepSeekError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
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
