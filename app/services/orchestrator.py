from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models import (
    AgentMessage,
    AgentSession,
    AgentSessionStatus,
    ChatMessageRole,
    CreativeMemory,
    EmbeddingJob,
    ScriptChunk,
    ScriptDocument,
    ScriptVersion,
    WorkflowPlan,
    WorkflowTask,
    WorkflowTaskStatus,
)
from app.services.rag import chunk_script

REQUIRED_FACTS = {
    "name": "项目名称",
    "visual_style": "整体视觉风格",
    "world_setting": "故事发生的时代和地点",
    "aspect_ratio": "输出画幅",
    "prompt_mode": "镜头提示词模式（首帧或故事板）",
}

FACT_CATEGORIES = {
    "name": "production",
    "visual_style": "visual",
    "world_setting": "world",
    "aspect_ratio": "production",
    "language": "production",
    "prompt_mode": "production",
    "genre": "story_core",
    "tone": "story_core",
    "audience": "constraint",
}

STAGES = [
    {"key": "project_script", "label": "创建项目与剧本"},
    {"key": "assets", "label": "提取资产并生成提示词"},
    {"key": "shots", "label": "拆分分镜"},
    {"key": "prompts", "label": "生成镜头提示词"},
    {"key": "images", "label": "提议图片生成批次", "optional": True},
]


@dataclass
class ConversationResult:
    reply: str
    missing: list[str]


def upsert_memory(db: Session, session: AgentSession, key: str, value: str) -> CreativeMemory:
    memory = db.scalar(
        select(CreativeMemory).where(
            CreativeMemory.session_id == session.id,
            CreativeMemory.category == FACT_CATEGORIES.get(key, "decision"),
            CreativeMemory.key == key,
        )
    )
    if memory is None:
        memory = CreativeMemory(
            session_id=session.id,
            project_id=session.project_id,
            category=FACT_CATEGORIES.get(key, "decision"),
            key=key,
            value=value,
            status="confirmed",
            source_type="user",
        )
        db.add(memory)
    else:
        memory.value = value
        memory.status = "confirmed"
        memory.source_type = "user"
    return memory


def _infer_facts(content: str) -> dict[str, str]:
    facts: dict[str, str] = {}
    ratios = re.search(r"\b(16:9|2\.39:1|4:3|9:16|1:1)\b", content)
    if ratios:
        facts["aspect_ratio"] = ratios.group(1)
    if "故事板" in content:
        facts["prompt_mode"] = "storyboard"
    elif "首帧" in content:
        facts["prompt_mode"] = "initial_frame"
    for style in ("写实", "动画", "水墨", "赛博朋克", "黑白", "电影感"):
        if style in content:
            facts["visual_style"] = style
            break
    return facts


def continue_conversation(
    db: Session, session: AgentSession, content: str, explicit_facts: dict[str, str]
) -> ConversationResult:
    db.add(AgentMessage(session_id=session.id, role=ChatMessageRole.user, content=content))
    if not session.original_input:
        session.original_input = content
    for key, value in {**_infer_facts(content), **explicit_facts}.items():
        if value.strip():
            upsert_memory(db, session, key, value.strip())
    current = {
        memory.key: memory.value
        for memory in session.memories
        if memory.status != "superseded"
    }
    current.update({key: value for key, value in explicit_facts.items() if value.strip()})
    missing = [label for key, label in REQUIRED_FACTS.items() if not current.get(key)]
    if missing:
        session.status = AgentSessionStatus.clarifying
        reply = "在生成制作计划前，还需要确认：" + "、".join(missing) + "。"
    else:
        session.status = AgentSessionStatus.plan_ready
        reply = "关键信息已经齐全。我可以生成完整制作计划，确认后再分阶段执行。"
    db.add(
        AgentMessage(
            session_id=session.id,
            role=ChatMessageRole.assistant,
            content=reply,
            metadata_json={"missing_information": missing},
        )
    )
    db.commit()
    return ConversationResult(reply=reply, missing=missing)


