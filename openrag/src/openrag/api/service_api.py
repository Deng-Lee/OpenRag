"""Machine-to-machine API (service token only; no JWT on these routes)."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, List, Literal, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy.orm import Session, sessionmaker

from openrag.api.deps import get_db, get_service_token_context
from openrag.api.search_api import (
    SearchRequest,
    SearchResponse,
    SearchResult,
    _execute_search,
)
from openrag.config import get_config, get_preview_public_web_base_url
from openrag.models.document_chunk import DocumentChunk
from openrag.models.file import File as DbFile
from openrag.models.task import Task
from openrag.models.workspace import Workspace
from openrag.retrieval.reranker import Reranker
from openrag.services.document_retry_status import (
    document_processing_fields,
    retry_failed_document_processing,
    task_retry_fields,
)
from openrag.services.file_deletion import (
    FileStorageCleanupError,
    _release_tag_and_soft_delete,
    delete_file_with_storage,
)
from openrag.services.file_ingest import (
    ingest_new_file,
    replace_file_content,
    upsert_file_by_tag,
    validate_path,
)
from openrag.services.preview_token_service import (
    create_preview_token,
    decode_preview_token,
)
from openrag.services.search_grant_service import (
    SearchGrantClaims,
    SearchGrantError,
    create_search_grant,
    decode_search_grant,
    normalize_grant_paths,
    path_is_within,
    unverified_scope_ref,
    validate_search_grant_context,
)
from openrag.services.service_token_service import (
    ServiceTokenContext,
    assert_token_workspace_permission,
    require_workspace_for_name,
)
from openrag.services.upload_policy import read_upload_content
from openrag.services.workspace_file_tree import (
    build_nested_tree,
    get_file_document_by_path,
    list_direct_children,
    list_entries_by_prefix,
    search_documents_by_name,
)
from openrag.tracing.context import set_trace_context

router = APIRouter(prefix="/service/v1", tags=["service"])

logger = logging.getLogger(__name__)


@router.get("/workspaces")
async def service_list_workspaces(
    ctx: ServiceTokenContext = Depends(get_service_token_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return the workspace(s) accessible to the authenticated service token."""
    items = []
    for binding in ctx.bindings:
        ws = db.query(Workspace).filter(Workspace.id == binding.workspace_id).first()
        if ws is not None:
            items.append({
                "id": ws.id,
                "name": ws.name,
                "slug": ws.slug,
                "description": ws.description,
                "permission": binding.permission,
            })
    return {"items": items, "workspaces": items}


class ServiceSearchRequest(BaseModel):
    """Semantic search body for service token API (workspace comes from URL only)."""

    query: str = Field(..., min_length=1)
    path_prefix: Optional[str] = Field(
        None,
        description="If set, only hits whose file uri is under this logical path prefix",
    )
    paths: Optional[List[str]] = Field(
        None,
        description="限定检索范围到这些逻辑路径；显式传入时覆盖 path_prefix（含 []=空范围）",
    )
    top_k: int = Field(10, gt=0, le=100)
    use_rerank: bool = False
    use_contextual_retrieval: bool = False
    contextual_l0_top_n: int = Field(40, ge=5, le=200)
    contextual_l1_top_n: int = Field(30, ge=5, le=200)
    contextual_chunk_fetch_multiplier: int = Field(4, ge=1, le=20)
    retrieval_strategy: str = "auto"
    use_l1_llm_navigation: bool = False


class ServiceMultiWorkspaceSearchRequest(BaseModel):
    """Semantic search body for service token API across multiple workspaces."""

    workspace_names: Optional[List[str]] = Field(default=None, max_length=20)
    query: str = Field(..., min_length=1)
    path_prefix: Optional[str] = Field(
        None,
        description="If set, only hits whose file uri is under this logical path prefix",
    )
    paths: Optional[List[str]] = Field(
        None,
        description="限定检索范围到这些逻辑路径；显式传入时覆盖 path_prefix（含 []=空范围）",
    )
    top_k: int = Field(10, gt=0, le=100)
    use_rerank: bool = False
    use_contextual_retrieval: bool = False
    contextual_l0_top_n: int = Field(40, ge=5, le=200)
    contextual_l1_top_n: int = Field(30, ge=5, le=200)
    contextual_chunk_fetch_multiplier: int = Field(4, ge=1, le=20)
    retrieval_strategy: str = "auto"
    use_l1_llm_navigation: bool = False
    rerank_scope: Literal["workspace", "global"] = "workspace"


class SkippedWorkspace(BaseModel):
    workspace_name: str
    reason: str
    message: str


class MultiWorkspaceSearchResult(SearchResult):
    workspace_id: int
    workspace_name: str
    retrieval_score: Optional[float] = None
    rerank_score: Optional[float] = None


class MultiWorkspaceSearchResponse(BaseModel):
    results: list[MultiWorkspaceSearchResult]
    total: int
    query_time_ms: float
    workspace_count: int
    l1_llm_applied: Optional[bool] = None
    l1_llm_skip_reason: Optional[str] = None
    skipped_workspaces: list[SkippedWorkspace]
    rerank_scope_applied: Literal["workspace", "global"] = "workspace"
    rerank_status: Literal["success", "disabled", "degraded"] = "disabled"
    ranking_mode: Literal[
        "global_rerank", "global_retrieval_score", "workspace_rerank"
    ] = "workspace_rerank"
    candidate_count: int = 0
    candidate_pool_truncated: bool = False
    requested_workspace_count: int = 0
    failed_workspace_count: int = 0
    rerank_time_ms: float = 0.0


class SearchGrantIssueScope(BaseModel):
    scope_ref: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    workspace_name: str = Field(min_length=1, max_length=255, pattern=r".*\S.*")
    allowed_paths: list[str] | None = Field(default=None, max_length=50)


class SearchGrantIssueRequest(BaseModel):
    request_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    query_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    max_top_k: int = Field(gt=0, le=100)
    scopes: list[SearchGrantIssueScope] = Field(min_length=1, max_length=20)

class IssuedSearchGrant(BaseModel):
    scope_ref: str
    grant: str = Field(repr=False)
    expires_at: int


class SearchGrantIssueFailure(BaseModel):
    scope_ref: str
    code: str
    message: str


class SearchGrantIssueResponse(BaseModel):
    instance_id: str
    grants: list[IssuedSearchGrant]
    failures: list[SearchGrantIssueFailure]


class FederatedSearchRequest(BaseModel):
    request_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    query: str = Field(min_length=1, max_length=10000, pattern=r".*\S.*")
    top_k: int = Field(default=10, gt=0, le=100)
    use_rerank: bool = True
    use_contextual_retrieval: bool = False
    contextual_l0_top_n: int = Field(40, ge=5, le=200)
    contextual_l1_top_n: int = Field(30, ge=5, le=200)
    contextual_chunk_fetch_multiplier: int = Field(4, ge=1, le=20)
    retrieval_strategy: str = "auto"
    use_l1_llm_navigation: bool = False
    grants: list[SecretStr] = Field(min_length=1, max_length=20)

