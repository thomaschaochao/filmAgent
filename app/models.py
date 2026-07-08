import uuid
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


class ProjectStatus(StrEnum):
    draft_script = "DRAFT_SCRIPT"
    script_review = "SCRIPT_REVIEW"
    script_approved = "SCRIPT_APPROVED"
    asset_review = "ASSET_REVIEW"
    shot_list_review = "SHOT_LIST_REVIEW"
    prompt_review = "PROMPT_REVIEW"
    completed = "COMPLETED"


class ChatPage(StrEnum):
    script = "script"
    assets = "assets"
    shots = "shots"
    prompts = "prompts"


class ChatScope(StrEnum):
    page = "page"
    object = "object"


class ChatThreadStatus(StrEnum):
    active = "active"
    archived = "archived"


class ChatMessageRole(StrEnum):
    user = "user"
    assistant = "assistant"
    system = "system"


class ProposalStatus(StrEnum):
    draft = "draft"
    applying = "applying"
    applied = "applied"
    failed = "failed"
    rejected = "rejected"
    reverted = "reverted"


class AgentSessionStatus(StrEnum):
    collecting = "collecting"
    clarifying = "clarifying"
    researching = "researching"
    plan_ready = "plan_ready"
    awaiting_approval = "awaiting_approval"
    executing = "executing"
    awaiting_stage_approval = "awaiting_stage_approval"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class WorkflowTaskStatus(StrEnum):
    pending = "pending"
    running = "running"
    awaiting_approval = "awaiting_approval"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    language: Mapped[str] = mapped_column(String(32), default="zh-CN")
    aspect_ratio: Mapped[str] = mapped_column(String(16), default="16:9")
    visual_style: Mapped[str] = mapped_column(Text, default="cinematic storyboard")
    world_setting: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[ProjectStatus] = mapped_column(
        Enum(ProjectStatus), default=ProjectStatus.draft_script
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    scripts: Mapped[list["ScriptVersion"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    scenes: Mapped[list["Scene"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    assets: Mapped[list["Asset"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="Asset.name"
    )
    chat_threads: Mapped[list["ChatThread"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class Asset(Base):
    __tablename__ = "assets"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_asset_project_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    asset_type: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    prompt: Mapped[str] = mapped_column(Text, default="")
    image_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    project: Mapped[Project] = relationship(back_populates="assets")
    images: Mapped[list["AssetImage"]] = relationship(
        back_populates="asset", cascade="all, delete-orphan", order_by="AssetImage.created_at"
    )

    @property
    def image_url(self) -> str | None:
        return f"/storage/{self.image_path}" if self.image_path else None


class AssetImage(Base):
    __tablename__ = "asset_images"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(32), default="generated")
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    prompt_snapshot: Mapped[str] = mapped_column(Text, default="")
    size: Mapped[str] = mapped_column(String(32), default="1536x1024")
    quality: Mapped[str] = mapped_column(String(32), default="high")
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    asset: Mapped[Asset] = relationship(back_populates="images")

    @property
    def image_url(self) -> str | None:
        return f"/storage/{self.image_path}" if self.image_path else None

    @property
    def local_path(self) -> str | None:
        if not self.image_path:
            return None
        storage_root = (Path(__file__).resolve().parent.parent / "storage").resolve()
        return str((storage_root / self.image_path).resolve())


class ScriptVersion(Base):
    __tablename__ = "script_versions"
    __table_args__ = (UniqueConstraint("project_id", "version", name="uq_script_project_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(200), default="未命名剧本")
    content: Mapped[str] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(32), default="user")
    is_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    project: Mapped[Project] = relationship(back_populates="scripts")
    scenes: Mapped[list["Scene"]] = relationship(back_populates="script_version")


class Scene(Base):
    __tablename__ = "scenes"
    __table_args__ = (UniqueConstraint("project_id", "sequence", name="uq_scene_project_sequence"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    script_version_id: Mapped[str] = mapped_column(
        ForeignKey("script_versions.id", ondelete="CASCADE")
    )
    sequence: Mapped[int] = mapped_column(Integer)
    heading: Mapped[str] = mapped_column(String(300))
    summary: Mapped[str] = mapped_column(Text, default="")
    location: Mapped[str] = mapped_column(String(300), default="")
    time_of_day: Mapped[str] = mapped_column(String(100), default="")

    project: Mapped[Project] = relationship(back_populates="scenes")
    script_version: Mapped[ScriptVersion] = relationship(back_populates="scenes")
    shots: Mapped[list["Shot"]] = relationship(
        back_populates="scene", cascade="all, delete-orphan", order_by="Shot.sequence"
    )


class Shot(Base):
    __tablename__ = "shots"
    __table_args__ = (UniqueConstraint("scene_id", "sequence", name="uq_shot_scene_sequence"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    scene_id: Mapped[str] = mapped_column(ForeignKey("scenes.id", ondelete="CASCADE"))
    sequence: Mapped[int] = mapped_column(Integer)
    script_reference: Mapped[str] = mapped_column(Text, default="")
    subject: Mapped[str] = mapped_column(Text, default="")
    action: Mapped[str] = mapped_column(Text, default="")
    environment: Mapped[str] = mapped_column(Text, default="")
    shot_size: Mapped[str] = mapped_column(String(100), default="medium shot")
    camera_angle: Mapped[str] = mapped_column(String(100), default="eye level")
    camera_motion: Mapped[str] = mapped_column(String(100), default="static")
    duration_seconds: Mapped[float] = mapped_column(Float, default=4.0)
    emotion: Mapped[str] = mapped_column(String(200), default="")
    lighting: Mapped[str] = mapped_column(String(300), default="")
    dialogue: Mapped[str] = mapped_column(Text, default="")
    narrative_purpose: Mapped[str] = mapped_column(Text, default="")
    continuity: Mapped[str] = mapped_column(Text, default="")
    is_locked: Mapped[bool] = mapped_column(Boolean, default=False)

    scene: Mapped[Scene] = relationship(back_populates="shots")
    prompts: Mapped[list["PromptVersion"]] = relationship(
        back_populates="shot", cascade="all, delete-orphan"
    )


class PromptVersion(Base):
    __tablename__ = "prompt_versions"
    __table_args__ = (UniqueConstraint("shot_id", "version", name="uq_prompt_shot_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    shot_id: Mapped[str] = mapped_column(ForeignKey("shots.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    positive_prompt: Mapped[str] = mapped_column(Text)
    negative_prompt: Mapped[str] = mapped_column(Text, default="")
    model_target: Mapped[str] = mapped_column(String(100), default="generic")
    prompt_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    shot: Mapped[Shot] = relationship(back_populates="prompts")


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    operation: Mapped[str] = mapped_column(String(64), index=True)
    target_type: Mapped[str] = mapped_column(String(32), default="project")
    target_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    validation_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    validation_results: Mapped[list] = mapped_column(JSON, default=list)
    is_regeneration: Mapped[bool] = mapped_column(Boolean, default=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider: Mapped[str] = mapped_column(String(32), default="deepseek")
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=1)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(64), default="v1")
    rules_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rules_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    user_prompt: Mapped[str] = mapped_column(Text, default="")
    raw_response: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class StoryboardSnapshot(Base):
    __tablename__ = "storyboard_snapshots"
    __table_args__ = (
        UniqueConstraint("project_id", "version", name="uq_storyboard_snapshot_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    script_version_id: Mapped[str] = mapped_column(ForeignKey("script_versions.id"))
    version: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(String(32), default="generation")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChatThread(Base):
    __tablename__ = "chat_threads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    page: Mapped[ChatPage] = mapped_column(Enum(ChatPage), index=True)
    scope: Mapped[ChatScope] = mapped_column(Enum(ChatScope), index=True)
    target_type: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    target_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(200), default="New chat")
    status: Mapped[ChatThreadStatus] = mapped_column(
        Enum(ChatThreadStatus), default=ChatThreadStatus.active, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    project: Mapped[Project] = relationship(back_populates="chat_threads")
    messages: Mapped[list["ChatMessage"]] = relationship(
        back_populates="thread", cascade="all, delete-orphan", order_by="ChatMessage.created_at"
    )
    proposals: Mapped[list["ChangeProposal"]] = relationship(
        back_populates="thread",
        cascade="all, delete-orphan",
        order_by="ChangeProposal.created_at",
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    thread_id: Mapped[str] = mapped_column(
        ForeignKey("chat_threads.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[ChatMessageRole] = mapped_column(Enum(ChatMessageRole), index=True)
    content: Mapped[str] = mapped_column(Text)
    context_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    thread: Mapped[ChatThread] = relationship(back_populates="messages")
    proposals: Mapped[list["ChangeProposal"]] = relationship(back_populates="trigger_message")


class ChangeProposal(Base):
    __tablename__ = "change_proposals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    thread_id: Mapped[str] = mapped_column(
        ForeignKey("chat_threads.id", ondelete="CASCADE"), index=True
    )
    trigger_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True, index=True
    )
    target_scope: Mapped[dict] = mapped_column(JSON, default=dict)
    operations: Mapped[list] = mapped_column(JSON, default=list)
    before_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    after_preview: Mapped[dict] = mapped_column(JSON, default=dict)
    base_version: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[ProposalStatus] = mapped_column(
        Enum(ProposalStatus), default=ProposalStatus.draft, index=True
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reverted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    thread: Mapped[ChatThread] = relationship(back_populates="proposals")
    trigger_message: Mapped[ChatMessage | None] = relationship(back_populates="proposals")


class AgentSession(Base):
    __tablename__ = "agent_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(200), default="新的影片计划")
    status: Mapped[AgentSessionStatus] = mapped_column(
        Enum(AgentSessionStatus), default=AgentSessionStatus.collecting, index=True
    )
    current_stage: Mapped[str] = mapped_column(String(64), default="discovery")
    original_input: Mapped[str] = mapped_column(Text, default="")
    context_summary: Mapped[str] = mapped_column(Text, default="")
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    messages: Mapped[list["AgentMessage"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="AgentMessage.created_at"
    )
    memories: Mapped[list["CreativeMemory"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    plans: Mapped[list["WorkflowPlan"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class AgentMessage(Base):
    __tablename__ = "agent_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[ChatMessageRole] = mapped_column(Enum(ChatMessageRole), index=True)
    content: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    session: Mapped[AgentSession] = relationship(back_populates="messages")


class CreativeMemory(Base):
    __tablename__ = "creative_memories"
    __table_args__ = (
        UniqueConstraint("session_id", "category", "key", name="uq_memory_session_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    category: Mapped[str] = mapped_column(String(32), index=True)
    key: Mapped[str] = mapped_column(String(100))
    value: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="inferred", index=True)
    source_type: Mapped[str] = mapped_column(String(32), default="conversation")
    source_reference: Mapped[str | None] = mapped_column(String(500), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    session: Mapped[AgentSession] = relationship(back_populates="memories")


class WorkflowPlan(Base):
    __tablename__ = "workflow_plans"
    __table_args__ = (UniqueConstraint("session_id", "version", name="uq_plan_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    project_spec: Mapped[dict] = mapped_column(JSON, default=dict)
    assumptions: Mapped[list] = mapped_column(JSON, default=list)
    missing_information: Mapped[list] = mapped_column(JSON, default=list)
    stages: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    session: Mapped[AgentSession] = relationship(back_populates="plans")
    tasks: Mapped[list["WorkflowTask"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan", order_by="WorkflowTask.sequence"
    )


class WorkflowTask(Base):
    __tablename__ = "workflow_tasks"
    __table_args__ = (UniqueConstraint("plan_id", "sequence", name="uq_task_sequence"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_plans.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    stage: Mapped[str] = mapped_column(String(64), index=True)
    operation: Mapped[str] = mapped_column(String(64))
    status: Mapped[WorkflowTaskStatus] = mapped_column(
        Enum(WorkflowTaskStatus), default=WorkflowTaskStatus.pending, index=True
    )
    input_data: Mapped[dict] = mapped_column(JSON, default=dict)
    result_data: Mapped[dict] = mapped_column(JSON, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    idempotency_key: Mapped[str] = mapped_column(String(100), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    plan: Mapped[WorkflowPlan] = relationship(back_populates="tasks")


class ScriptDocument(Base):
    __tablename__ = "script_documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    script_version_id: Mapped[str] = mapped_column(
        ForeignKey("script_versions.id", ondelete="CASCADE"), unique=True, index=True
    )
    project_id: Mapped[str] = mapped_column(String(36), index=True)
    version: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ScriptChunk(Base):
    __tablename__ = "script_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "sequence", name="uq_document_chunk_sequence"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("script_documents.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    token_count: Mapped[int] = mapped_column(Integer)
    chapter: Mapped[str] = mapped_column(String(200), default="")
    scene: Mapped[str] = mapped_column(String(300), default="")
    characters: Mapped[list] = mapped_column(JSON, default=list)
    locations: Mapped[list] = mapped_column(JSON, default=list)
    start_offset: Mapped[int] = mapped_column(Integer)
    end_offset: Mapped[int] = mapped_column(Integer)
    previous_chunk_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    next_chunk_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ScriptSummary(Base):
    __tablename__ = "script_summaries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("script_documents.id", ondelete="CASCADE"), index=True
    )
    level: Mapped[str] = mapped_column(String(32), index=True)
    scope_key: Mapped[str] = mapped_column(String(300), default="")
    content: Mapped[str] = mapped_column(Text)
    source_chunk_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EmbeddingJob(Base):
    __tablename__ = "embedding_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("script_documents.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    model: Mapped[str] = mapped_column(String(200))
    processed_count: Mapped[int] = mapped_column(Integer, default=0)
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ResearchSource(Base):
    __tablename__ = "research_sources"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="CASCADE"), index=True
    )
    query: Mapped[str] = mapped_column(Text, default="")
    title: Mapped[str] = mapped_column(String(500))
    url: Mapped[str] = mapped_column(String(2000))
    summary: Mapped[str] = mapped_column(Text, default="")
    adoption_reason: Mapped[str] = mapped_column(Text, default="")
    fetch_method: Mapped[str] = mapped_column(String(64), default="local")
    adopted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    accessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