def create_plan(
    db: Session, session: AgentSession, project_spec: dict, assumptions: list[str]
) -> WorkflowPlan:
    memory = {item.key: item.value for item in session.memories if item.status != "superseded"}
    spec = {
        "name": memory.get("name", session.title),
        "description": session.original_input[:1000],
        "visual_style": memory.get("visual_style", "电影感写实"),
        "world_setting": memory.get("world_setting", "待用户确认"),
        "aspect_ratio": memory.get("aspect_ratio", "16:9"),
        "language": memory.get("language", "zh-CN"),
        "prompt_mode": memory.get("prompt_mode", "initial_frame"),
        "script_content": session.original_input,
        **project_spec,
    }
    missing = [label for key, label in REQUIRED_FACTS.items() if not spec.get(key)]
    latest = db.scalar(
        select(func.max(WorkflowPlan.version)).where(WorkflowPlan.session_id == session.id)
    )
    plan = WorkflowPlan(
        session_id=session.id,
        version=(latest or 0) + 1,
        project_spec=spec,
        assumptions=assumptions,
        missing_information=missing,
        stages=STAGES,
        status="ready" if not missing else "draft",
    )
    db.add(plan)
    db.flush()
    for sequence, stage in enumerate(STAGES, start=1):
        db.add(
            WorkflowTask(
                plan_id=plan.id,
                sequence=sequence,
                stage=stage["key"],
                operation=stage["key"],
                status=WorkflowTaskStatus.awaiting_approval,
                idempotency_key=f"{plan.id}:{stage['key']}",
            )
        )
    session.status = AgentSessionStatus.awaiting_approval
    session.current_stage = "plan_approval"
    db.commit()
    db.refresh(plan)
    return plan


def index_script(db: Session, script: ScriptVersion) -> tuple[ScriptDocument, EmbeddingJob]:
    digest = hashlib.sha256(script.content.encode("utf-8")).hexdigest()
    existing = db.scalar(
        select(ScriptDocument).where(ScriptDocument.script_version_id == script.id)
    )
    if existing:
        job = db.scalar(
            select(EmbeddingJob)
            .where(EmbeddingJob.document_id == existing.id)
            .order_by(EmbeddingJob.created_at.desc())
        )
        if job:
            return existing, job
    document = ScriptDocument(
        script_version_id=script.id,
        project_id=script.project_id,
        version=script.version,
        content_hash=digest,
        status="chunking",
    )
    db.add(document)
    db.flush()
    drafts = chunk_script(script.content)
    chunks = []
    for sequence, draft in enumerate(drafts, start=1):
        chunk = ScriptChunk(
            document_id=document.id,
            sequence=sequence,
            content=draft.content,
            content_hash=draft.content_hash,
            token_count=draft.token_count,
            chapter=draft.chapter,
            scene=draft.scene,
            characters=draft.characters,
            locations=draft.locations,
            start_offset=draft.start_offset,
            end_offset=draft.end_offset,
        )
        db.add(chunk)
        chunks.append(chunk)
    db.flush()
    try:
        db.execute(
            text(
                "CREATE VIRTUAL TABLE IF NOT EXISTS script_chunks_fts "
                "USING fts5(chunk_id UNINDEXED, content, chapter, scene)"
            )
        )
        for chunk in chunks:
            db.execute(
                text(
                    "INSERT INTO script_chunks_fts(chunk_id, content, chapter, scene) "
                    "VALUES (:chunk_id, :content, :chapter, :scene)"
                ),
                {
                    "chunk_id": chunk.id,
                    "content": chunk.content,
                    "chapter": chunk.chapter,
                    "scene": chunk.scene,
                },
            )
    except SQLAlchemyError:
        # SQLite builds without FTS5 retain the regular-table keyword fallback.
        pass
    for index, chunk in enumerate(chunks):
        chunk.previous_chunk_id = chunks[index - 1].id if index else None
        chunk.next_chunk_id = chunks[index + 1].id if index + 1 < len(chunks) else None
    document.status = "pending_embedding"
    job = EmbeddingJob(
        document_id=document.id,
        status="pending",
        model="BAAI/bge-m3",
        total_count=len(chunks),
    )
    db.add(job)
    db.commit()
    db.refresh(document)
    db.refresh(job)
    return document, job
