"""Search API endpoints with permission checks and reranking."""

import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from openrag.database import get_db
from openrag.models.document_chunk import DocumentChunk
from openrag.models.file import File as FileModel
from openrag.embedding.embedding_engine import EmbeddingEngine
from openrag.retrieval.reranker import Reranker
from openrag.retrieval.retrieval_service import RetrievalService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/search", tags=["search"])

# ---------------------------------------------------------------------------
# Singleton-ish factories (created once per process)
# ---------------------------------------------------------------------------

_embedding_engine: Optional[EmbeddingEngine] = None
_vector_store = None
_layer_store_instance = None
_layer_store_init_failed: bool = False
_fulltext_store = None
_fulltext_store_attempted: bool = False


def _get_embedding_engine() -> EmbeddingEngine:
    global _embedding_engine
    if _embedding_engine is None:
        _embedding_engine = EmbeddingEngine()
    return _embedding_engine


def _get_vector_store():
    global _vector_store
    if _vector_store is None:
        try:
            from openrag.vectorstore.milvus_store import MilvusStore

            _vector_store = MilvusStore(dimension=_get_embedding_engine().dimension)
        except Exception as exc:
            logger.error("Cannot connect to Milvus: %s", exc)
            raise HTTPException(status_code=503, detail="Vector database unavailable")
    return _vector_store


def _get_layer_store():
    """Milvus L0/L1 集合；不可用时返回 None（退化为平面切片检索）。"""
    global _layer_store_instance, _layer_store_init_failed
    if _layer_store_init_failed:
        return None
    if _layer_store_instance is not None:
        return _layer_store_instance
    try:
        from openrag.vectorstore.milvus_layer_store import MilvusLayerStore

        _layer_store_instance = MilvusLayerStore(
            dimension=_get_embedding_engine().dimension
        )
        return _layer_store_instance
    except Exception as exc:
        logger.warning("Layer store unavailable: %s", exc)
        _layer_store_init_failed = True
        return None


def _get_fulltext_store():
    """Elasticsearch chunk 全文；未启用或不可用时返回 None。"""
    global _fulltext_store, _fulltext_store_attempted
    if _fulltext_store_attempted:
        return _fulltext_store
    _fulltext_store_attempted = True
    try:
        from openrag.search.es_chunk_store import create_es_chunk_store_from_config

        _fulltext_store = create_es_chunk_store_from_config()
        return _fulltext_store
    except Exception as exc:
        logger.warning("Fulltext store unavailable: %s", exc)
        _fulltext_store = None
        return None


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, description="Search query text")
    top_k: int = Field(10, gt=0, le=100, description="Number of results")
    workspace_id: Optional[int] = Field(None, description="Restrict to workspace")
    use_rerank: bool = Field(True, description="Apply reranking")
    use_contextual_retrieval: bool = Field(
        False,
        description="OpenViking 式：L0 粗筛 → L1 → 候选内 L2，并按 0.2/0.3/0.5 融合打分",
    )
    contextual_l0_top_n: int = Field(40, ge=5, le=200, description="L0 候选文件数上限")
    contextual_l1_top_n: int = Field(30, ge=5, le=200, description="L1 检索 depth")
    contextual_chunk_fetch_multiplier: int = Field(
        4, ge=1, le=20, description="在候选文件内抓取切片时的 top_k 倍率"
    )
    retrieval_strategy: str = Field(
        "auto",
        description="B5 意图: auto|light|deep|precise|flat；与 use_contextual_retrieval 组合使用",
    )
    use_l1_llm_navigation: bool = Field(
        False,
        description="B7/B8: 启用时由 LLM 根据 L1 选择 chunk 下标并过滤（需 OPENAI_API_KEY，仅上下文检索生效）",
    )
    vector_similarity_weight: float = Field(
        1.0,
        ge=0.0,
        le=1.0,
        description="向量融合分权重；全文 BM25 权重为 1-该值。1 表示仅向量侧（默认）。",
    )


class SearchResult(BaseModel):
    text: str
    score: float
    file_id: int
    chunk_id: Optional[str] = None
    chunk_index: Optional[int] = None
    page: int = 0
    level: int = 0
    block_type: str = "text"
    start_offset: int = 0
    end_offset: int = 0
    bbox_x0: Optional[float] = None
    bbox_y0: Optional[float] = None
    bbox_x1: Optional[float] = None
    bbox_y1: Optional[float] = None
    source_block_id: Optional[str] = None
    source_char_start: Optional[int] = None
    source_char_end: Optional[int] = None
    filename: Optional[str] = None
    uri: Optional[str] = None
    object_key: Optional[str] = None
    object_url: Optional[str] = None
    local_chunk_path: Optional[str] = None
    text_preview: Optional[str] = None
    retrieval_strategy: Optional[str] = None
    l1_llm_filtered: Optional[bool] = None


class SearchResponse(BaseModel):
    results: list[SearchResult]
    total: int
    query_time_ms: float
    l1_llm_applied: Optional[bool] = Field(
        None, description="L1 LLM 导航是否实际执行了（区别于用户请求）"
    )
    l1_llm_skip_reason: Optional[str] = Field(
        None,
        description="若 L1 LLM 未执行，给出原因（如 no_api_key, not_contextual, no_l1_hits）",
    )