class FederatedSearchResult(MultiWorkspaceSearchResult):
    scope_ref: str


class FederatedScopeOutcome(BaseModel):
    scope_ref: str
    status: Literal["succeeded", "failed"]
    code: str | None = None
    message: str | None = None


class FederatedSearchResponse(BaseModel):
    results: list[FederatedSearchResult]
    scope_outcomes: list[FederatedScopeOutcome]
    total: int
    query_time_ms: float
    workspace_count: int
    rerank_scope_applied: Literal["global"] = "global"
    rerank_status: Literal["success", "disabled", "degraded"]
    ranking_mode: Literal["global_rerank", "global_retrieval_score"]
    candidate_count: int = 0
    candidate_pool_truncated: bool = False
    failed_workspace_count: int = 0
    rerank_time_ms: float = 0.0


class ServicePreviewLinkRequest(BaseModel):
    file_id: int
    chunk_id: str = Field(..., min_length=1)
    chunk_index: int | None = None
    ttl_seconds: int | None = None


class ServicePreviewLinkResponse(BaseModel):
    preview_url: str
    expires_at: datetime
    ttl_seconds: int


def _resolve_scope_paths(
    paths: Optional[List[str]], path_prefix: Optional[str]
) -> Optional[List[str]]:
    """paths overrides path_prefix when explicitly provided (including []); only fall
    back to the legacy path_prefix when paths is None."""
    if paths is not None:
        return paths
    if path_prefix and str(path_prefix).strip() and str(path_prefix).strip() != "/":
        return [path_prefix]
    return None


def _file_summary(f: DbFile) -> dict[str, Any]:
    return {
        "id": f.id,
        "path": f.uri,
        "name": f.name,
        "kind": "dir" if f.is_directory else "file",
        "size": f.size,
        "mime_type": f.mime_type,
        "updated_at": f.updated_at.isoformat() if f.updated_at else None,
    }


def _document_summary(db: Session, f: DbFile) -> dict[str, Any]:
    payload = {
        "id": f.id,
        "path": f.uri,
        "name": f.name,
        "size": f.size,
        "mime_type": f.mime_type,
        "tag": f.tag,
        "updated_at": f.updated_at.isoformat() if f.updated_at else None,
    }
    payload.update(document_processing_fields(db, f))
    return payload


def _get_service_document_by_id(db: Session, workspace_id: int, document_id: int) -> DbFile:
    row = (
        db.query(DbFile)
        .filter(
            DbFile.id == document_id,
            DbFile.workspace_id == workspace_id,
            DbFile.is_directory.is_(False),
            DbFile.deleted_at.is_(None),
        )
        .first()
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    return row


def _upload_response_dict(
    db: Session, file_record: DbFile, task_record: Task | None
) -> dict[str, Any]:
    payload = {
        "id": file_record.id,
        "path": file_record.uri,
        "name": file_record.name,
        "owner_id": file_record.owner_id,
        "parent_id": file_record.parent_id,
        "is_directory": file_record.is_directory,
        "size": file_record.size,
        "mime_type": file_record.mime_type,
        "tag": file_record.tag,
        "created_at": file_record.created_at.isoformat() if file_record.created_at else None,
        "updated_at": file_record.updated_at.isoformat() if file_record.updated_at else None,
    }
    payload.update(document_processing_fields(db, file_record))
    if task_record is not None:
        payload.update(task_retry_fields(task_record))
    return payload


def _resolve_url_prefix(url_prefix: Optional[str], path_prefix: Optional[str]) -> str:
    """Resolve compatible prefix params and return normalized logical path."""
    if url_prefix is None and path_prefix is None:
        return "/"
    if url_prefix is not None and path_prefix is not None:
        normalized_url_prefix = validate_path(url_prefix)
        normalized_path_prefix = validate_path(path_prefix)
        if normalized_url_prefix != normalized_path_prefix:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="url_prefix and path_prefix must be the same when both provided",
            )
        return normalized_url_prefix
    return validate_path(url_prefix if url_prefix is not None else path_prefix or "/")


