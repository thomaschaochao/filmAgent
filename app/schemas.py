from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models import (
    ChatMessageRole,
    ChatPage,
    ChatScope,
    ChatThreadStatus,
    ProjectStatus,
    ProposalStatus,
)


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


ImageProvider = Literal["openai", "seedream"]


class ImageProviderRead(BaseModel):
    id: ImageProvider
    name: str
    model: str
    configured: bool


class AssetImageGenerateRequest(BaseModel):
    provider: ImageProvider


class AssetImageRead(ORMModel):
    id: str
    asset_id: str
    source: str
    provider: str | None
    model: str | None
    prompt_snapshot: str
    size: str
    quality: str
    status: str
    error_message: str | None
    image_url: str | None
    local_path: str | None
    is_primary: bool
    created_at: datetime
    updated_at: datetime


class AssetDraft(BaseModel):
    asset_type: AssetType
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=3)


class AssetExtractionDraft(BaseModel):
    assets: list[AssetDraft] = Field(min_length=1)


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
    script_reference: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    action: str = Field(min_length=1)
    environment: str = Field(min_length=1)
    shot_size: str = Field(min_length=1)
    camera_angle: str = Field(min_length=1)
    camera_motion: str = Field(min_length=1)
    duration_seconds: float = Field(default=4.0, ge=0.5, le=60)
    emotion: str = ""
    lighting: str = ""
    dialogue: str = ""
    narrative_purpose: str = ""
    continuity: str = ""


class SceneDraft(BaseModel):
    sequence: int = Field(ge=1)
    heading: str = Field(min_length=1)
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
    duration_seconds: float | None = Field(default=None, ge=0.5, le=60)
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


PromptMode = Literal["initial_frame", "storyboard"]
StoryboardFrameCount = Literal[4, 6, 9]


class PromptGenerateRequest(BaseModel):
    mode: PromptMode = "initial_frame"
    frame_count: StoryboardFrameCount | None = None


class StoryboardFrameDraft(BaseModel):
    index: int = Field(ge=1, le=9)
    phase: Literal["start", "middle", "end"]
    description: str = Field(min_length=1)


class PromptDraft(BaseModel):
    positive_prompt: str = Field(min_length=20)
    negative_prompt: str = ""
    model_target: str = "generic"
    subject_position: str = Field(min_length=1)
    action_constraints: str = Field(min_length=1)
    spatial_constraints: str = Field(min_length=1)
    camera_strategy: str = Field(min_length=1)
    components: dict[str, str] = Field(default_factory=dict)
    asset_references: list[str] = Field(default_factory=list)
    frames: list[StoryboardFrameDraft] = Field(default_factory=list)


class PromptRead(ORMModel):
    id: str
    shot_id: str
    version: int
    positive_prompt: str
    negative_prompt: str
    model_target: str
    prompt_metadata: dict
    created_at: datetime


class ValidationCheckRead(BaseModel):
    key: str
    label: str
    passed: bool
    value: float | int
    threshold: float | int | None = None
    detail: str


class AgentRunRead(ORMModel):
    id: str
    operation: str
    target_type: str
    target_id: str | None
    status: str
    validation_passed: bool | None
    validation_results: list[ValidationCheckRead]
    is_regeneration: bool
    error_message: str | None
    error_type: str | None
    provider: str
    model: str | None
    request_id: str | None
    latency_ms: int | None
    attempt_count: int
    input_tokens: int | None
    output_tokens: int | None
    prompt_version: str
    rules_version: str | None
    created_at: datetime


class AgentRunDetailRead(AgentRunRead):
    system_prompt: str
    user_prompt: str
    raw_response: str
    rules_snapshot: dict


class StoryboardSnapshotRead(ORMModel):
    id: str
    project_id: str
    script_version_id: str
    version: int
    payload: dict
    source: str
    created_at: datetime


class ValidationMetricRead(BaseModel):
    key: str
    label: str
    total: int
    passed: int
    pass_rate: float


class AgentMetricsRead(BaseModel):
    total_runs: int
    passed_runs: int
    failed_runs: int
    pass_rate: float
    regeneration_count: int
    validation_failed_count: int
    request_failed_count: int
    total_input_tokens: int
    total_output_tokens: int
    average_latency_ms: float
    validations: list[ValidationMetricRead]
    recent_runs: list[AgentRunRead]


CHAT_TARGET_TYPES = {
    ChatPage.script: {"script"},
    ChatPage.assets: {"asset"},
    ChatPage.shots: {"shot"},
    ChatPage.prompts: {"prompt"},
}


class ChatThreadCreate(BaseModel):
    page: ChatPage
    scope: ChatScope = ChatScope.page
    target_type: str | None = None
    target_id: str | None = None
    title: str = Field(default="New chat", min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_scope(self) -> "ChatThreadCreate":
        if self.scope == ChatScope.page and (self.target_type or self.target_id):
            raise ValueError("page-scoped threads cannot specify a target")
        if self.scope == ChatScope.object:
            if not self.target_type or not self.target_id:
                raise ValueError("object-scoped threads require target_type and target_id")
            if self.target_type not in CHAT_TARGET_TYPES[self.page]:
                raise ValueError("target_type does not belong to the selected page")
        return self


ProposalAction = Literal["create", "update", "delete", "reorder", "create_version"]
ProposalResource = Literal["script", "asset", "shot", "prompt"]


class ProposalOperation(BaseModel):
    action: ProposalAction
    resource: ProposalResource
    target_id: str | None = None
    values: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_target(self) -> "ProposalOperation":
        if self.action in {"update", "delete", "reorder", "create_version"} and not self.target_id:
            raise ValueError(f"{self.action} operations require target_id")
        if self.action == "create" and self.target_id:
            raise ValueError("create operations cannot specify target_id")
        return self


class ChangeProposalDraft(BaseModel):
    operations: list[ProposalOperation] = Field(min_length=1)
    summary: str = Field(min_length=1)


class ChatMessageCreate(BaseModel):
    content: str = Field(min_length=1)
    proposal: ChangeProposalDraft | None = None


class ChatAssistantDraft(BaseModel):
    reply: str = Field(min_length=1)
    proposal: ChangeProposalDraft | None = None


class ChatThreadRead(ORMModel):
    id: str
    project_id: str
    page: ChatPage
    scope: ChatScope
    target_type: str | None
    target_id: str | None
    title: str
    status: ChatThreadStatus
    created_at: datetime
    updated_at: datetime


class ChatMessageRead(ORMModel):
    id: str
    thread_id: str
    role: ChatMessageRole
    content: str
    context_summary: dict
    created_at: datetime


class ChangeProposalRead(ORMModel):
    id: str
    thread_id: str
    trigger_message_id: str | None
    target_scope: dict
    operations: list[ProposalOperation]
    before_snapshot: dict
    after_preview: dict
    base_version: dict
    status: ProposalStatus
    error_message: str | None
    created_at: datetime
    applied_at: datetime | None
    reverted_at: datetime | None


class ChatThreadDetail(ChatThreadRead):
    messages: list[ChatMessageRead]
    proposals: list[ChangeProposalRead]
