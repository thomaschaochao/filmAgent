from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings

HEADING_RE = re.compile(
    r"^(?:第[一二三四五六七八九十百零\d]+[章节幕卷]|(?:INT\.?|EXT\.?|内景|外景)[ .、：:].+)$",
    re.IGNORECASE,
)
CHARACTER_RE = re.compile(r"^[\u4e00-\u9fffA-Z][\u4e00-\u9fffA-Z0-9· ._-]{0,24}[：:]$")


@dataclass
class ChunkDraft:
    content: str
    start_offset: int
    end_offset: int
    chapter: str = ""
    scene: str = ""
    characters: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()

    @property
    def token_count(self) -> int:
        return estimate_tokens(self.content)


def estimate_tokens(text: str) -> int:
    chinese = len(re.findall(r"[\u3400-\u9fff]", text))
    other = len(re.findall(r"[A-Za-z0-9_]+|[^\s\u3400-\u9fff]", text))
    return chinese + other


def _semantic_blocks(text: str) -> list[tuple[str, int, int]]:
    blocks: list[tuple[str, int, int]] = []
    for match in re.finditer(r"\S(?:.*?\S)?(?=\n\s*\n|\Z)", text, re.DOTALL):
        raw = match.group(0).strip()
        if raw:
            blocks.append((raw, match.start(), match.end()))
    return blocks or ([(text.strip(), 0, len(text))] if text.strip() else [])


def chunk_script(text: str, target_tokens: int = 1000, hard_limit: int = 1600) -> list[ChunkDraft]:
    """Split at screenplay structure and paragraph boundaries, never inside a semantic block."""
    blocks = _semantic_blocks(text)
    chunks: list[ChunkDraft] = []
    pending: list[tuple[str, int, int]] = []
    pending_tokens = 0
    chapter = ""
    scene = ""

    def flush() -> None:
        nonlocal pending, pending_tokens
        if not pending:
            return
        content = "\n\n".join(block[0] for block in pending)
        character_names = []
        for line in content.splitlines():
            clean = line.strip()
            if CHARACTER_RE.match(clean):
                character_names.append(clean.rstrip("：:"))
        chunks.append(
            ChunkDraft(
                content=content,
                start_offset=pending[0][1],
                end_offset=pending[-1][2],
                chapter=chapter,
                scene=scene,
                characters=list(dict.fromkeys(character_names)),
            )
        )
        pending = []
        pending_tokens = 0

    for block in blocks:
        first_line = block[0].splitlines()[0].strip()
        is_heading = bool(HEADING_RE.match(first_line))
        if is_heading and pending:
            flush()
        if re.match(r"^第.+[章节幕卷]$", first_line):
            chapter = first_line
        elif is_heading:
            scene = first_line
        block_tokens = estimate_tokens(block[0])
        if pending and (
            pending_tokens + block_tokens > hard_limit or pending_tokens >= target_tokens
        ):
            flush()
        pending.append(block)
        pending_tokens += block_tokens
    flush()
    return chunks