def _dedupe_workspace_names(workspace_names: Optional[List[str]]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for raw_name in workspace_names or []:
        name = str(raw_name).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def _token_can_read_workspace(ctx: ServiceTokenContext, workspace_id: int) -> bool:
    binding = next((b for b in ctx.bindings if b.workspace_id == workspace_id), None)
    return binding is not None and binding.permission in ("read", "write")


def _merge_l1_applied(values: list[Optional[bool]]) -> Optional[bool]:
    if any(value is True for value in values):
        return True
    if all(value is None for value in values):
        return None
    return False


def _merge_l1_skip_reason(values: list[Optional[str]]) -> Optional[str]:
    reasons = {value for value in values if value is not None}
    if not reasons:
        return None
    if len(reasons) == 1:
        return next(iter(reasons))
    return "mixed"


@dataclass(frozen=True)
class _WorkspaceSearchTarget:
    workspace_id: int
    workspace_name: str
    owner_id: int
    paths: tuple[str, ...] | None = None


@dataclass(frozen=True)
class _WorkspaceSearchOutcome:
    target: _WorkspaceSearchTarget
    response: SearchResponse


def _candidate_limit(
    *, workspace_count: int, final_top_k: int, use_rerank: bool
) -> tuple[int, bool]:
    config = get_config().multi_workspace_search
    desired = final_top_k
    if use_rerank:
        desired = max(
            final_top_k * config.candidate_multiplier,
            config.min_candidates_per_workspace,
        )
    desired = min(desired, config.max_candidates_per_workspace)
    global_budget = max(1, config.max_global_rerank_candidates // workspace_count)
    candidate_limit = max(1, min(desired, global_budget))
    return candidate_limit, candidate_limit < desired


def _candidate_identity(candidate: dict[str, Any]) -> tuple[Any, ...]:
    workspace_id = candidate["_workspace_id"]
    chunk_id = candidate.get("chunk_id")
    if chunk_id:
        return workspace_id, "chunk", chunk_id
    return (
        workspace_id,
        "position",
        candidate.get("file_id"),
        candidate.get("chunk_index"),
    )


def _dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best_by_identity: dict[tuple[Any, ...], dict[str, Any]] = {}
    for candidate in candidates:
        identity = _candidate_identity(candidate)
        previous = best_by_identity.get(identity)
        if previous is None or float(candidate.get("score", 0.0)) > float(
            previous.get("score", 0.0)
        ):
            best_by_identity[identity] = candidate
    return list(best_by_identity.values())


def _multi_workspace_result(candidate: dict[str, Any]) -> MultiWorkspaceSearchResult:
    effective_score = float(
        candidate.get("reranked_score", candidate.get("score", 0.0))
    )
    payload = dict(candidate)
    payload["score"] = effective_score
    payload.pop("retrieval_score", None)
    return MultiWorkspaceSearchResult(
        **payload,
        workspace_id=int(candidate["_workspace_id"]),
        workspace_name=str(candidate["_workspace_name"]),
        retrieval_score=float(candidate.get("score", 0.0)),
        rerank_score=(
            float(candidate["reranked_score"])
            if candidate.get("reranked_score") is not None
            else None
        ),
    )


async def _execute_global_multi_workspace_search(
    *,
    body: ServiceMultiWorkspaceSearchRequest,
    ctx: ServiceTokenContext,
    db: Session,
    workspace_names: list[str],
) -> MultiWorkspaceSearchResponse:
    config = get_config().multi_workspace_search
    if len(workspace_names) > config.max_workspaces:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"multi_space search supports at most {config.max_workspaces} workspaces",
        )

    workspaces = (
        db.query(Workspace).filter(Workspace.name.in_(workspace_names)).all()
    )
    workspace_by_name = {workspace.name: workspace for workspace in workspaces}
    skipped: list[SkippedWorkspace] = []
    targets: list[_WorkspaceSearchTarget] = []
    scope_paths = _resolve_scope_paths(body.paths, body.path_prefix)
    for workspace_name in workspace_names:
        workspace = workspace_by_name.get(workspace_name)
        if workspace is None:
            skipped.append(
                SkippedWorkspace(
                    workspace_name=workspace_name,
                    reason="not_found",
                    message="Workspace not found",
                )
            )
            continue
        if not _token_can_read_workspace(ctx, workspace.id):
            skipped.append(
                SkippedWorkspace(
                    workspace_name=workspace_name,
                    reason="permission_denied",
                    message="Token not authorized for this workspace",
                )
            )
            continue
        targets.append(
            _WorkspaceSearchTarget(
                workspace_id=workspace.id,
                workspace_name=workspace.name,
                owner_id=workspace.owner_id,
                paths=tuple(scope_paths) if scope_paths is not None else None,
            )
        )

    return await _execute_global_workspace_targets(
        body=body,
        db=db,
        targets=targets,
        skipped=skipped,
        requested_workspace_count=len(workspace_names),
        endpoint="service_multi_workspace_candidate",
    )


async def _execute_global_workspace_targets(
    *,
    body: ServiceMultiWorkspaceSearchRequest | FederatedSearchRequest,
    db: Session,
    targets: list[_WorkspaceSearchTarget],
    skipped: list[SkippedWorkspace] | None = None,
    requested_workspace_count: int | None = None,
    endpoint: str,
) -> MultiWorkspaceSearchResponse:
    """Recall bounded per-workspace targets and rank their shared candidate pool once."""
    started_at = time.perf_counter()
    config = get_config().multi_workspace_search
    skipped = [] if skipped is None else skipped
    requested_workspace_count = (
        len(targets)
        if requested_workspace_count is None
        else requested_workspace_count
    )
    if not targets:
        return MultiWorkspaceSearchResponse(
            results=[],
            total=0,
            query_time_ms=(time.perf_counter() - started_at) * 1000,
            workspace_count=0,
            skipped_workspaces=skipped,
            rerank_scope_applied="global",
            rerank_status="disabled" if not body.use_rerank else "success",
            ranking_mode=(
                "global_retrieval_score" if not body.use_rerank else "global_rerank"
            ),
            requested_workspace_count=requested_workspace_count,
        )

    candidate_top_k, candidate_pool_truncated = _candidate_limit(
        workspace_count=len(targets),
        final_top_k=body.top_k,
        use_rerank=body.use_rerank,
    )
    semaphore = asyncio.Semaphore(min(config.recall_concurrency, len(targets)))
    session_factory = sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=db.get_bind(),
    )
    def run_workspace(target: _WorkspaceSearchTarget) -> _WorkspaceSearchOutcome:
        worker_db = session_factory()
        set_trace_context(
            trace_id=uuid.uuid4().hex,
            span_id=None,
            trace_type="retrieval",
            workspace_id=target.workspace_id,
            user_id=target.owner_id,
        )
        try:
            request = SearchRequest(
                query=body.query,
                top_k=candidate_top_k,
                workspace_id=target.workspace_id,
                use_rerank=False,
                use_contextual_retrieval=body.use_contextual_retrieval,
                contextual_l0_top_n=body.contextual_l0_top_n,
                contextual_l1_top_n=body.contextual_l1_top_n,
                contextual_chunk_fetch_multiplier=body.contextual_chunk_fetch_multiplier,
                retrieval_strategy=body.retrieval_strategy,
                use_l1_llm_navigation=body.use_l1_llm_navigation,
                paths=list(target.paths) if target.paths is not None else None,
            )
            response = _execute_search(
                worker_db,
                target.owner_id,
                request,
                endpoint=endpoint,
                rerank_hierarchical_boost=None,
                workspace_access_prevalidated=True,
                allow_shadow=False,
            )
            return _WorkspaceSearchOutcome(target=target, response=response)
        finally:
            worker_db.close()

    async def search_workspace(target: _WorkspaceSearchTarget):
        async with semaphore:
            return await asyncio.to_thread(run_workspace, target)

    raw_outcomes = await asyncio.gather(
        *(search_workspace(target) for target in targets),
        return_exceptions=True,
    )
    outcomes: list[_WorkspaceSearchOutcome] = []
    failed_workspace_count = 0
    for target, outcome in zip(targets, raw_outcomes, strict=True):
        if isinstance(outcome, BaseException):
            failed_workspace_count += 1
            logger.warning(
                "multi_workspace_search.workspace_failed workspace_name=%s exception_type=%s",
                target.workspace_name,
                type(outcome).__name__,
            )
            skipped.append(
                SkippedWorkspace(
                    workspace_name=target.workspace_name,
                    reason="search_failed",
                    message="Workspace search failed",
                )
            )
            continue
        outcomes.append(outcome)

    if targets and not outcomes:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="All workspace searches failed",
        )

    candidates: list[dict[str, Any]] = []
    for outcome in outcomes:
        for result in outcome.response.results:
            candidate = result.model_dump()
            candidate["_workspace_id"] = outcome.target.workspace_id
            candidate["_workspace_name"] = outcome.target.workspace_name
            candidates.append(candidate)
    candidates = _dedupe_candidates(candidates)
    candidates.sort(key=lambda candidate: float(candidate.get("score", 0.0)), reverse=True)
    if len(candidates) > config.max_global_rerank_candidates:
        candidates = candidates[: config.max_global_rerank_candidates]
        candidate_pool_truncated = True
    candidate_count = len(candidates)

    rerank_time_ms = 0.0
    rerank_status: Literal["success", "disabled", "degraded"]
    ranking_mode: Literal[
        "global_rerank", "global_retrieval_score", "workspace_rerank"
    ]
    final_candidates: list[dict[str, Any]]
    if body.use_rerank and candidates:
        rerank_started_at = time.perf_counter()
        try:
            reranked = Reranker().rerank(
                body.query,
                candidates,
                top_k=body.top_k,
                original_score_weight=0.0,
            )
            if any(item.get("reranked_score") is not None for item in reranked):
                final_candidates = reranked
                rerank_status = "success"
                ranking_mode = "global_rerank"
            else:
                final_candidates = candidates[: body.top_k]
                rerank_status = "degraded"
                ranking_mode = "global_retrieval_score"
        except Exception as exc:
            logger.warning(
                "multi_workspace_search.rerank_degraded exception_type=%s",
                type(exc).__name__,
            )
            final_candidates = candidates[: body.top_k]
            rerank_status = "degraded"
            ranking_mode = "global_retrieval_score"
        rerank_time_ms = (time.perf_counter() - rerank_started_at) * 1000
    elif body.use_rerank:
        final_candidates = []
        rerank_status = "success"
        ranking_mode = "global_rerank"
    else:
        final_candidates = candidates[: body.top_k]
        rerank_status = "disabled"
        ranking_mode = "global_retrieval_score"

    results = [_multi_workspace_result(candidate) for candidate in final_candidates]
    return MultiWorkspaceSearchResponse(
        results=results,
        total=len(results),
        query_time_ms=(time.perf_counter() - started_at) * 1000,
        workspace_count=len(outcomes),
        l1_llm_applied=_merge_l1_applied(
            [outcome.response.l1_llm_applied for outcome in outcomes]
        ),
        l1_llm_skip_reason=_merge_l1_skip_reason(
            [outcome.response.l1_llm_skip_reason for outcome in outcomes]
        ),
        skipped_workspaces=skipped,
        rerank_scope_applied="global",
        rerank_status=rerank_status,
        ranking_mode=ranking_mode,
        candidate_count=candidate_count,
        candidate_pool_truncated=candidate_pool_truncated,
        requested_workspace_count=requested_workspace_count,
        failed_workspace_count=failed_workspace_count,
        rerank_time_ms=rerank_time_ms,
    )


