"""Search API endpoints with permission checks and reranking."""

import hashlib
import logging
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from openrag.database import get_db
from openrag.models import TraceRun
from openrag.models.document_chunk import DocumentChunk
from openrag.models.file import File as FileModel
from openrag.embedding.embedding_engine import EmbeddingEngine
from openrag.embedding.errors import (
    EmbeddingConfigurationError,
    EmbeddingError,
    EmbeddingInputError,
    EmbeddingResponseError,
)
from openrag.retrieval.reranker import Reranker
from openrag.retrieval.retrieval_service import (
    PermissionScopeResolutionError,
    RetrievalService,
    l0_l1_retrieval_enabled,
)
from openrag.services.trace_service import TraceService
from openrag.tracing.context import get_trace_context, set_trace_context

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/search", tags=["search"])

# ---------------------------------------------------------------------------
# Singleton-ish factories (created once per process)
# ---------------------------------------------------------------------------

_embedding_engine: Optional[EmbeddingEngine] = None
_vector_store = None
_layer_store_instance = None
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
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "vector_database_unavailable",
                    "message": "Vector database is temporarily unavailable",
                },
            )
    return _vector_store


def _get_layer_store():
    """Milvus L0/L1 集合；不可用时返回 None（退化为平面切片检索）。"""
    if not l0_l1_retrieval_enabled():
        return None

    global _layer_store_instance
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
        0.7,
        ge=0.0,
        le=1.0,
        description="向量融合分权重；全文 BM25 权重为 1-该值。默认 0.7；1 表示仅向量侧。",
    )
    paths: Optional[list[str]] = Field(
        None,
        description="限定检索范围到这些逻辑路径（文件夹递归 / 单文件）；需配合 workspace_id。空数组=空范围",
    )

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        query = value.strip()
        if not query:
            raise ValueError("Search query must not be blank")
        return query


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
from openrag.services.workspace_file_tree import resolve_scope_file_ids
from openrag.services.workspace_service import WorkspaceService


def get_user_id(current_user: User = Depends(get_current_active_user)) -> int:
    return current_user.id


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


def assert_search_workspace_read(db: Session, user_id: int, workspace_id: Optional[int]) -> None:
    """Internal JWT search entry workspace read-permission check. workspace_id=None keeps
    the existing 'search across the user's accessible workspaces' semantics; admin is
    allowed via check_user_permission. JWT endpoints only — not the service-token path."""
    if workspace_id is None:
        return
    try:
        allowed = WorkspaceService(db).check_user_permission(
            workspace_id, user_id, "read"
        )
    except Exception as exc:
        logger.exception(
            "workspace_read_resolution_failed user_id=%s workspace_id=%s "
            "exception_type=%s",
            user_id,
            workspace_id,
            type(exc).__name__,
        )
        raise PermissionScopeResolutionError(
            "Workspace read permission could not be resolved"
        ) from exc
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Read permission required",
        )


def _embedding_error_to_http_exception(exc: EmbeddingError) -> HTTPException:
    if isinstance(exc, EmbeddingInputError):
        status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
        code = "embedding_input_invalid"
        message = "Search query is invalid"
    elif isinstance(exc, EmbeddingResponseError):
        status_code = status.HTTP_502_BAD_GATEWAY
        code = "embedding_response_invalid"
        message = "Embedding service returned an invalid response"
    elif isinstance(exc, EmbeddingConfigurationError):
        status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        code = "embedding_configuration_error"
        message = "Semantic search is not configured"
    else:
        status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        code = "embedding_service_unavailable"
        message = "Semantic search is temporarily unavailable"
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )


