from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import get_settings
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
from app.services.crew import crew_plan_metadata
from app.services.deepseek import DeepSeekClient, DeepSeekError
from app.services.rag import chunk_script

REQUIRED_FACTS = {
    "name": "项目名称",
    "visual_style": "整体视觉风格",
    "world_setting": "故事发生的时代和地点",
    "aspect_ratio": "输出画幅",
    "prompt_mode": "镜头提示词模式（首帧或故事板）",
}

FACT_OPTION_PRESETS = {
    "name": [
        {"label": "使用当前会话标题", "facts": {"name": ""}},
        {"label": "先用“未命名影片项目”", "facts": {"name": "未命名影片项目"}},
    ],
    "visual_style": [
        {"label": "电影感写实", "facts": {"visual_style": "电影感写实"}},
        {"label": "高概念科幻写实", "facts": {"visual_style": "高概念科幻写实"}},
        {"label": "动画电影风格", "facts": {"visual_style": "动画电影风格"}},
    ],
    "world_setting": [
        {"label": "当代美国城市", "facts": {"world_setting": "当代美国城市"}},
        {"label": "近未来亚洲都市", "facts": {"world_setting": "近未来亚洲都市"}},
        {"label": "架空世界，地点待细化", "facts": {"world_setting": "架空世界，地点待细化"}},
    ],
    "aspect_ratio": [
        {"label": "16:9 横屏", "facts": {"aspect_ratio": "16:9"}},
        {"label": "2.39:1 宽银幕", "facts": {"aspect_ratio": "2.39:1"}},
        {"label": "9:16 竖屏", "facts": {"aspect_ratio": "9:16"}},
    ],
    "prompt_mode": [
        {"label": "首帧模式", "facts": {"prompt_mode": "initial_frame"}},
        {"label": "故事板模式", "facts": {"prompt_mode": "storyboard"}},
        {"label": "由智能体按镜头复杂度决定", "facts": {"prompt_mode": "auto"}},
    ],
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


def _reply_options(
    session: AgentSession, missing_keys: list[str]
) -> list[dict[str, object]]:
    if not missing_keys:
        return [
            {
                "label": "生成完整制作计划",
                "action": "generate_plan",
                "description": "先生成可确认的阶段计划，不会立即创建项目。",
            },
            {
                "label": "我还要补充设定",
                "custom": True,
                "placeholder": "继续补充风格、地点、角色关系或制作偏好……",
            },
        ]
    key = missing_keys[0]
    presets = FACT_OPTION_PRESETS.get(key, [])
    options: list[dict[str, object]] = []
    for index, preset in enumerate(presets, start=1):
        facts = dict(preset.get("facts", {}))
        if key == "name" and facts.get("name") == "":
            facts["name"] = session.title
        label = str(preset.get("label", "选项"))
        options.append(
            {
                "label": f"选项{index}：{label}",
                "content": f"我选择{label}",
                "facts": facts,
                "description": preset.get("description", ""),
            }
        )
    options.append(
        {
            "label": "其它想法",
            "custom": True,
            "placeholder": f"请输入{REQUIRED_FACTS.get(key, '补充信息')}，也可以同时补充其它设定……",
        }
    )
    return options


def _message_metadata(missing_keys: list[str], session: AgentSession) -> dict[str, object]:
    missing = [REQUIRED_FACTS[key] for key in missing_keys]
    return {
        "missing_information": missing,
        "missing_keys": missing_keys,
        "option_mode": "clarify" if missing_keys else "plan_ready",
        "reply_options": _reply_options(session, missing_keys),
    }


def upsert_memory(
    db: Session,
    session: AgentSession,
    key: str,
    value: str,
    *,
    status: str = "confirmed",
    source_type: str = "user",
) -> CreativeMemory:
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
            status=status,
            source_type=source_type,
        )
        db.add(memory)
    else:
        memory.value = value
        memory.status = status
        memory.source_type = source_type
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


def _extract_with_ai(session: AgentSession, content: str) -> tuple[dict[str, str], str | None]:
    if not get_settings().master_agent_ai_enabled:
        return {}, None
    history = [
        {"role": message.role.value, "content": message.content}
        for message in session.messages[-12:]
    ]
    memory = {item.key: item.value for item in session.memories if item.status != "superseded"}
    system = """You are FilmPilot's master pre-production agent. Return one JSON object only.
Extract only information supported by the user's message. `facts` may contain name,
visual_style, world_setting, aspect_ratio, language, prompt_mode, genre, tone, audience.
Do not invent missing facts. `reply` should briefly acknowledge the request and ask about
important missing production information. Never claim that work has been executed."""
    try:
        result = DeepSeekClient().chat_json(
            system,
            str({"confirmed_memory": memory, "recent_messages": history, "message": content}),
        )
    except (DeepSeekError, RuntimeError):
        return {}, None
    facts = result.get("facts") if isinstance(result.get("facts"), dict) else {}
    safe_facts = {
        key: str(value).strip()
        for key, value in facts.items()
        if key in FACT_CATEGORIES and str(value).strip()
    }
    reply = result.get("reply")
    return safe_facts, str(reply).strip() if reply else None


def continue_conversation(
    db: Session, session: AgentSession, content: str, explicit_facts: dict[str, str]
) -> ConversationResult:
    db.add(AgentMessage(session_id=session.id, role=ChatMessageRole.user, content=content))
    if not session.original_input:
        session.original_input = content
    ai_facts, ai_reply = _extract_with_ai(session, content)
    inferred_facts = {**_infer_facts(content), **ai_facts}
    for key, value in inferred_facts.items():
        if value.strip() and key not in explicit_facts:
            upsert_memory(
                db, session, key, value.strip(), status="inferred", source_type="assistant"
            )
    for key, value in explicit_facts.items():
        if value.strip():
            upsert_memory(db, session, key, value.strip())
    current = {
        memory.key: memory.value
        for memory in session.memories
        if memory.status != "superseded"
    }
    current.update({key: value for key, value in explicit_facts.items() if value.strip()})
    missing_keys = [key for key in REQUIRED_FACTS if not current.get(key)]
    missing = [REQUIRED_FACTS[key] for key in missing_keys]
    if missing:
        session.status = AgentSessionStatus.clarifying
        reply = ai_reply or "在生成制作计划前，还需要确认：" + "、".join(missing) + "。"
    else:
        session.status = AgentSessionStatus.plan_ready
        reply = "关键信息已经齐全。我可以生成完整制作计划，确认后再分阶段执行。"
    db.add(
        AgentMessage(
            session_id=session.id,
            role=ChatMessageRole.assistant,
            content=reply,
            metadata_json=_message_metadata(missing_keys, session),
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
        "agent_runtime": crew_plan_metadata(),
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