def _require_search_grants_enabled():
    config = get_config().search_grant
    if not config.enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "search_grants_disabled",
                "message": "Search grants are not enabled",
            },
        )
    return config


def _failed_scope_outcome(
    scope_ref: str,
    code: str,
    message: str,
) -> FederatedScopeOutcome:
    return FederatedScopeOutcome(
        scope_ref=scope_ref,
        status="failed",
        code=code,
        message=message,
    )


@router.post("/search-grants", response_model=SearchGrantIssueResponse)
async def service_issue_search_grants(
    body: SearchGrantIssueRequest,
    ctx: ServiceTokenContext = Depends(get_service_token_context),
    db: Session = Depends(get_db),
) -> SearchGrantIssueResponse:
    """Exchange one service token for query-bound, short-lived scope grants."""
    started_at = time.perf_counter()
    config = _require_search_grants_enabled()
    scope_refs = [scope.scope_ref for scope in body.scopes]
    if len(scope_refs) != len(set(scope_refs)):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "duplicate_scope_ref",
                "message": "Search grant scopes contain duplicate scope_ref values",
            },
        )
    if len(body.scopes) > config.max_scopes_per_issue:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "too_many_scopes",
                "message": (
                    "Search grant issue supports at most "
                    f"{config.max_scopes_per_issue} scopes"
                ),
            },
        )

    workspace_names = {scope.workspace_name.strip() for scope in body.scopes}
    workspaces = db.query(Workspace).filter(Workspace.name.in_(workspace_names)).all()
    workspace_by_name = {workspace.name: workspace for workspace in workspaces}
    grants: list[IssuedSearchGrant] = []
    failures: list[SearchGrantIssueFailure] = []

    for scope in body.scopes:
        workspace = workspace_by_name.get(scope.workspace_name.strip())
        if workspace is None:
            failures.append(
                SearchGrantIssueFailure(
                    scope_ref=scope.scope_ref,
                    code="workspace_not_found",
                    message="Workspace not found",
                )
            )
            continue
        if not _token_can_read_workspace(ctx, workspace.id):
            failures.append(
                SearchGrantIssueFailure(
                    scope_ref=scope.scope_ref,
                    code="workspace_permission_denied",
                    message="Token not authorized for this workspace",
                )
            )
            continue
        try:
            allowed_paths = normalize_grant_paths(scope.allowed_paths)
        except ValueError:
            failures.append(
                SearchGrantIssueFailure(
                    scope_ref=scope.scope_ref,
                    code="invalid_scope_path",
                    message="Scope path is invalid",
                )
            )
            continue

        grant, expires_at = create_search_grant(
            workspace_id=workspace.id,
            workspace_name=workspace.name,
            allowed_paths=allowed_paths,
            scope_ref=scope.scope_ref,
            request_id=body.request_id,
            query_hash=body.query_hash,
            max_top_k=body.max_top_k,
            config=config,
        )
        grants.append(
            IssuedSearchGrant(
                scope_ref=scope.scope_ref,
                grant=grant,
                expires_at=expires_at,
            )
        )

    logger.info(
        "search_grants.issued request_id=%s instance_id=%s "
        "requested_scope_count=%s granted_scope_count=%s failed_scope_count=%s "
        "grant_issue_ms=%.2f",
        body.request_id,
        config.instance_id,
        len(body.scopes),
        len(grants),
        len(failures),
        (time.perf_counter() - started_at) * 1000,
    )
    return SearchGrantIssueResponse(
        instance_id=config.instance_id or "",
        grants=grants,
        failures=failures,
    )


