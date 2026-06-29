"""Workspace-scoped file access helpers and route mount point."""

import logging
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from openrag.api.deps import get_current_active_user, get_db
from openrag.models import DocumentParseArtifact
from openrag.models.document_chunk import DocumentChunk
from openrag.models.file import File as FileModel
from openrag.models.user import User
from openrag.models.workspace import Workspace
from openrag.services.file_preview import build_file_preview
from openrag.services.workspace_service import WorkspaceService
from openrag.storage.minio_storage import MinioStorage


router = APIRouter(prefix="/workspaces/{workspace_id}/files", tags=["workspace-files"])

logger = logging.getLogger(__name__)


_SIMPLE_STATUS_MAP: dict[str, str] = {
    "pending": "processing",
    "parsing": "processing",
    "building_hierarchy": "processing",
    "embedding": "processing",
    "completed": "done",
    "failed": "failed",
}


class WorkspaceFileSummary(BaseModel):
    id: int
    workspace_id: int
    name: str
    uri: str
    mime_type: Optional[str] = None
    processing_status: Optional[str] = None
    simple_status: Optional[str] = None
    total_chunks: int


class WorkspaceDocumentChunkItem(BaseModel):
    file_id: int
    workspace_id: int
    filename: str
    chunk_id: str
    chunk_index: int
    text: str
    is_truncated: bool
    page: int
    bbox_x0: Optional[float] = None
    bbox_y0: Optional[float] = None
    bbox_x1: Optional[float] = None
    bbox_y1: Optional[float] = None
    source_char_start: Optional[int] = None
    source_char_end: Optional[int] = None
    position_int: list[list[int]]
    positions: list[list[int]]


class WorkspaceDocumentChunkListResponse(BaseModel):
    file: WorkspaceFileSummary
    items: list[WorkspaceDocumentChunkItem]
    total: int
    skip: int
    limit: int


class WorkspaceFilePreviewResponse(BaseModel):
    format: str
    content: str


def get_readable_workspace_file_or_404(
    db: Session, workspace_id: int, file_id: int, current_user: User
) -> FileModel:
    file = (
        db.query(FileModel)
        .filter(FileModel.id == file_id, FileModel.deleted_at.is_(None))
        .first()
    )
    if not file or file.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="File not found"
        )

    ws_service = WorkspaceService(db)
    if not ws_service.check_user_permission(workspace_id, current_user.id, "read"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to access this workspace",
        )
    return file


def _simple_status(file: FileModel) -> Optional[str]:
    processing_status = getattr(file, "processing_status", None)
    if processing_status is None:
        return None
    raw = processing_status.value if hasattr(processing_status, "value") else str(processing_status)
    return _SIMPLE_STATUS_MAP.get(raw, raw)


def _content_disposition_inline(filename: str) -> str:
    raw = (filename or "file").replace("\r", "").replace("\n", "").replace('"', "'")
    if not raw.strip():
        raw = "file"
    try:
        raw.encode("latin-1")
        return f'inline; filename="{raw}"'
    except UnicodeEncodeError:
        fallback = (
            raw.encode("ascii", "replace")
            .decode("ascii")
            .replace("?", "_")
            .strip()
            or "download"
        )
        encoded = quote(raw, safe="")
        return f"inline; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"


def _raise_if_directory(file: FileModel, detail: str) -> None:
    if file.is_directory:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _get_workspace_or_404(db: Session, workspace_id: int) -> Workspace:
    workspace = WorkspaceService(db).get_workspace(workspace_id)
    if not workspace:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workspace {workspace_id} not found",
        )
    return workspace


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _sanitize_position_int(value) -> Optional[list[int]]:
    if not isinstance(value, (list, tuple)) or len(value) != 5:
        return None

    sanitized: list[int] = []
    for item in value:
        if isinstance(item, bool):
            return None
        if isinstance(item, int):
            sanitized.append(item)
            continue
        if isinstance(item, float):
            if not item.is_integer():
                return None
            sanitized.append(int(item))
            continue
        if isinstance(item, str):
            raw = item.strip()
            if not raw:
                return None
            try:
                parsed = float(raw)
            except ValueError:
                return None
            if not parsed.is_integer():
                return None
            sanitized.append(int(parsed))
            continue
        return None

    return sanitized