class LocalRAG:
    """Local BGE-M3/Qdrant adapter.

    SQLite remains the source of truth. Qdrant stores only vectors and rebuildable metadata;
    callers must use chunk ids returned from search to read authoritative text from SQLite.
    """

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._model: Any = None
        self._client: Any = None

    def status(self) -> dict[str, Any]:
        dependencies = True
        qdrant_local = True
        error = None
        try:
            import FlagEmbedding  # noqa: F401
        except ImportError:
            dependencies = False
            error = "Local RAG dependencies are not installed. Install the 'rag' extra."
        try:
            import qdrant_client  # noqa: F401
        except ImportError:
            qdrant_local = False
            error = "Qdrant Local dependency is not installed. Install the 'rag' extra."
        model_path = self._path(self.settings.embedding_model_path)
        available = (
            self.settings.embedding_enabled
            and dependencies
            and qdrant_local
            and model_path.exists()
        )
        return {
            "enabled": self.settings.embedding_enabled,
            "configured": dependencies,
            "available": available,
            "vector_backend": "qdrant_local",
            "qdrant_local": qdrant_local,
            "collection": self.settings.vector_collection,
            "model": self.settings.embedding_model,
            "device": self._device(),
            "model_cached": model_path.exists(),
            "index_status": "ready" if available else "keyword_fallback",
            "error": error,
        }

    def _path(self, path: Path) -> Path:
        return path if path.is_absolute() else Path.cwd() / path

    def _device(self) -> str:
        if self.settings.embedding_device != "auto":
            return self.settings.embedding_device
        try:
            import torch

            return "cuda" if torch.cuda.is_available() else "cpu"
        except ImportError:
            return "cpu"

    def _load(self) -> None:
        if self._model is not None:
            return
        from FlagEmbedding import BGEM3FlagModel
        from qdrant_client import QdrantClient, models

        device = self._device()
        model_path = self._path(self.settings.embedding_model_path)
        source = str(model_path) if model_path.exists() else self.settings.embedding_model
        self._model = BGEM3FlagModel(source, use_fp16=device == "cuda", devices=[device])
        qdrant_path = self._path(self.settings.qdrant_path)
        qdrant_path.mkdir(parents=True, exist_ok=True)
        self._client = QdrantClient(path=str(qdrant_path))
        collections = {item.name for item in self._client.get_collections().collections}
        if self.settings.vector_collection not in collections:
            self._client.create_collection(
                self.settings.vector_collection,
                vectors_config={
                    "dense": models.VectorParams(size=1024, distance=models.Distance.COSINE)
                },
                sparse_vectors_config={"sparse": models.SparseVectorParams()},
            )

    def embed(self, texts: list[str]) -> tuple[list[list[float]], list[dict[int, float]]]:
        self._load()
        device = self._device()
        batch_size = (
            self.settings.embedding_gpu_batch_size
            if device == "cuda"
            else self.settings.embedding_cpu_batch_size
        )
        while True:
            try:
                output = self._model.encode(
                    texts,
                    batch_size=batch_size,
                    max_length=self.settings.embedding_max_length,
                    return_dense=True,
                    return_sparse=True,
                    return_colbert_vecs=False,
                )
                dense = [vector.tolist() for vector in output["dense_vecs"]]
                sparse = [
                    {int(k): float(v) for k, v in item.items()}
                    for item in output["lexical_weights"]
                ]
                return dense, sparse
            except (RuntimeError, MemoryError):
                if batch_size == 1:
                    raise
                batch_size = max(1, batch_size // 2)

    def index_chunks(self, records: list[dict[str, Any]]) -> None:
        from qdrant_client import models

        if not records:
            return
        dense, sparse = self.embed([record["content"] for record in records])
        points = []
        for record, dense_vector, sparse_vector in zip(records, dense, sparse, strict=True):
            points.append(
                models.PointStruct(
                    id=record["id"],
                    vector={
                        "dense": dense_vector,
                        "sparse": models.SparseVector(
                            indices=list(sparse_vector), values=list(sparse_vector.values())
                        ),
                    },
                    payload={key: value for key, value in record.items() if key != "content"},
                )
            )
        self._client.upsert(self.settings.vector_collection, points=points, wait=True)

    def index(self, records: list[dict[str, Any]]) -> None:
        """Backward-compatible alias for existing callers."""
        self.index_chunks(records)

    def _filter(
        self,
        *,
        project_id: str | None = None,
        script_id: str | None = None,
        content_type: str | None = None,
    ) -> Any:
        from qdrant_client import models

        conditions = []
        if project_id:
            conditions.append(
                models.FieldCondition(
                    key="project_id", match=models.MatchValue(value=project_id)
                )
            )
        if script_id:
            conditions.append(
                models.FieldCondition(
                    key="script_id", match=models.MatchValue(value=script_id)
                )
            )
        if content_type:
            conditions.append(
                models.FieldCondition(
                    key="content_type", match=models.MatchValue(value=content_type)
                )
            )
        return models.Filter(must=conditions) if conditions else None

    def hybrid_search(
        self,
        query: str,
        *,
        project_id: str | None = None,
        script_id: str | None = None,
        content_type: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        from qdrant_client import models

        dense, sparse = self.embed([query])
        result = self._client.query_points(
            collection_name=self.settings.vector_collection,
            prefetch=[
                models.Prefetch(query=dense[0], using="dense", limit=20),
                models.Prefetch(
                    query=models.SparseVector(
                        indices=list(sparse[0]), values=list(sparse[0].values())
                    ),
                    using="sparse",
                    limit=20,
                ),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            query_filter=self._filter(
                project_id=project_id, script_id=script_id, content_type=content_type
            ),
            limit=limit,
            with_payload=True,
        )
        return [
            {"chunk_id": str(point.payload["chunk_id"]), "score": float(point.score)}
            for point in result.points
        ]

    def search(
        self,
        query: str,
        *,
        project_id: str | None = None,
        script_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Backward-compatible alias for existing callers."""
        return self.hybrid_search(query, project_id=project_id, script_id=script_id, limit=limit)

    def delete_project_vectors(self, project_id: str) -> None:
        self._delete_by_filter(project_id=project_id)

    def delete_script_vectors(self, script_id: str) -> None:
        self._delete_by_filter(script_id=script_id)

    def self_test(self) -> dict[str, Any]:
        status = self.status()
        result: dict[str, Any] = {
            "available": status["available"],
            "vector_backend": "qdrant_local",
            "model": status["model"],
            "device": status["device"],
            "collection": status["collection"],
            "checks": {
                "dependencies": status["configured"],
                "qdrant_local": status["qdrant_local"],
                "model_cached": status["model_cached"],
                "index": False,
                "search": False,
                "delete": False,
            },
            "message": "Local RAG is unavailable; keyword retrieval fallback is active.",
        }
        if not status["available"]:
            return result
        point_id = str(uuid.uuid4())
        try:
            self.index_chunks(
                [
                    {
                        "id": point_id,
                        "content": "FilmAgent local rag self test runway beacon.",
                        "chunk_id": point_id,
                        "project_id": "__self_test__",
                        "script_id": "__self_test__",
                        "script_version": 0,
                        "content_type": "self_test",
                        "chapter": "diagnostics",
                        "scene": "qdrant_local",
                        "characters": [],
                        "locations": [],
                        "content_hash": hashlib.sha256(point_id.encode("utf-8")).hexdigest(),
                        "is_current": False,
                    }
                ]
            )
            result["checks"]["index"] = True
            hits = self.hybrid_search(
                "runway beacon",
                project_id="__self_test__",
                script_id="__self_test__",
                content_type="self_test",
                limit=1,
            )
            result["checks"]["search"] = any(hit["chunk_id"] == point_id for hit in hits)
            self.delete_project_vectors("__self_test__")
            result["checks"]["delete"] = True
            result["available"] = all(result["checks"].values())
            result["message"] = (
                "Local RAG self-test passed."
                if result["available"]
                else "Local RAG self-test completed with failed checks."
            )
        except Exception:
            result["available"] = False
            result["message"] = (
                "Local RAG self-test failed; see server logs for operational details."
            )
        return result

    def _delete_by_filter(
        self, *, project_id: str | None = None, script_id: str | None = None
    ) -> None:
        from qdrant_client import models

        query_filter = self._filter(project_id=project_id, script_id=script_id)
        if query_filter is None:
            raise ValueError("A project_id or script_id is required to delete vectors")
        self._load()
        self._client.delete(
            collection_name=self.settings.vector_collection,
            points_selector=models.FilterSelector(filter=query_filter),
            wait=True,
        )
