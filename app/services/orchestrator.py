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


@dataclass
class ConversationExtraction:
    facts: dict[str, str]
    reply: str | None
    reply_options: list[dict[str, object]]


FALLBACK_OPTION_PROFILES = {
    "theater": [
        {
            "name": "后台灯下",
            "visual_style": "现实主义戏剧影像",
            "world_setting": "当代剧场后台",
            "aspect_ratio": "16:9",
        },
        {
            "name": "开演之前",
            "visual_style": "舞台感电影光影",
            "world_setting": "现代小剧场与后台空间",
            "aspect_ratio": "2.39:1",
        },
        {
            "name": "剧场暗门",
            "visual_style": "自然主义生活质感",
            "world_setting": "当代城市剧院环境",
            "aspect_ratio": "16:9",
        },
    ],
    "aviation": [
        {
            "name": "暴雨返航",
            "visual_style": "电影感写实",
            "world_setting": "当代机场与暴雨夜空",
            "aspect_ratio": "16:9",
        },
        {
            "name": "最后进近",
            "visual_style": "紧张灾难片质感",
            "world_setting": "现代民航机场与驾驶舱",
            "aspect_ratio": "2.39:1",
        },
        {
            "name": "雨夜塔台",
            "visual_style": "冷峻纪实影像",
            "world_setting": "雨夜机场跑道与塔台周边",
            "aspect_ratio": "16:9",
        },
    ],
    "default": [
        {
            "name": "镜头之外",
            "visual_style": "电影感写实",
            "world_setting": "当代城市环境",
            "aspect_ratio": "16:9",
        },
        {
            "name": "暗场之前",
            "visual_style": "戏剧化电影光影",
            "world_setting": "近现代室内外生活空间",
            "aspect_ratio": "2.39:1",
        },
        {
            "name": "另一种日常",
            "visual_style": "自然主义生活质感",
            "world_setting": "架空但现实可信的故事世界",
            "aspect_ratio": "16:9",
        },
    ],
}

INTERNAL_OPTION_PHRASES = (
    "沿用剧本",
    "不额外预设",
    "暂不指定",
    "先保留为用户",
    "由当前剧本语境决定",
    "缺失地点",
    "继续向用户追问",
)

PLACEHOLDER_PROJECT_NAMES = {"新的影片计划", "总控智能体", "未命名影片", "片名待定"}

FACT_CHIP_LABELS = {
    "name": "项目",
    "visual_style": "风格",
    "world_setting": "世界",
    "aspect_ratio": "画幅",
    "language": "语言",
}


def _fact_chips(facts: dict[str, str]) -> list[dict[str, str]]:
    return [
        {"key": key, "label": FACT_CHIP_LABELS.get(key, key), "value": value}
        for key, value in facts.items()
        if value
    ]


def _custom_option(missing_keys: list[str]) -> dict[str, object]:
    missing = "、".join(REQUIRED_FACTS[key] for key in missing_keys) or "补充信息"
    return {
        "label": "其它想法",
        "custom": True,
        "placeholder": f"请输入{missing}，也可以同时补充其它设定……",
    }


def _needs_web_research(content: str) -> bool:
    keywords = (
        "联网",
        "搜索",
        "查一下",
        "查资料",
        "真实资料",
        "现实资料",
        "参考资料",
        "新闻",
        "历史资料",
        "法规",
        "web",
        "search",
        "research",
    )
    lowered = content.lower()
    return any(keyword in lowered for keyword in keywords)


def _web_research_options(query: str) -> list[dict[str, object]]:
    return [
        {
            "label": "联网搜索资料",
            "action": "research_web",
            "query": query[:1000],
            "description": "搜索公开资料，结果先只进入当前对话供你确认。",
        },
        {
            "label": "先不用联网，按剧本继续",
            "content": "先不用联网，按当前剧本和已确认设定继续。",
            "facts": {},
            "description": "跳过外部资料，不影响后续制作流程。",
        },
    ]


def _plan_ready_options() -> list[dict[str, object]]:
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


def _plan_approval_options(plan_id: str) -> list[dict[str, object]]:
    return [
        {
            "label": "立即执行计划",
            "action": "approve_plan",
            "plan_id": plan_id,
            "description": "创建项目与剧本，并进入资产提取阶段。",
        },
        {
            "label": "稍后执行",
            "action": "defer_plan",
            "description": "保留当前计划，之后可回到这个对话继续执行。",
        },
    ]