def _fallback_position_from_bbox(row: DocumentChunk) -> list[list[int]]:
    values = [row.page, row.bbox_x0, row.bbox_x1, row.bbox_y0, row.bbox_y1]
    if any(value is None or isinstance(value, bool) for value in values):
        return []
    return [[int(value) for value in values]]


def _positions_for_chunk(row: DocumentChunk) -> list[list[int]]:
    positions: list[list[int]] = []
    if isinstance(row.position_int, list):
        for item in row.position_int:
            sanitized = _sanitize_position_int(item)
            if sanitized is not None:
                positions.append(sanitized)

    if not positions:
        positions = _fallback_position_from_bbox(row)

    return positions


def _chunk_to_response(
    row: DocumentChunk, file: FileModel
) -> WorkspaceDocumentChunkItem:
    positions = _positions_for_chunk(row)
    return WorkspaceDocumentChunkItem(
        file_id=file.id,
        workspace_id=file.workspace_id,
        filename=file.name,
        chunk_id=row.chunk_id,
        chunk_index=row.chunk_index,
        text=row.text_preview or "",
        is_truncated=False,
        page=row.page,
        bbox_x0=row.bbox_x0,
        bbox_y0=row.bbox_y0,
        bbox_x1=row.bbox_x1,
        bbox_y1=row.bbox_y1,
        source_char_start=row.source_char_start,
        source_char_end=row.source_char_end,
        position_int=positions,
        positions=positions,
    )


def file_to_summary(file: FileModel) -> WorkspaceFileSummary:
    processing_status = getattr(file, "processing_status", None)
    processing_status_value = (
        processing_status.value
        if hasattr(processing_status, "value")
        else str(processing_status)
        if processing_status is not None
        else None
    )
    return WorkspaceFileSummary(
        id=file.id,
        workspace_id=file.workspace_id,
        name=file.name,
        uri=file.uri,
        mime_type=file.mime_type,
        processing_status=processing_status_value,
        simple_status=_simple_status(file),
        total_chunks=file.total_chunks,
    )


def document_chunk_to_response(
    row: DocumentChunk, file: FileModel
) -> WorkspaceDocumentChunkItem:
    return _chunk_to_response(row, file)


def _latest_completed_parse_artifact(
    db: Session, workspace_id: int, file_id: int
) -> Optional[DocumentParseArtifact]:
    return (
        db.query(DocumentParseArtifact)
        .filter(
            DocumentParseArtifact.workspace_id == workspace_id,
            DocumentParseArtifact.file_id == file_id,
            DocumentParseArtifact.status == "completed",
        )
        .order_by(
            DocumentParseArtifact.updated_at.desc(),
            DocumentParseArtifact.created_at.desc(),
            DocumentParseArtifact.id.desc(),
        )
        .first()
    )


def latest_completed_parse_artifact(
    db: Session, workspace_id: int, file_id: int
) -> Optional[DocumentParseArtifact]:
    return _latest_completed_parse_artifact(db, workspace_id, file_id)