def _execute_search(
    db: Session,
    user_id: int,
    request: SearchRequest,
    *,
    endpoint: str,
    rerank_hierarchical_boost: Optional[float],
    workspace_access_prevalidated: bool = False,
) -> SearchResponse:
    start = time.time()
    trace_service, started_trace_run = _prepare_retrieval_trace(
        db=db,
        user_id=user_id,
        request=request,
        endpoint=endpoint,
    )

    try:
        if workspace_access_prevalidated and request.workspace_id is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Invalid prevalidated workspace scope",
            )
        if not workspace_access_prevalidated:
            assert_search_workspace_read(db, user_id, request.workspace_id)

        scope_file_ids = None
        if request.paths is not None:
            if request.workspace_id is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="workspace_id is required when paths is set",
                )
            try:
                scope_file_ids = resolve_scope_file_ids(
                    db,
                    request.workspace_id,
                    request.paths,
                )
            except HTTPException:
                raise
            except Exception as exc:
                logger.exception(
                    "path_scope_resolution_failed user_id=%s workspace_id=%s "
                    "endpoint=%s exception_type=%s",
                    user_id,
                    request.workspace_id,
                    endpoint,
                    type(exc).__name__,
                )
                raise PermissionScopeResolutionError(
                    "Search path scope could not be resolved"
                ) from exc

        # Resolve authorization once before initializing retrieval dependencies.
        svc = RetrievalService(
            db=db,
            embedding_engine=None,
            vector_store=None,
            layer_store=None,
            fulltext_store=None,
        )
        resolved_file_scope = svc.resolve_file_scope(
            user_id,
            request.workspace_id,
            scope_file_ids,
        )

        if resolved_file_scope.file_ids == ():
            if started_trace_run:
                trace_service.finish_run()
            return SearchResponse(
                results=[],
                total=0,
                query_time_ms=(time.time() - start) * 1000,
                l1_llm_applied=None,
                l1_llm_skip_reason=None,
            )

        hierarchy_enabled = l0_l1_retrieval_enabled()
        is_hierarchical = endpoint == "hierarchical"
        needs_layer_store = request.use_contextual_retrieval or is_hierarchical
        svc.embedding_engine = _get_embedding_engine()
        svc.vector_store = _get_vector_store()
        svc.layer_store = _get_layer_store() if needs_layer_store else None
        svc.fulltext_store = _get_fulltext_store()

        use_contextual_retrieval = request.use_contextual_retrieval and hierarchy_enabled
        use_l1_llm_navigation = request.use_l1_llm_navigation and hierarchy_enabled
        force_contextual = (
            hierarchy_enabled and is_hierarchical and not request.use_contextual_retrieval
        )
        if needs_layer_store and (
            not hierarchy_enabled or svc.layer_store is None
        ):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "hierarchical_index_unavailable",
                    "message": "Hierarchical search index is temporarily unavailable",
                },
            )
        effective_hierarchical_boost = (
            rerank_hierarchical_boost if hierarchy_enabled else None
        )

        if use_contextual_retrieval or force_contextual:
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
                use_l1_llm_navigation=use_l1_llm_navigation,
                vector_similarity_weight=request.vector_similarity_weight,
                resolved_file_scope=resolved_file_scope,
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
                resolved_file_scope=resolved_file_scope,
            )

            pre_rerank_results = list(results)
            if request.use_rerank and results:
                if effective_hierarchical_boost is not None:
                    reranker = Reranker(hierarchical_boost=effective_hierarchical_boost)
                else:
                    reranker = Reranker()
                results = reranker.rerank(
                    request.query,
                    results,
                    top_k=request.top_k,
                    trace_service=trace_service,
                )
            else:
                results = results[: request.top_k]

        if use_contextual_retrieval or force_contextual:
            pre_rerank_results = list(results)

        use_fused_scores = (use_contextual_retrieval or force_contextual) and bool(
            results
        )
        if (
            not use_contextual_retrieval
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
            use_contextual_retrieval,
            force_contextual,
            request.retrieval_strategy,
            strat_resolved,
            use_l1_llm_navigation,
            l1_llm_applied,
            l1_llm_skip_reason,
            len(formatted),
            l1_llm_hits,
            ",".join(sorted(str(m) for m in modes if m)) if modes else "-",
            request.use_rerank,
            elapsed_ms,
        )

        _record_response_trace(
            trace_service,
            final_results=results,
            pre_rerank_results=pre_rerank_results,
        )
        if started_trace_run:
            trace_service.finish_run()

        return SearchResponse(
            results=formatted,
            total=len(formatted),
            query_time_ms=elapsed_ms,
            l1_llm_applied=l1_llm_applied,
            l1_llm_skip_reason=l1_llm_skip_reason,
        )
    except PermissionScopeResolutionError as exc:
        if started_trace_run:
            trace_service.fail_run(
                error_stage="authorization.scope_resolution",
                error_message="permission_scope_resolution_failed",
            )
        cause = exc.__cause__ or exc
        logger.error(
            "search_authorization_unavailable endpoint=%s user_id=%s workspace_id=%s "
            "exception_type=%s",
            endpoint,
            user_id,
            request.workspace_id,
            type(cause).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Search authorization temporarily unavailable",
        ) from exc
    except HTTPException as exc:
        if started_trace_run:
            is_denied = exc.status_code == status.HTTP_403_FORBIDDEN
            trace_service.fail_run(
                error_stage=(
                    "authorization.workspace_read" if is_denied else "retrieval"
                ),
                error_message=(
                    "workspace_read_denied" if is_denied else f"http_{exc.status_code}"
                ),
            )
        raise
    except Exception as exc:
        if started_trace_run:
            trace_service.fail_run(error_stage="retrieval", error_message=str(exc))
        raise


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
    except EmbeddingError as exc:
        raise _embedding_error_to_http_exception(exc) from exc
    except Exception as exc:
        logger.exception("Search failed")
        if type(exc).__module__.startswith("pymilvus"):
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "vector_database_unavailable",
                    "message": "Vector database is temporarily unavailable",
                },
            ) from exc
        raise HTTPException(
            status_code=500,
            detail={"code": "search_failed", "message": "Search request failed"},
        ) from exc


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
    except EmbeddingError as exc:
        raise _embedding_error_to_http_exception(exc) from exc
    except Exception as exc:
        logger.exception("Search failed")
        if type(exc).__module__.startswith("pymilvus"):
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "vector_database_unavailable",
                    "message": "Vector database is temporarily unavailable",
                },
            ) from exc
        raise HTTPException(
            status_code=500,
            detail={"code": "search_failed", "message": "Search request failed"},
        ) from exc


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