@router.post("/federated/search", response_model=FederatedSearchResponse)
async def service_federated_search(
    body: FederatedSearchRequest,
    x_openrag_token: str | None = Header(default=None, alias="X-OpenRag-Token"),
    db: Session = Depends(get_db),
) -> FederatedSearchResponse:
    """Search scopes authorized solely by short-lived grants."""
    started_at = time.perf_counter()
    config = _require_search_grants_enabled()
    if x_openrag_token is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "service_token_not_allowed",
                "message": "Federated search does not accept a service token",
            },
        )
    grant_values = [grant.get_secret_value() for grant in body.grants]
    if any(not grant.strip() for grant in grant_values) or len(grant_values) != len(
        set(grant_values)
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "invalid_grants",
                "message": "Federated search grants must be non-blank and unique",
            },
        )
    if len(body.grants) > config.max_grants_per_search:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "too_many_grants",
                "message": (
                    "Federated search accepts at most "
                    f"{config.max_grants_per_search} grants"
                ),
            },
        )

    ordered_scope_refs: list[str] = []
    outcome_by_ref: dict[str, FederatedScopeOutcome] = {}
    claims_by_workspace: dict[int, list[SearchGrantClaims]] = {}
    accepted_scope_refs: set[str] = set()
    verify_started_at = time.perf_counter()
    for index, grant in enumerate(grant_values):
        fallback_ref = f"grant-{index + 1}"
        scope_ref = unverified_scope_ref(grant) or fallback_ref
        if scope_ref not in ordered_scope_refs:
            ordered_scope_refs.append(scope_ref)
        try:
            claims = decode_search_grant(grant, config=config)
            validate_search_grant_context(
                claims,
                request_id=body.request_id,
                query=body.query,
                top_k=body.top_k,
            )
        except SearchGrantError as exc:
            outcome_by_ref[scope_ref] = _failed_scope_outcome(
                scope_ref,
                exc.code,
                str(exc),
            )
            continue

        scope_ref = claims.scope_ref
        if scope_ref not in ordered_scope_refs:
            ordered_scope_refs.append(scope_ref)
        if scope_ref in accepted_scope_refs or scope_ref in outcome_by_ref:
            outcome_by_ref[scope_ref] = _failed_scope_outcome(
                scope_ref,
                "scope_overlap_conflict",
                "Duplicate scope_ref",
            )
            accepted_scope_refs.discard(scope_ref)
            continue
        accepted_scope_refs.add(scope_ref)
        claims_by_workspace.setdefault(claims.workspace_id, []).append(claims)
    grant_verify_ms = (time.perf_counter() - verify_started_at) * 1000

    workspace_ids = list(claims_by_workspace)
    workspaces = (
        db.query(Workspace).filter(Workspace.id.in_(workspace_ids)).all()
        if workspace_ids
        else []
    )
    workspace_by_id = {workspace.id: workspace for workspace in workspaces}
    scopes_by_workspace: dict[int, list[SearchGrantClaims]] = {}
    targets: list[_WorkspaceSearchTarget] = []

    for workspace_id, workspace_claims in claims_by_workspace.items():
        active_claims = [
            claims
            for claims in workspace_claims
            if claims.scope_ref in accepted_scope_refs
            and claims.scope_ref not in outcome_by_ref
        ]
        if not active_claims:
            continue
        workspace = workspace_by_id.get(workspace_id)
        if workspace is None or any(
            claims.workspace_name != workspace.name for claims in active_claims
        ):
            for claims in active_claims:
                outcome_by_ref[claims.scope_ref] = _failed_scope_outcome(
                    claims.scope_ref,
                    "workspace_not_found",
                    "Workspace not found",
                )
            continue

        root_claims = [claims for claims in active_claims if claims.allowed_paths is None]
        explicit_paths: dict[str, str] = {}
        non_empty_explicit_claims = [
            claims for claims in active_claims if claims.allowed_paths
        ]
        conflict = len(root_claims) > 1 or bool(
            root_claims and non_empty_explicit_claims
        )
        if not conflict:
            for claims in active_claims:
                for path in claims.allowed_paths or ():
                    owner = explicit_paths.get(path)
                    if owner is not None and owner != claims.scope_ref:
                        conflict = True
                        break
                    explicit_paths[path] = claims.scope_ref
                if conflict:
                    break
        if conflict:
            for claims in active_claims:
                outcome_by_ref[claims.scope_ref] = _failed_scope_outcome(
                    claims.scope_ref,
                    "scope_overlap_conflict",
                    "Workspace scope ranges overlap ambiguously",
                )
            continue

        try:
            target_paths = (
                None
                if root_claims
                else normalize_grant_paths(list(explicit_paths))
            )
        except ValueError:
            for claims in active_claims:
                outcome_by_ref[claims.scope_ref] = _failed_scope_outcome(
                    claims.scope_ref,
                    "scope_overlap_conflict",
                    "Combined Workspace path scope exceeds the supported limit",
                )
            continue
        scopes_by_workspace[workspace_id] = active_claims
        targets.append(
            _WorkspaceSearchTarget(
                workspace_id=workspace.id,
                workspace_name=workspace.name,
                owner_id=workspace.owner_id,
                paths=target_paths,
            )
        )

    if not targets:
        scope_outcomes = [
            outcome_by_ref.get(scope_ref)
            or _failed_scope_outcome(scope_ref, "grant_invalid", "Invalid search grant")
            for scope_ref in ordered_scope_refs
        ]
        failure_codes = {item.code for item in scope_outcomes if item.code}
        error_code = next(iter(failure_codes)) if len(failure_codes) == 1 else "grant_invalid"
        error_status = (
            status.HTTP_403_FORBIDDEN
            if error_code
            in {
                "grant_expired",
                "grant_invalid",
                "grant_context_mismatch",
                "grant_instance_mismatch",
            }
            else status.HTTP_404_NOT_FOUND
            if error_code == "workspace_not_found"
            else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(
            status_code=error_status,
            detail={
                "code": error_code,
                "message": "No valid search grant scopes",
                "scope_outcomes": [item.model_dump() for item in scope_outcomes],
            },
        )

    try:
        execution = await _execute_global_workspace_targets(
            body=body,
            db=db,
            targets=targets,
            requested_workspace_count=len(targets),
            endpoint="service_federated_candidate",
        )
    except HTTPException as exc:
        if exc.status_code != status.HTTP_503_SERVICE_UNAVAILABLE:
            raise
        for workspace_claims in scopes_by_workspace.values():
            for claims in workspace_claims:
                outcome_by_ref[claims.scope_ref] = _failed_scope_outcome(
                    claims.scope_ref,
                    "search_failed",
                    "Workspace search failed",
                )
        scope_outcomes = [
            outcome_by_ref.get(scope_ref)
            or _failed_scope_outcome(scope_ref, "search_failed", "Workspace search failed")
            for scope_ref in ordered_scope_refs
        ]
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "search_failed",
                "message": "All workspace searches failed",
                "scope_outcomes": [item.model_dump() for item in scope_outcomes],
            },
        ) from exc
    failed_workspace_names = {
        item.workspace_name
        for item in execution.skipped_workspaces
        if item.reason == "search_failed"
    }
    for target in targets:
        for claims in scopes_by_workspace[target.workspace_id]:
            if target.workspace_name in failed_workspace_names:
                outcome_by_ref[claims.scope_ref] = _failed_scope_outcome(
                    claims.scope_ref,
                    "search_failed",
                    "Workspace search failed",
                )
            elif claims.scope_ref not in outcome_by_ref:
                outcome_by_ref[claims.scope_ref] = FederatedScopeOutcome(
                    scope_ref=claims.scope_ref,
                    status="succeeded",
                )

    results: list[FederatedSearchResult] = []
    for result in execution.results:
        uri = result.uri
        workspace_claims = scopes_by_workspace.get(result.workspace_id, [])
        matched_scope_ref: str | None = None
        normalized_uri: tuple[str, ...] | None = None
        if uri:
            try:
                normalized_uri = normalize_grant_paths([uri])
            except ValueError:
                normalized_uri = None
        root_claim = next(
            (claims for claims in workspace_claims if claims.allowed_paths is None),
            None,
        )
        if root_claim is not None and normalized_uri:
            matched_scope_ref = root_claim.scope_ref
        elif normalized_uri:
            matches = [
                (len(path), claims.scope_ref)
                for claims in workspace_claims
                for path in claims.allowed_paths or ()
                if normalized_uri and path_is_within(normalized_uri[0], path)
            ]
            if matches:
                matches.sort(key=lambda item: (-item[0], item[1]))
                matched_scope_ref = matches[0][1]
        if matched_scope_ref is None:
            logger.warning(
                "federated_search.result_scope_invalid request_id=%s workspace_id=%s",
                body.request_id,
                result.workspace_id,
            )
            continue
        result_payload = result.model_dump()
        if uri and normalized_uri:
            result_payload["uri"] = normalized_uri[0]
        results.append(
            FederatedSearchResult(
                **result_payload,
                scope_ref=matched_scope_ref,
            )
        )

    scope_outcomes = [
        outcome_by_ref.get(scope_ref)
        or _failed_scope_outcome(scope_ref, "grant_invalid", "Invalid search grant")
        for scope_ref in ordered_scope_refs
    ]
    logger.info(
        "federated_search.completed request_id=%s instance_id=%s "
        "grant_verify_ms=%.2f requested_scope_count=%s succeeded_scope_count=%s "
        "failed_scope_count=%s workspace_count=%s candidate_count=%s "
        "recall_ms=%.2f rerank_time_ms=%.2f candidate_pool_truncated=%s "
        "total_ms=%.2f",
        body.request_id,
        config.instance_id,
        grant_verify_ms,
        len(ordered_scope_refs),
        sum(item.status == "succeeded" for item in scope_outcomes),
        sum(item.status == "failed" for item in scope_outcomes),
        execution.workspace_count,
        execution.candidate_count,
        max(0.0, execution.query_time_ms - execution.rerank_time_ms),
        execution.rerank_time_ms,
        execution.candidate_pool_truncated,
        (time.perf_counter() - started_at) * 1000,
    )
    return FederatedSearchResponse(
        results=results,
        scope_outcomes=scope_outcomes,
        total=len(results),
        query_time_ms=(time.perf_counter() - started_at) * 1000,
        workspace_count=execution.workspace_count,
        rerank_status=execution.rerank_status,
        ranking_mode=execution.ranking_mode,
        candidate_count=execution.candidate_count,
        candidate_pool_truncated=execution.candidate_pool_truncated,
        failed_workspace_count=execution.failed_workspace_count,
        rerank_time_ms=execution.rerank_time_ms,
    )