@router.get("/{file_id}/chunks", response_model=WorkspaceDocumentChunkListResponse)
def list_workspace_file_chunks(
    workspace_id: int,
    file_id: int,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    q: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> WorkspaceDocumentChunkListResponse:
    file = get_readable_workspace_file_or_404(db, workspace_id, file_id, current_user)
    _raise_if_directory(file, "Cannot list chunks for a directory")

    query = db.query(DocumentChunk).filter(
        DocumentChunk.file_id == file.id,
        DocumentChunk.workspace_id == workspace_id,
    )
    if q:
        query = query.filter(
            DocumentChunk.text_preview.like(f"%{_escape_like(q)}%", escape="\\")
        )

    total = query.count()
    rows = (
        query.order_by(DocumentChunk.chunk_index.asc())
        .offset(skip)
        .limit(limit)
        .all()
    )

    return WorkspaceDocumentChunkListResponse(
        file=file_to_summary(file),
        items=[_chunk_to_response(row, file) for row in rows],
        total=total,
        skip=skip,
        limit=limit,
    )


@router.get("/{file_id}/chunk-source", response_model=WorkspaceFilePreviewResponse)
def get_workspace_file_chunk_source(
    workspace_id: int,
    file_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> WorkspaceFilePreviewResponse:
    file = get_readable_workspace_file_or_404(db, workspace_id, file_id, current_user)
    _raise_if_directory(file, "Cannot get chunk source for a directory")

    return build_workspace_file_chunk_source_response(db, workspace_id, file)


@router.get("/{file_id}/content")
def get_workspace_file_content(
    workspace_id: int,
    file_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> StreamingResponse:
    file = get_readable_workspace_file_or_404(db, workspace_id, file_id, current_user)
    _raise_if_directory(file, "Cannot stream directory content")
    workspace = _get_workspace_or_404(db, workspace_id)
    return build_workspace_file_content_response(file, workspace)


@router.get("/{file_id}/preview", response_model=WorkspaceFilePreviewResponse)
def get_workspace_file_preview(
    workspace_id: int,
    file_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> WorkspaceFilePreviewResponse:
    file = get_readable_workspace_file_or_404(db, workspace_id, file_id, current_user)
    _raise_if_directory(file, "Cannot preview a directory")
    workspace = _get_workspace_or_404(db, workspace_id)
    return build_workspace_file_preview_response(file, workspace)


def _read_file_bytes_from_workspace(file: FileModel, workspace: Workspace) -> bytes:
    object_key = file.uri.lstrip("/")
    try:
        return MinioStorage().read_object_bytes(workspace.slug, object_key)
    except Exception as exc:
        logger.warning(
            "MinIO read_object_bytes failed bucket=%s object_key=%s: %s",
            workspace.slug,
            object_key,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File object not found in storage",
        ) from exc


def _open_file_stream_from_workspace(file: FileModel, workspace: Workspace):
    object_key = file.uri.lstrip("/")
    try:
        return MinioStorage().open_object_stream(workspace.slug, object_key)
    except Exception as exc:
        logger.warning(
            "MinIO open_object_stream failed bucket=%s object_key=%s: %s",
            workspace.slug,
            object_key,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File object not found in storage",
        ) from exc


def get_workspace_or_404(db: Session, workspace_id: int) -> Workspace:
    return _get_workspace_or_404(db, workspace_id)


def content_disposition_inline(filename: str) -> str:
    return _content_disposition_inline(filename)


def read_file_bytes_from_workspace(file: FileModel, workspace: Workspace) -> bytes:
    return _read_file_bytes_from_workspace(file, workspace)


def open_file_stream_from_workspace(file: FileModel, workspace: Workspace):
    return _open_file_stream_from_workspace(file, workspace)


def build_workspace_file_content_response(
    file: FileModel, workspace: Workspace
) -> StreamingResponse:
    obj = open_file_stream_from_workspace(file, workspace)

    def iterfile():
        try:
            for chunk in obj.stream(64 * 1024):
                yield chunk
        finally:
            try:
                obj.close()
            finally:
                obj.release_conn()

    return StreamingResponse(
        iterfile(),
        media_type=file.mime_type or "application/octet-stream",
        headers={
            "Content-Disposition": content_disposition_inline(file.name or "file"),
        },
    )


def build_workspace_file_preview_response(
    file: FileModel, workspace: Workspace
) -> WorkspaceFilePreviewResponse:
    data = read_file_bytes_from_workspace(file, workspace)

    try:
        fmt, body = build_file_preview(data, file.mime_type, file.name or "")
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.exception("Preview generation failed for file_id=%s", file.id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate preview",
        ) from exc

    return WorkspaceFilePreviewResponse(format=fmt, content=body)


def build_workspace_file_chunk_source_response(
    db: Session, workspace_id: int, file: FileModel
) -> WorkspaceFilePreviewResponse:
    artifact = latest_completed_parse_artifact(db, workspace_id, file.id)
    if artifact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Canonical chunk source not found",
        )

    try:
        data = MinioStorage().read_object_bytes(
            artifact.canonical_md_bucket,
            artifact.canonical_md_object_key,
        )
    except Exception as exc:
        logger.warning(
            "MinIO canonical chunk source read failed bucket=%s object_key=%s: %s",
            artifact.canonical_md_bucket,
            artifact.canonical_md_object_key,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Canonical chunk source not found",
        ) from exc

    return WorkspaceFilePreviewResponse(
        format="text",
        content=data.decode("utf-8", errors="replace"),
    )