class ChunkNeighbor(BaseModel):
    chunk_id: Optional[str] = None
    chunk_index: int
    object_key: str
    object_url: Optional[str] = None


class ChunkContextFile(BaseModel):
    id: int
    name: str
    uri: str
    workspace_id: int
    l0_path: Optional[str] = None
    l1_path: Optional[str] = None
    l2_path: Optional[str] = None


class ChunkContextResponse(BaseModel):
    chunk_id: str
    chunk_index: int
    object_key: str
    object_url: Optional[str] = None
    local_chunk_path: Optional[str] = None
    text_preview: Optional[str] = None
    page: int = 0
    level: int = 0
    block_type: str = "text"
    start_offset: int = 0
    end_offset: int = 0
    bbox_x0: Optional[float] = None
    bbox_y0: Optional[float] = None
    bbox_x1: Optional[float] = None
    bbox_y1: Optional[float] = None
    source_block_id: Optional[str] = None
    source_char_start: Optional[int] = None
    source_char_end: Optional[int] = None
    file: ChunkContextFile
    prev_chunk: Optional[ChunkNeighbor] = None
    next_chunk: Optional[ChunkNeighbor] = None


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

from openrag.api.deps import get_current_active_user
from openrag.models.user import User
from openrag.services.workspace_service import WorkspaceService


def get_user_id(current_user: User = Depends(get_current_active_user)) -> int:
    return current_user.id


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


def _execute_search(
    db: Session,
    user_id: int,
    request: SearchRequest,
    *,
    endpoint: str,
    rerank_hierarchical_boost: Optional[float],
) -> SearchResponse:
    start = time.time()
    svc = RetrievalService(
        db=db,
        embedding_engine=_get_embedding_engine(),
        vector_store=_get_vector_store(),
        layer_store=_get_layer_store(),
        fulltext_store=_get_fulltext_store(),
    )

    is_hierarchical = endpoint == "hierarchical"
    force_contextual = is_hierarchical and not request.use_contextual_retrieval

    if request.use_contextual_retrieval or force_contextual:
        results = svc.search(
            query=request.query,
            user_id=user_id,
            workspace_id=request.workspace_id,
            top_k=request.top_k,
            use_contextual=True,
            contextual_l0_top_n=request.contextual_l0_top_n,
            contextual_l1_top_n=request.contextual_l1_top_n,
            contextual_chunk_fetch_multiplier=request.contextual_chunk_fetch_multiplier,
            retrieval_strategy=request.retrieval_strategy,
            use_l1_llm_navigation=request.use_l1_llm_navigation,
            vector_similarity_weight=request.vector_similarity_weight,
        )
    else:
        fetch_k = request.top_k * 3 if request.use_rerank else request.top_k
        results = svc.search(
            query=request.query,
            user_id=user_id,
            workspace_id=request.workspace_id,
            top_k=fetch_k,
            use_contextual=False,
            retrieval_strategy=request.retrieval_strategy,
            use_l1_llm_navigation=False,
            vector_similarity_weight=request.vector_similarity_weight,
        )

        if request.use_rerank and results:
            if rerank_hierarchical_boost is not None:
                reranker = Reranker(hierarchical_boost=rerank_hierarchical_boost)
            else:
                reranker = Reranker()
            results = reranker.rerank(request.query, results, top_k=request.top_k)
        else:
            results = results[: request.top_k]

    use_fused_scores = (request.use_contextual_retrieval or force_contextual) and bool(
        results
    )
    if (
        not request.use_contextual_retrieval
        and not force_contextual
        and request.vector_similarity_weight < 1.0 - 1e-12
        and bool(results)
    ):
        use_fused_scores = True
    formatted = _format(results, reranked=use_fused_scores or request.use_rerank)
    elapsed_ms = (time.time() - start) * 1000

    l1_llm_applied = None
    l1_llm_skip_reason = None
    if results:
        first = results[0]
        l1_llm_applied = first.get("_l1_llm_applied")
        l1_llm_skip_reason = first.get("_l1_llm_skip_reason")

    strat_resolved = None
    if results:
        strat_resolved = next(
            (
                r.get("retrieval_strategy")
                for r in results
                if r.get("retrieval_strategy")
            ),
            None,
        )
    modes = {r.get("retrieval_mode") for r in results if r.get("retrieval_mode")}
    l1_llm_hits = sum(1 for r in results if r.get("l1_llm_filtered"))

    logger.info(
        "retrieval_complete endpoint=%s workspace_id=%s contextual=%s "
        "force_contextual=%s strategy_req=%s strategy_resolved=%s "
        "use_l1_llm_req=%s l1_llm_applied=%s l1_llm_skip=%s "
        "hits=%d l1_llm_hits=%d modes=%s rerank=%s ms=%.2f",
        endpoint,
        request.workspace_id,
        request.use_contextual_retrieval,
        force_contextual,
        request.retrieval_strategy,
        strat_resolved,
        request.use_l1_llm_navigation,
        l1_llm_applied,
        l1_llm_skip_reason,
        len(formatted),
        l1_llm_hits,
        ",".join(sorted(str(m) for m in modes if m)) if modes else "-",
        request.use_rerank,
        elapsed_ms,
    )

    return SearchResponse(
        results=formatted,
        total=len(formatted),
        query_time_ms=elapsed_ms,
        l1_llm_applied=l1_llm_applied,
        l1_llm_skip_reason=l1_llm_skip_reason,
    )


