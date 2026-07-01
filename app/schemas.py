from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models import ProjectStatus


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    language: str = "zh-CN"
    aspect_ratio: str = "16:9"
    visual_style: str = "cinematic storyboard"
    world_setting: str = ""


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    language: str | None = None
    aspect_ratio: str | None = None
    visual_style: str | None = None
    world_setting: str | None = None


class ProjectRead(ProjectCreate, ORMModel):
    id: str
    status: ProjectStatus
    created_at: datetime
    updated_at: datetime


AssetType = Literal["character", "location", "prop"]


class AssetCreate(BaseModel):
    asset_type: AssetType
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    prompt: str = ""


class AssetUpdate(BaseModel):
    asset_type: AssetType | None = None
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    prompt: str | None = None


class AssetRead(AssetCreate, ORMModel):
    id: str
    project_id: str
    image_url: str | None
    created_at: datetime
    updated_at: datetime


class AssetDraft(BaseModel):
    asset_type: AssetType
    name: str = Field(min_length=1, max_length=200)
    description: str


class AssetExtractionDraft(BaseModel):
    assets: list[AssetDraft]


class ScriptCreate(BaseModel):
    title: str = "未命名剧本"
    content: str = Field(min_length=1)
    source_type: str = "user"


class ScriptGenerateRequest(BaseModel):
    brief: str = Field(min_length=1)
    title: str = "AI 生成剧本"
    instructions: str = ""


class ScriptRead(ORMModel):
    id: str
    project_id: str
    version: int
    title: str
    content: str
    source_type: str
    is_approved: bool
    created_at: datetime


class ShotDraft(BaseModel):
    sequence: int = Field(ge=1)
    script_reference: str = ""
    subject: str
    action: str
    environment: str
    shot_size: str
    camera_angle: str
    camera_motion: str
    emotion: str = ""
    lighting: str = ""
    dialogue: str = ""
    narrative_purpose: str = ""
    continuity: str = ""


class SceneDraft(BaseModel):
    sequence: int = Field(ge=1)
    heading: str
    summary: str = ""
    location: str = ""
    time_of_day: str = ""
    shots: list[ShotDraft] = Field(min_length=1)


class StoryboardDraft(BaseModel):
    scenes: list[SceneDraft] = Field(min_length=1)


class ShotRead(ShotDraft, ORMModel):
    id: str
    scene_id: str
    is_locked: bool


class ShotUpdate(BaseModel):
    script_reference: str | None = None
    subject: str | None = None
    action: str | None = None
    environment: str | None = None
    shot_size: str | None = None
    camera_angle: str | None = None
    camera_motion: str | None = None
    emotion: str | None = None
    lighting: str | None = None
    dialogue: str | None = None
    narrative_purpose: str | None = None
    continuity: str | None = None
    is_locked: bool | None = None


class SceneRead(ORMModel):
    id: str
    project_id: str
    script_version_id: str
    sequence: int
    heading: str
    summary: str
    location: str
    time_of_day: str
    shots: list[ShotRead]


class PromptDraft(BaseModel):
    positive_prompt: str
    negative_prompt: str = ""
    model_target: str = "generic"
    components: dict[str, str] = Field(default_factory=dict)
    asset_references: list[str] = Field(default_factory=list)


class PromptRead(ORMModel):
    id: str
    shot_id: str
    version: int
    positive_prompt: str
    negative_prompt: str
    model_target: str
    prompt_metadata: dict
    created_at: datetime