def _prepare_retrieval_trace(
    *,
    db: Session,
    user_id: int,
    request: SearchRequest,
    endpoint: str,
) -> tuple[TraceService, bool]:
    trace_service = TraceService(db)
    query_hash = _query_hash(request.query)
    query_preview = _preview_query(request.query)
    ctx = get_trace_context()
    trace_id = ctx["trace_id"] or uuid.uuid4().hex
    trace_type = ctx["trace_type"] or "retrieval"
    set_trace_context(
        trace_id=trace_id,
        trace_type=trace_type,
        workspace_id=request.workspace_id,
        user_id=user_id,
    )

    started_run = False
    if trace_type == "retrieval":
        existing = db.query(TraceRun).filter(TraceRun.trace_id == trace_id).first()
        if existing is None:
            trace_service.start_run(
                trace_type="retrieval",
                trace_id=trace_id,
                workspace_id=request.workspace_id,
                user_id=user_id,
                query_hash=query_hash,
                query_preview=query_preview,
                sampling_reason=ctx["sampling_reason"] or "search_api",
                search_config_snapshot={
                    "endpoint": endpoint,
                    "top_k": request.top_k,
                    "use_rerank": request.use_rerank,
                    "use_contextual_retrieval": request.use_contextual_retrieval,
                    "retrieval_strategy": request.retrieval_strategy,
                    "vector_similarity_weight": request.vector_similarity_weight,
                },
            )
            started_run = True

    trace_service.start_span(
        "retrieval.request",
        input_summary={
            "query_hash": query_hash,
            "query_preview": query_preview,
            "workspace_id": request.workspace_id,
            "top_k": request.top_k,
            "use_rerank": request.use_rerank,
            "vector_similarity_weight": request.vector_similarity_weight,
            "retrieval_strategy": request.retrieval_strategy,
        },
    )
    trace_service.finish_span(output_summary={"endpoint": endpoint})
    return trace_service, started_run


def _record_response_trace(
    trace_service: TraceService,
    *,
    final_results: list[dict],
    pre_rerank_results: list[dict],
) -> None:
    final_top50 = final_results[:50]
    pre_top50 = pre_rerank_results[:50]
    output_summary = {
        "final_result_count": len(final_results),
        "zero_hit": len(final_results) == 0,
        "top50_source_file_count": len(
            {r.get("file_id") for r in final_top50 if r.get("file_id") is not None}
        ),
        "fusion_overlap": _chunk_overlap(pre_top50, final_top50),
        "rerank_overlap": _chunk_overlap(pre_top50, final_top50),
    }
    trace_service.start_span("retrieval.response")
    trace_service.finish_span(output_summary=output_summary)


def _chunk_overlap(left: list[dict], right: list[dict]) -> int:
    left_ids = {str(r.get("chunk_id")) for r in left if r.get("chunk_id")}
    right_ids = {str(r.get("chunk_id")) for r in right if r.get("chunk_id")}
    return len(left_ids & right_ids)


def _query_hash(query: str) -> str:
    normalized = " ".join((query or "").split()).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _preview_query(query: str, max_len: int = 512) -> str:
    normalized = " ".join((query or "").split())
    if len(normalized) <= max_len:
        return normalized
    return normalized[: max_len - 1] + "..."


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
    f = (
        db.query(FileModel)
        .filter(FileModel.id == row.file_id, FileModel.deleted_at.is_(None))
        .first()
    )
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