@router.post("/semantic", response_model=SearchResponse)
@router.post("", response_model=SearchResponse)
async def semantic_search(
    request: SearchRequest,
    user_id: int = Depends(get_user_id),
    db: Session = Depends(get_db),
):
    """Semantic search with permission filtering and optional reranking."""
    try:
        return _execute_search(
            db,
            user_id,
            request,
            endpoint="semantic",
            rerank_hierarchical_boost=None,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Search failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/hierarchical", response_model=SearchResponse)
async def hierarchical_search(
    request: SearchRequest,
    user_id: int = Depends(get_user_id),
    db: Session = Depends(get_db),
):
    """Hierarchical search (title/heading aware) with permission filtering."""
    try:
        return _execute_search(
            db,
            user_id,
            request,
            endpoint="hierarchical",
            rerank_hierarchical_boost=0.15,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Search failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format(results: list[dict], reranked: bool = False) -> list[SearchResult]:
    out = []
    for r in results:
        score = r.get("reranked_score", r.get("score", 0.0))
        out.append(
            SearchResult(
                text=r.get("text", ""),
                score=score,
                file_id=r.get("file_id", 0),
                chunk_id=r.get("chunk_id"),
                chunk_index=r.get("chunk_index"),
                page=r.get("page", 0),
                level=r.get("level", 0),
                block_type=r.get("block_type", "text"),
                start_offset=r.get("start_offset", 0),
                end_offset=r.get("end_offset", 0),
                bbox_x0=r.get("bbox_x0"),
                bbox_y0=r.get("bbox_y0"),
                bbox_x1=r.get("bbox_x1"),
                bbox_y1=r.get("bbox_y1"),
                source_block_id=r.get("source_block_id"),
                source_char_start=r.get("source_char_start"),
                source_char_end=r.get("source_char_end"),
                filename=r.get("filename"),
                uri=r.get("uri"),
                object_key=r.get("object_key"),
                object_url=r.get("object_url"),
                local_chunk_path=r.get("local_chunk_path"),
                text_preview=r.get("text_preview"),
                retrieval_strategy=r.get("retrieval_strategy"),
                l1_llm_filtered=r.get("l1_llm_filtered"),
            )
        )
    return out


@router.get("/chunks/{chunk_id}", response_model=ChunkContextResponse)
async def get_chunk_context(
    chunk_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """根据 Milvus 返回的 chunk_id 查询切片存储路径、摘要及所属文档（含前后邻块）。"""
    row = db.query(DocumentChunk).filter(DocumentChunk.chunk_id == chunk_id).first()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Chunk not found",
        )
    f = db.query(FileModel).filter(FileModel.id == row.file_id).first()
    if not f or f.is_directory:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File not found",
        )

    ws_service = WorkspaceService(db)
    if not ws_service.check_user_permission(f.workspace_id, current_user.id, "read"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Read permission required",
        )

    def _neighbor(c: Optional[DocumentChunk]) -> Optional[ChunkNeighbor]:
        if not c:
            return None
        return ChunkNeighbor(
            chunk_id=c.chunk_id,
            chunk_index=c.chunk_index,
            object_key=c.object_key,
            object_url=c.object_url,
        )

    prev_row = (
        db.query(DocumentChunk)
        .filter(
            DocumentChunk.file_id == row.file_id,
            DocumentChunk.chunk_index == row.chunk_index - 1,
        )
        .first()
    )
    next_row = (
        db.query(DocumentChunk)
        .filter(
            DocumentChunk.file_id == row.file_id,
            DocumentChunk.chunk_index == row.chunk_index + 1,
        )
        .first()
    )

    return ChunkContextResponse(
        chunk_id=row.chunk_id,
        chunk_index=row.chunk_index,
        object_key=row.object_key,
        object_url=row.object_url,
        local_chunk_path=row.local_chunk_path,
        text_preview=row.text_preview,
        page=row.page,
        level=row.level,
        block_type=row.block_type,
        start_offset=row.start_offset,
        end_offset=row.end_offset,
        bbox_x0=row.bbox_x0,
        bbox_y0=row.bbox_y0,
        bbox_x1=row.bbox_x1,
        bbox_y1=row.bbox_y1,
        source_block_id=row.source_block_id,
        source_char_start=row.source_char_start,
        source_char_end=row.source_char_end,
        file=ChunkContextFile(
            id=f.id,
            name=f.name,
            uri=f.uri,
            workspace_id=f.workspace_id,
            l0_path=f.l0_path,
            l1_path=f.l1_path,
            l2_path=f.l2_path,
        ),
        prev_chunk=_neighbor(prev_row),
        next_chunk=_neighbor(next_row),
    )