def _fallback_profile_key(session: AgentSession) -> str:
    context = f"{session.title or ''}\n{session.original_input or ''}".lower()
    if any(token in context for token in ("戏剧", "剧场", "舞台", "后台", "theater", "theatre")):
        return "theater"
    aviation_tokens = ("飞行", "飞行员", "飞机", "机场", "跑道", "aviation", "pilot")
    if any(token in context for token in aviation_tokens):
        return "aviation"
    return "default"


def _session_title_is_placeholder(title: str | None) -> bool:
    return not title or title.strip() in PLACEHOLDER_PROJECT_NAMES


def _is_valid_required_fact(key: str, value: str | None) -> bool:
    if not value or not str(value).strip():
        return False
    if key == "name" and str(value).strip() in PLACEHOLDER_PROJECT_NAMES:
        return False
    return True


def _is_user_facing_fact_value(value: str) -> bool:
    return not any(phrase in value for phrase in INTERNAL_OPTION_PHRASES)


def _fallback_reply_options(
    session: AgentSession, missing_keys: list[str]
) -> list[dict[str, object]]:
    if not missing_keys:
        return _plan_ready_options()
    options: list[dict[str, object]] = []
    profiles = FALLBACK_OPTION_PROFILES[_fallback_profile_key(session)]
    for index, profile in enumerate(profiles, start=1):
        facts = {
            key: (
                session.title
                if key == "name" and not _session_title_is_placeholder(session.title)
                else profile.get(key, "")
            )
            for key in missing_keys
        }
        facts = {
            key: value
            for key, value in facts.items()
            if value and _is_user_facing_fact_value(str(value))
        }
        label_parts = [
            facts.get("visual_style"),
            facts.get("world_setting"),
            facts.get("aspect_ratio"),
        ]
        label = " / ".join(part for part in label_parts if part) or f"方案{index}"
        options.append(
            {
                "label": f"选项{index}：{label}",
                "content": "我选择这套设定：" + "；".join(
                    f"{FACT_CHIP_LABELS.get(key, key)}：{value}" for key, value in facts.items()
                ),
                "facts": facts,
                "description": "适合作为当前创作方向，之后仍可继续细调。",
                "fact_chips": _fact_chips(facts),
                "source": "fallback",
            }
        )
    options.append(_custom_option(missing_keys))
    return options


def _validated_ai_reply_options(
    raw_options: object, missing_keys: list[str]
) -> list[dict[str, object]]:
    if not missing_keys or not isinstance(raw_options, list):
        return []
    required = set(missing_keys)
    seen: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    options: list[dict[str, object]] = []
    for raw in raw_options:
        if not isinstance(raw, dict):
            continue
        facts = raw.get("facts")
        if not isinstance(facts, dict):
            continue
        safe_facts = {
            key: str(value).strip()
            for key, value in facts.items()
            if key in required
            and str(value).strip()
            and _is_valid_required_fact(key, str(value).strip())
            and _is_user_facing_fact_value(str(value).strip())
        }
        if not required.issubset(safe_facts):
            continue
        label = str(raw.get("label", "")).strip()
        content = str(raw.get("content", "")).strip()
        if not label or not content:
            continue
        fingerprint = (label.casefold(), tuple(sorted(safe_facts.items())))
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        options.append(
            {
                "label": label,
                "content": content,
                "facts": safe_facts,
                "description": str(raw.get("description", "")).strip(),
                "fact_chips": _fact_chips(safe_facts),
                "source": "ai",
            }
        )
        if len(options) >= 3:
            break
    return options if len(options) >= 2 else []


