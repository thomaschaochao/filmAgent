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
