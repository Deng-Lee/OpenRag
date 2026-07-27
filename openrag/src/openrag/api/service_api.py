"""Machine-to-machine API (service token only; no JWT on these routes)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from openrag.api.deps import get_db, get_service_token_context
from openrag.api.search_api import (
    SearchRequest,
    SearchResponse,
    SearchResult,
    _execute_search,
)
from openrag.config import get_preview_public_web_base_url
from openrag.models.document_chunk import DocumentChunk
from openrag.models.file import File as DbFile
from openrag.models.task import Task
from openrag.models.workspace import Workspace
from openrag.services.file_deletion import (
    _release_tag_and_soft_delete,
    delete_file_with_storage,
)
from openrag.services.file_ingest import (
    ingest_new_file,
    replace_file_content,
    upsert_file_by_tag,
    validate_path,
)
from openrag.services.preview_token_service import create_preview_token, decode_preview_token
from openrag.services.document_retry_status import (
    document_processing_fields,
    retry_failed_document_processing,
    task_retry_fields,
)
from openrag.services.service_token_service import (
    ServiceTokenContext,
    assert_token_workspace_permission,
    require_workspace_for_name,
)
from openrag.services.workspace_file_tree import (
    build_nested_tree,
    get_file_document_by_path,
    list_entries_by_prefix,
    list_direct_children,
    search_documents_by_name,
)

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
    use_rerank: bool = True
    use_contextual_retrieval: bool = False
    contextual_l0_top_n: int = Field(40, ge=5, le=200)
    contextual_l1_top_n: int = Field(30, ge=5, le=200)
    contextual_chunk_fetch_multiplier: int = Field(4, ge=1, le=20)
    retrieval_strategy: str = "auto"
    use_l1_llm_navigation: bool = False


class ServiceMultiWorkspaceSearchRequest(BaseModel):
    """Semantic search body for service token API across multiple workspaces."""

    workspace_names: Optional[List[str]] = None
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
    use_rerank: bool = True
    use_contextual_retrieval: bool = False
    contextual_l0_top_n: int = Field(40, ge=5, le=200)
    contextual_l1_top_n: int = Field(30, ge=5, le=200)
    contextual_chunk_fetch_multiplier: int = Field(4, ge=1, le=20)
    retrieval_strategy: str = "auto"
    use_l1_llm_navigation: bool = False


class SkippedWorkspace(BaseModel):
    workspace_name: str
    reason: str
    message: str


class MultiWorkspaceSearchResult(SearchResult):
    workspace_id: int
    workspace_name: str


class MultiWorkspaceSearchResponse(BaseModel):
    results: list[MultiWorkspaceSearchResult]
    total: int
    query_time_ms: float
    workspace_count: int
    l1_llm_applied: Optional[bool] = None
    l1_llm_skip_reason: Optional[str] = None
    skipped_workspaces: list[SkippedWorkspace]


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
    body = await file.read()
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
    body = await file.read()
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
    body = await file.read()
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
    delete_file_with_storage(db, row, ws)
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

    results: list[MultiWorkspaceSearchResult] = []
    skipped: list[SkippedWorkspace] = []
    l1_applied_values: list[Optional[bool]] = []
    l1_skip_reasons: list[Optional[str]] = []
    query_time_ms = 0.0
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
            query_time_ms += resp.query_time_ms
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
        query_time_ms=query_time_ms,
        workspace_count=workspace_count,
        l1_llm_applied=_merge_l1_applied(l1_applied_values),
        l1_llm_skip_reason=_merge_l1_skip_reason(l1_skip_reasons),
        skipped_workspaces=skipped,
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