def resolve_preview_target(
    db: Session,
    *,
    workspace_id: int,
    file_id: int,
    chunk_id: str,
    chunk_index: int | None,
) -> tuple[DbFile, DocumentChunk]:
    file_row = (
        db.query(DbFile)
        .filter(
            DbFile.id == file_id,
            DbFile.workspace_id == workspace_id,
            DbFile.deleted_at.is_(None),
        )
        .first()
    )
    if file_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    if file_row.is_directory:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="File is a directory")

    chunk_row = (
        db.query(DocumentChunk)
        .filter(
            DocumentChunk.chunk_id == chunk_id,
            DocumentChunk.file_id == file_row.id,
            DocumentChunk.workspace_id == workspace_id,
        )
        .first()
    )
    if chunk_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chunk not found")
    if chunk_index is not None and chunk_row.chunk_index != chunk_index:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="chunk_index does not match chunk",
        )
    return file_row, chunk_row


def build_preview_url(*, base_url: str, token: str) -> str:
    return f"{(base_url or '').strip().rstrip('/')}/embed/document-preview#token={token}"


@router.post(
    "/workspaces/{workspace_name}/documents",
    status_code=status.HTTP_201_CREATED,
)
async def service_upload_document(
    workspace_name: str,
    path: str = Form(..., description="Parent directory logical path"),
    file: UploadFile = File(...),
    parser_type: str = Form(default="auto"),
    create_dirs: bool = Form(
        default=False,
        description="Create missing parent directories (mkdir -p) before upload",
    ),
    tag: str | None = Form(default=None, description="Unique tag within the workspace"),
    ctx: ServiceTokenContext = Depends(get_service_token_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    ws = require_workspace_for_name(db, workspace_name)
    assert_token_workspace_permission(ctx, ws.id, "write")
    body = await read_upload_content(file)
    # Parent directories (when create_dirs) are materialised inside ingest_new_file
    # via require_parent_dir=False, AFTER the tag dup-check — so a tag conflict aborts
    # with 409 before any empty directory rows are created.
    file_record, task_record = ingest_new_file(
        db,
        ws,
        ws.owner_id,
        parent_logical_path=path,
        upload_filename=file.filename or "unnamed",
        file_content=body,
        content_type=file.content_type,
        parser_type=parser_type,
        require_parent_dir=not create_dirs,
        duplicate_status_code=status.HTTP_409_CONFLICT,
        tag=tag,
    )
    return _upload_response_dict(db, file_record, task_record)


@router.put("/workspaces/{workspace_name}/documents/by-path")
async def service_replace_document(
    workspace_name: str,
    path: str = Query(..., description="Full file logical path"),
    file: UploadFile = File(...),
    parser_type: str = Form(default="auto"),
    ctx: ServiceTokenContext = Depends(get_service_token_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    ws = require_workspace_for_name(db, workspace_name)
    assert_token_workspace_permission(ctx, ws.id, "write")
    p = validate_path(path)
    row = (
        db.query(DbFile)
        .filter(
            DbFile.workspace_id == ws.id,
            DbFile.uri == p,
            DbFile.is_directory.is_(False),
            DbFile.deleted_at.is_(None),
        )
        .first()
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    body = await read_upload_content(file)
    task = replace_file_content(
        db,
        ws,
        ws.owner_id,
        row,
        new_content=body,
        content_type=file.content_type,
        parser_type=parser_type,
    )
    db.refresh(row)
    return _upload_response_dict(db, row, task)


@router.put("/workspaces/{workspace_name}/documents/upsert-by-tag")
async def service_upsert_document_by_tag(
    workspace_name: str,
    tag: str = Form(..., min_length=1, description="Per-workspace unique tag (idempotency key)"),
    target_path: str = Form(..., description="Full file logical path, e.g. /dir/name.pdf"),
    file: UploadFile = File(...),
    parser_type: str = Form(default="auto"),
    create_dirs: bool = Form(
        default=False,
        description="Create missing parent directories (mkdir -p) before upsert",
    ),
    ctx: ServiceTokenContext = Depends(get_service_token_context),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Idempotent-by-tag upsert: create (201) / update-in-place (200) / move+replace (200).

    ``target_path`` is the FULL file path; the uri is never derived from the multipart
    filename (spec §4.8/§8.5#10).
    """
    ws = require_workspace_for_name(db, workspace_name)
    assert_token_workspace_permission(ctx, ws.id, "write")
    body = await read_upload_content(file)
    file_record, task_record, action = upsert_file_by_tag(
        db,
        ws,
        ws.owner_id,
        tag=tag,
        target_path=target_path,
        file_content=body,
        content_type=file.content_type,
        parser_type=parser_type,
        create_dirs=create_dirs,
    )
    status_code = status.HTTP_201_CREATED if action == "created" else status.HTTP_200_OK
    payload = _upload_response_dict(db, file_record, task_record)
    payload["action"] = action
    return JSONResponse(status_code=status_code, content=payload)


@router.delete("/workspaces/{workspace_name}/documents/by-path")
async def service_delete_document_by_path(
    workspace_name: str,
    path: str = Query(..., description="Full file logical path"),
    background: bool = Query(True, description="true: async soft-delete (202); false: sync physical delete (200)"),
    ctx: ServiceTokenContext = Depends(get_service_token_context),
    db: Session = Depends(get_db),
) -> JSONResponse:
    ws = require_workspace_for_name(db, workspace_name)
    assert_token_workspace_permission(ctx, ws.id, "write")
    p = validate_path(path)
    row = (
        db.query(DbFile)
        .filter(
            DbFile.workspace_id == ws.id,
            DbFile.uri == p,
            DbFile.is_directory.is_(False),
            DbFile.deleted_at.is_(None),
        )
        .first()
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    if background:
        task = _release_tag_and_soft_delete(db, row, user_id=ws.owner_id)
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={"message": "File deletion queued", "task_id": task.id, "async": True},
        )
    try:
        delete_file_with_storage(db, row, ws)
    except FileStorageCleanupError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=exc.public_message,
        ) from exc
    return JSONResponse(status_code=status.HTTP_200_OK, content={"message": "File deleted", "async": False})


@router.post("/workspaces/{workspace_name}/documents/by-path/retry")
async def service_retry_document_by_path(
    workspace_name: str,
    path: str = Query(..., description="Full file logical path"),
    ctx: ServiceTokenContext = Depends(get_service_token_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    ws = require_workspace_for_name(db, workspace_name)
    assert_token_workspace_permission(ctx, ws.id, "write")
    p = validate_path(path)
    file_record = (
        db.query(DbFile)
        .filter(
            DbFile.workspace_id == ws.id,
            DbFile.uri == p,
            DbFile.is_directory.is_(False),
            DbFile.deleted_at.is_(None),
        )
        .first()
    )
    if file_record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    retry_failed_document_processing(db, file_record)
    db.refresh(file_record)
    return _document_summary(db, file_record)


@router.post("/workspaces/{workspace_name}/documents/{document_id}/retry")
async def service_retry_document_by_id(
    workspace_name: str,
    document_id: int,
    ctx: ServiceTokenContext = Depends(get_service_token_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    ws = require_workspace_for_name(db, workspace_name)
    assert_token_workspace_permission(ctx, ws.id, "write")
    file_record = _get_service_document_by_id(db, ws.id, document_id)
    retry_failed_document_processing(db, file_record)
    db.refresh(file_record)
    return _document_summary(db, file_record)


@router.post("/workspaces/multi_space/search", response_model=MultiWorkspaceSearchResponse)
async def service_multi_workspace_semantic_search(
    body: ServiceMultiWorkspaceSearchRequest,
    ctx: ServiceTokenContext = Depends(get_service_token_context),
    db: Session = Depends(get_db),
) -> MultiWorkspaceSearchResponse:
    """
    Semantic search across multiple workspace names visible to the service token.
    Inaccessible workspace names are reported in ``skipped_workspaces``.
    """
    workspace_names = _dedupe_workspace_names(body.workspace_names)
    if not workspace_names:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="workspace_names is required for multi_space search",
        )
    if body.rerank_scope == "global":
        return await _execute_global_multi_workspace_search(
            body=body,
            ctx=ctx,
            db=db,
            workspace_names=workspace_names,
        )

    legacy_started_at = time.perf_counter()
    results: list[MultiWorkspaceSearchResult] = []
    skipped: list[SkippedWorkspace] = []
    l1_applied_values: list[Optional[bool]] = []
    l1_skip_reasons: list[Optional[str]] = []
    workspace_count = 0

    try:
        for workspace_name in workspace_names:
            ws = db.query(Workspace).filter(Workspace.name == workspace_name).first()
            if ws is None:
                skipped.append(
                    SkippedWorkspace(
                        workspace_name=workspace_name,
                        reason="not_found",
                        message="Workspace not found",
                    )
                )
                continue
            if not _token_can_read_workspace(ctx, ws.id):
                skipped.append(
                    SkippedWorkspace(
                        workspace_name=workspace_name,
                        reason="permission_denied",
                        message="Token not authorized for this workspace",
                    )
                )
                continue

            search_req = SearchRequest(
                query=body.query,
                top_k=body.top_k,
                workspace_id=ws.id,
                use_rerank=body.use_rerank,
                use_contextual_retrieval=body.use_contextual_retrieval,
                contextual_l0_top_n=body.contextual_l0_top_n,
                contextual_l1_top_n=body.contextual_l1_top_n,
                contextual_chunk_fetch_multiplier=body.contextual_chunk_fetch_multiplier,
                retrieval_strategy=body.retrieval_strategy,
                use_l1_llm_navigation=body.use_l1_llm_navigation,
                paths=_resolve_scope_paths(body.paths, body.path_prefix),
            )
            resp = _execute_search(
                db,
                ws.owner_id,
                search_req,
                endpoint="service_multi_workspace",
                rerank_hierarchical_boost=None,
                workspace_access_prevalidated=True,
            )
            workspace_count += 1
            l1_applied_values.append(resp.l1_llm_applied)
            l1_skip_reasons.append(resp.l1_llm_skip_reason)
            for hit in resp.results:
                results.append(
                    MultiWorkspaceSearchResult(
                        **hit.model_dump(),
                        workspace_id=ws.id,
                        workspace_name=ws.name,
                    )
                )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Service multi-workspace search failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc

    results = sorted(results, key=lambda hit: hit.score, reverse=True)[: body.top_k]
    return MultiWorkspaceSearchResponse(
        results=results,
        total=len(results),
        query_time_ms=(time.perf_counter() - legacy_started_at) * 1000,
        workspace_count=workspace_count,
        l1_llm_applied=_merge_l1_applied(l1_applied_values),
        l1_llm_skip_reason=_merge_l1_skip_reason(l1_skip_reasons),
        skipped_workspaces=skipped,
        rerank_scope_applied="workspace",
        rerank_status="success" if body.use_rerank else "disabled",
        ranking_mode="workspace_rerank",
        candidate_count=len(results),
        requested_workspace_count=len(workspace_names),
    )


@router.post("/workspaces/{workspace_name}/search", response_model=SearchResponse)
async def service_semantic_search(
    workspace_name: str,
    body: ServiceSearchRequest,
    ctx: ServiceTokenContext = Depends(get_service_token_context),
    db: Session = Depends(get_db),
) -> SearchResponse:
    """
    Semantic search scoped to the workspace from the URL path.
    Uses the same retrieval pipeline as ``POST /search`` with ``user_id=workspace.owner_id``.
    """
    ws = require_workspace_for_name(db, workspace_name)
    assert_token_workspace_permission(ctx, ws.id, "read")

    search_req = SearchRequest(
        query=body.query,
        top_k=body.top_k,
        workspace_id=ws.id,
        use_rerank=body.use_rerank,
        use_contextual_retrieval=body.use_contextual_retrieval,
        contextual_l0_top_n=body.contextual_l0_top_n,
        contextual_l1_top_n=body.contextual_l1_top_n,
        contextual_chunk_fetch_multiplier=body.contextual_chunk_fetch_multiplier,
        retrieval_strategy=body.retrieval_strategy,
        use_l1_llm_navigation=body.use_l1_llm_navigation,
        paths=_resolve_scope_paths(body.paths, body.path_prefix),
    )
    try:
        resp = _execute_search(
            db,
            ws.owner_id,
            search_req,
            endpoint="service_semantic",
            rerank_hierarchical_boost=None,
            workspace_access_prevalidated=True,
        )
        return resp
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Service search failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc


@router.post(
    "/workspaces/{workspace_name}/preview-links",
    response_model=ServicePreviewLinkResponse,
)
async def service_create_preview_link(
    workspace_name: str,
    body: ServicePreviewLinkRequest,
    ctx: ServiceTokenContext = Depends(get_service_token_context),
    db: Session = Depends(get_db),
) -> ServicePreviewLinkResponse:
    ws = require_workspace_for_name(db, workspace_name)
    assert_token_workspace_permission(ctx, ws.id, "read")
    file_row, chunk_row = resolve_preview_target(
        db,
        workspace_id=ws.id,
        file_id=body.file_id,
        chunk_id=body.chunk_id,
        chunk_index=body.chunk_index,
    )
    token, expires_at = create_preview_token(
        workspace_id=ws.id,
        file_id=file_row.id,
        chunk_id=chunk_row.chunk_id,
        chunk_index=chunk_row.chunk_index,
        ttl_seconds=body.ttl_seconds,
    )
    claims = decode_preview_token(token)
    return ServicePreviewLinkResponse(
        preview_url=build_preview_url(
            base_url=get_preview_public_web_base_url(),
            token=token,
        ),
        expires_at=expires_at,
        ttl_seconds=claims.exp - claims.iat,
    )


@router.get("/workspaces/{workspace_name}/tree")
async def service_workspace_tree(
    workspace_name: str,
    path_prefix: str = Query(default="/", description="Logical path prefix"),
    ctx: ServiceTokenContext = Depends(get_service_token_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    ws = require_workspace_for_name(db, workspace_name)
    assert_token_workspace_permission(ctx, ws.id, "read")
    return build_nested_tree(db, ws.id, path_prefix)


@router.get("/workspaces/{workspace_name}/children")
async def service_workspace_children(
    workspace_name: str,
    path: str = Query(..., description="Directory logical path"),
    ctx: ServiceTokenContext = Depends(get_service_token_context),
    db: Session = Depends(get_db),
) -> List[dict[str, Any]]:
    ws = require_workspace_for_name(db, workspace_name)
    assert_token_workspace_permission(ctx, ws.id, "read")
    rows = list_direct_children(db, ws.id, path)
    return [_file_summary(f) for f in rows]


@router.get("/workspaces/{workspace_name}/entries/by-prefix")
async def service_workspace_entries_by_prefix(
    workspace_name: str,
    url_prefix: Optional[str] = Query(
        default=None,
        description="URL/逻辑路径前缀，返回该前缀下（含自身）的所有目录和文件",
    ),
    path_prefix: Optional[str] = Query(
        default=None,
        description="兼容参数，与 url_prefix 等价",
    ),
    ctx: ServiceTokenContext = Depends(get_service_token_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    ws = require_workspace_for_name(db, workspace_name)
    assert_token_workspace_permission(ctx, ws.id, "read")
    prefix = _resolve_url_prefix(url_prefix, path_prefix)
    rows = list_entries_by_prefix(db, ws.id, prefix)
    return {
        "url_prefix": prefix,
        "total": len(rows),
        "items": [_file_summary(f) for f in rows],
    }


@router.get("/workspaces/{workspace_name}/documents/by-path")
async def service_document_by_path(
    workspace_name: str,
    path: str = Query(..., description="File logical path"),
    ctx: ServiceTokenContext = Depends(get_service_token_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    ws = require_workspace_for_name(db, workspace_name)
    assert_token_workspace_permission(ctx, ws.id, "read")
    f = get_file_document_by_path(db, ws.id, path)
    payload = _document_summary(db, f)
    payload.update(
        {
            "owner_id": f.owner_id,
            "parser_type": f.parser_type,
            "created_at": f.created_at.isoformat() if f.created_at else None,
        }
    )
    return payload


@router.get("/workspaces/{workspace_name}/documents/by-tag")
async def service_document_by_tag(
    workspace_name: str,
    tag: str = Query(..., min_length=1, description="Exact tag within this workspace"),
    ctx: ServiceTokenContext = Depends(get_service_token_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    ws = require_workspace_for_name(db, workspace_name)
    assert_token_workspace_permission(ctx, ws.id, "read")
    f = (
        db.query(DbFile)
        .filter(
            DbFile.workspace_id == ws.id,
            DbFile.tag == tag,
            DbFile.is_directory.is_(False),
            DbFile.deleted_at.is_(None),
        )
        .first()
    )
    if f is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No document with this tag")
    return _document_summary(db, f)


@router.get("/workspaces/{workspace_name}/documents/search-by-name")
async def service_search_documents_by_name(
    workspace_name: str,
    filename: str = Query(default="", description="Filename substring (case-insensitive); empty returns all files"),
    path_prefix: str = Query(default="/", description="Limit search scope to this path prefix"),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    ctx: ServiceTokenContext = Depends(get_service_token_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    ws = require_workspace_for_name(db, workspace_name)
    assert_token_workspace_permission(ctx, ws.id, "read")
    rows, total = search_documents_by_name(db, ws.id, filename, path_prefix, skip, limit)
    return {
        "items": [_document_summary(db, f) for f in rows],
        "total": total,
        "skip": skip,
        "limit": limit,
    }