def _reply_options(
    session: AgentSession,
    missing_keys: list[str],
    ai_reply_options: list[dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    if not missing_keys:
        return _plan_ready_options()
    options = _validated_ai_reply_options(ai_reply_options or [], missing_keys)
    if not options:
        options = _fallback_reply_options(session, missing_keys)[:-1]
    options.append(_custom_option(missing_keys))
    return options


def _message_metadata(
    missing_keys: list[str],
    session: AgentSession,
    ai_reply_options: list[dict[str, object]] | None = None,
    *,
    research_query: str | None = None,
) -> dict[str, object]:
    missing = [REQUIRED_FACTS[key] for key in missing_keys]
    reply_options = _reply_options(session, missing_keys, ai_reply_options)
    if research_query:
        reply_options = _web_research_options(research_query) + reply_options
    return {
        "missing_information": missing,
        "missing_keys": missing_keys,
        "option_mode": "clarify" if missing_keys else "plan_ready",
        "reply_options": reply_options,
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
    for style in ("写实", "动画", "水墨", "赛博朋克", "黑白", "电影感"):
        if style in content:
            facts["visual_style"] = style
            break
    return facts


def _extract_with_ai(session: AgentSession, content: str) -> ConversationExtraction:
    if not get_settings().master_agent_ai_enabled:
        return ConversationExtraction(facts={}, reply=None, reply_options=[])
    history = [
        {"role": message.role.value, "content": message.content}
        for message in session.messages[-12:]
    ]
    memory = {item.key: item.value for item in session.memories if item.status != "superseded"}
    system = """You are FilmPilot's master pre-production agent. Return one JSON object only.
Extract only information supported by the user's message. `facts` may contain name,
visual_style, world_setting, aspect_ratio, language, genre, tone, audience.
Do not invent missing facts. `reply` should briefly acknowledge the request and ask about
important missing production information. Also return `reply_options`: 2-3 distinct options
that directly answer your clarification question. Every option must include label, content,
facts, and description. If multiple production facts are missing, each option should fill all
missing facts together. Do not ask the user to choose initial-frame or storyboard mode during
project discovery; that choice is made later per shot based on shot complexity. Do not reuse
setting details from other projects or sessions. Never claim that work has been executed."""
    try:
        result = DeepSeekClient().chat_json(
            system,
            str({"confirmed_memory": memory, "recent_messages": history, "message": content}),
        )
    except (DeepSeekError, RuntimeError):
        return ConversationExtraction(facts={}, reply=None, reply_options=[])
    facts = result.get("facts") if isinstance(result.get("facts"), dict) else {}
    safe_facts = {
        key: str(value).strip()
        for key, value in facts.items()
        if key in FACT_CATEGORIES and key != "prompt_mode" and str(value).strip()
    }
    reply = result.get("reply")
    reply_options = result.get("reply_options")
    return ConversationExtraction(
        facts=safe_facts,
        reply=str(reply).strip() if reply else None,
        reply_options=reply_options if isinstance(reply_options, list) else [],
    )


def continue_conversation(
    db: Session, session: AgentSession, content: str, explicit_facts: dict[str, str]
) -> ConversationResult:
    db.add(AgentMessage(session_id=session.id, role=ChatMessageRole.user, content=content))
    if not session.original_input:
        session.original_input = content
    extraction = _extract_with_ai(session, content)
    inferred_facts = {**_infer_facts(content), **extraction.facts}
    for key, value in inferred_facts.items():
        if value.strip() and key not in explicit_facts:
            upsert_memory(
                db, session, key, value.strip(), status="inferred", source_type="assistant"
            )
    for key, value in explicit_facts.items():
        if (
            key in FACT_CATEGORIES
            and key != "prompt_mode"
            and value.strip()
            and _is_valid_required_fact(key, value)
        ):
            upsert_memory(db, session, key, value.strip())
    current = {
        memory.key: memory.value
        for memory in session.memories
        if memory.status != "superseded"
    }
    current.update(
        {
            key: value
            for key, value in explicit_facts.items()
            if key in FACT_CATEGORIES
            and key != "prompt_mode"
            and value.strip()
            and _is_valid_required_fact(key, value)
        }
    )
    missing_keys = [
        key for key in REQUIRED_FACTS if not _is_valid_required_fact(key, current.get(key))
    ]
    missing = [REQUIRED_FACTS[key] for key in missing_keys]
    if missing:
        session.status = AgentSessionStatus.clarifying
        reply = extraction.reply or "在生成制作计划前，还需要确认：" + "、".join(missing) + "。"
    else:
        session.status = AgentSessionStatus.plan_ready
        reply = "关键信息已经齐全。我可以生成完整制作计划，确认后再分阶段执行。"
    db.add(
        AgentMessage(
            session_id=session.id,
            role=ChatMessageRole.assistant,
            content=reply,
            metadata_json=_message_metadata(
                missing_keys,
                session,
                extraction.reply_options,
                research_query=content if _needs_web_research(content) else None,
            ),
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
    db.add(
        AgentMessage(
            session_id=session.id,
            role=ChatMessageRole.assistant,
            content=(
                f"完整制作计划 V{plan.version} 已生成。"
                "要现在执行吗？立即执行会创建项目与剧本，之后继续逐阶段确认资产、分镜和提示词。"
            ),
            metadata_json={
                "option_mode": "plan_approval",
                "plan_id": plan.id,
                "reply_options": _plan_approval_options(plan.id),
            },
        )
    )
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
