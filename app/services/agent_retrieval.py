from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AgentSession,
    EmbeddingJob,
    ResearchSource,
    ScriptChunk,
    ScriptDocument,
)
from app.schemas import RetrievalQuery
from app.services.rag import LocalRAG


def get_retrieval_status_snapshot(db: Session) -> dict:
    rag_status = LocalRAG().status()
    latest_job = db.scalar(select(EmbeddingJob).order_by(EmbeddingJob.updated_at.desc()))
    rag_status["index_status"] = latest_job.status if latest_job else rag_status["index_status"]
    rag_status["pending_jobs"] = db.scalar(
        select(func.count(EmbeddingJob.id)).where(
            EmbeddingJob.status.in_(["pending", "chunking", "embedding", "indexing"])
        )
    ) or 0
    rag_status["failed_jobs"] = db.scalar(
        select(func.count(EmbeddingJob.id)).where(EmbeddingJob.status == "failed")
    ) or 0
    return rag_status


def retrieve_context(payload: RetrievalQuery, db: Session) -> list[dict]:
    statement = select(ScriptChunk).join(
        ScriptDocument, ScriptDocument.id == ScriptChunk.document_id
    )
    if payload.project_id:
        statement = statement.where(ScriptDocument.project_id == payload.project_id)
    if payload.script_id:
        statement = statement.where(ScriptDocument.script_version_id == payload.script_id)
    else:
        statement = statement.where(ScriptDocument.is_current.is_(True))
    chunks = list(db.scalars(statement).all())
    research_statement = select(ResearchSource).where(ResearchSource.adopted.is_(True))
    if payload.project_id:
        session_ids = select(AgentSession.id).where(AgentSession.project_id == payload.project_id)
        research_statement = research_statement.where(ResearchSource.session_id.in_(session_ids))
    research_sources = list(db.scalars(research_statement).all())
    if not chunks and not research_sources:
        return []
    vector_script_id = payload.script_id
    if vector_script_id is None and chunks:
        current_document = db.get(ScriptDocument, chunks[0].document_id)
        vector_script_id = current_document.script_version_id if current_document else None

    vector_scores: dict[str, float] = {}
    rag = LocalRAG()
    if rag.status()["available"]:
        try:
            vector_scores = {
                item["chunk_id"]: item["score"]
                for item in rag.hybrid_search(
                    payload.query,
                    project_id=payload.project_id,
                    script_id=vector_script_id,
                    limit=20,
                )
            }
        except Exception:
            vector_scores = {}

    terms = [
        term.casefold()
        for term in re.findall(r"[\u3400-\u9fff]{2,}|[A-Za-z0-9_]+", payload.query)
    ]
    ranked = []
    for chunk in chunks:
        haystack = f"{chunk.chapter} {chunk.scene} {chunk.content}".casefold()
        lexical = sum(haystack.count(term) for term in terms)
        vector = vector_scores.get(chunk.id, 0.0)
        if lexical or vector:
            ranked.append(
                (
                    vector + min(lexical, 10) * 0.1,
                    lexical,
                    {
                        "chunk_id": chunk.id,
                        "content": chunk.content,
                        "source": (
                            "hybrid"
                            if chunk.id in vector_scores and lexical
                            else ("vector" if chunk.id in vector_scores else "keyword")
                        ),
                        "chapter": chunk.chapter,
                        "scene": chunk.scene,
                        "characters": chunk.characters,
                        "sequence": chunk.sequence,
                    },
                )
            )
    for source in research_sources:
        haystack = f"{source.title} {source.query} {source.summary}".casefold()
        lexical = sum(haystack.count(term) for term in terms)
        vector = vector_scores.get(source.id, 0.0)
        if lexical or vector:
            ranked.append(
                (
                    vector + min(lexical, 10) * 0.1,
                    lexical,
                    {
                        "chunk_id": source.id,
                        "content": source.summary,
                        "source": "research" if source.id not in vector_scores else "hybrid",
                        "chapter": "research",
                        "scene": source.title,
                        "characters": [],
                        "sequence": 0,
                    },
                )
            )
    ranked.sort(key=lambda item: (item[0], item[1], -item[2]["sequence"]), reverse=True)
    return [
        {
            "chunk_id": item["chunk_id"],
            "content": item["content"],
            "score": round(score, 6),
            "source": item["source"],
            "chapter": item["chapter"],
            "scene": item["scene"],
            "characters": item["characters"],
        }
        for score, _lexical, item in ranked[: payload.limit]
    ]
