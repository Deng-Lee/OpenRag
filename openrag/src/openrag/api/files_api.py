"""File Management API endpoints"""

import logging
import os
import shutil
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import or_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from openrag.api.deps import get_current_user, get_db
from openrag.chunking.document_type import DEFAULT_DOCUMENT_TYPE, normalize_document_type
from openrag.config import get_config
from openrag.models.document_chunk import DocumentChunk
from openrag.models.file import File as FileModel, ProcessingStatus
from openrag.models.user import User
from openrag.models.task import Task, TaskStatus, TaskType
from openrag.services.file_deletion import (
    delete_file_with_storage,
    delete_files_under_uri_prefix,
    delete_milvus_vectors_for_file,
)
from openrag.services.file_preview import build_file_preview
from openrag.services.file_ingest import (
    MAX_FILE_SIZE,
    SUPPORTED_PARSER_TYPES,
    build_file_uri,
    ingest_new_file,
    is_processing_supported,
    resolve_effective_mime_type,
    validate_path,
)
from openrag.services.task_service import TaskService
from openrag.storage.minio_storage import MinioStorage, chunk_object_key
from openrag.hierarchy.hierarchy_storage import HierarchyStorage
from openrag.tracing.context import set_trace_context


router = APIRouter(prefix="/files", tags=["files"])

logger = logging.getLogger(__name__)


def _normalize_uri_path(path: Optional[str]) -> str:
    """逻辑路径：必有前导 /；根为 /；其余去掉尾部 /。"""
    if path is None:
        return "/"
    p = str(path).strip().replace("\\", "/")
    if not p:
        return "/"
    if not p.startswith("/"):
        p = "/" + p
    if len(p) > 1:
        p = p.rstrip("/")
    return p or "/"


def _is_direct_child_uri(uri: str, parent_path: str) -> bool:
    """判断 uri 是否为 parent_path 下的直接子项（一层）。"""
    p = _normalize_uri_path(parent_path)
    u = (uri or "").strip().replace("\\", "/")
    if not u:
        return False
    if not u.startswith("/"):
        u = "/" + u
    if len(u) > 1:
        u = u.rstrip("/")
    if u == "/":
        return False
    if p == "/":
        inner = u[1:]
        return bool(inner) and "/" not in inner
    pref = p + "/"
    if not u.startswith(pref):
        return False
    suffix = u[len(pref) :]
    return bool(suffix) and "/" not in suffix


def _is_under_path_uri(uri: str, prefix_path: str) -> bool:
    """uri 在 prefix 之下（含 prefix 自身）。"""
    p = _normalize_uri_path(prefix_path)
    u = (uri or "").strip().replace("\\", "/")
    if not u.startswith("/"):
        u = "/" + u
    if p == "/":
        return len(u) > 1
    if u == p:
        return True
    return u.startswith(p + "/")


def _sqlalchemy_error_detail(exc: SQLAlchemyError) -> str:
    """便于排障：写入 HTTP detail 与日志；底层异常多为数据库驱动 OperationalError。"""
    orig = getattr(exc, "orig", None)
    if orig is not None:
        return f"{type(orig).__name__}: {orig}"
    return str(exc) or type(exc).__name__


def _content_disposition_inline(filename: str) -> str:
    """RFC 5987 filename*，避免中文文件名在 HTTP 头里触发 latin-1 编码错误。"""
    raw = (filename or "file").replace("\r", "").replace("\n", "").replace('"', "'")
    if not raw.strip():
        raw = "file"
    try:
        raw.encode("latin-1")
        return f'inline; filename="{raw}"'
    except UnicodeEncodeError:
        fb = (
            raw.encode("ascii", "replace")
            .decode("ascii")
            .replace("?", "_")
            .strip()
            or "download"
        )
        enc = quote(raw, safe="")
        return f'inline; filename="{fb}"; filename*=UTF-8\'\'{enc}'


# Pydantic schemas
class FileResponse(BaseModel):
    """File response schema"""

    id: int
    uri: str
    name: str
    owner_id: int
    owner_name: Optional[str] = None
    parent_id: Optional[int]
    is_directory: bool
    size: int
    mime_type: Optional[str]
    document_type: str
    tag: Optional[str] = None
    created_at: str
    updated_at: str
    processing_status: Optional[str] = None
    simple_status: Optional[str] = None
    error_message: Optional[str] = None

    model_config = {"from_attributes": True}


class FileUploadResponse(FileResponse):
    """File upload response with task ID"""

    task_id: Optional[int] = None


class FileListResponse(BaseModel):
    """File list response with pagination"""

    items: list[FileResponse]
    total: int
    skip: int
    limit: int


class FileMoveRequest(BaseModel):
    """File move/rename request"""

    new_path: str = Field(..., description="New file URI in format: /path/to/file")


class MessageResponse(BaseModel):
    """Generic message response"""

    message: str


class PathPrefixDeleteRequest(BaseModel):
    """按路径前缀级联删除（虚拟目录 + 其下所有文件与目录记录）。"""

    workspace_id: int = Field(..., description="工作区 ID")
    path: str = Field(..., description="目录 URI 前缀，例如 /resources")


class FilePreviewResponse(BaseModel):
    """服务端生成的文档预览（HTML 或纯文本）。"""

    format: Literal["html", "text"]
    content: str


class ReprocessRequest(BaseModel):
    """File reprocess request"""

    parser_type: Optional[str] = Field(
        default=None,
        description=f"Parser type: {', '.join(SUPPORTED_PARSER_TYPES)}. If not provided, uses the original parser_type."
    )
    document_type: Optional[str] = Field(
        default=None,
        description="Document type: general, manual, laws. If not provided, keeps the original document_type.",
    )

    @field_validator('parser_type')
    @classmethod
    def validate_parser_type(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in SUPPORTED_PARSER_TYPES:
            raise ValueError(f"Invalid parser_type. Supported types: {', '.join(SUPPORTED_PARSER_TYPES)}")
        return v


_SIMPLE_STATUS_MAP: dict[str, str] = {
    "pending": "unprocessed",
    "parsing": "processing",
    "building_hierarchy": "processing",
    "embedding": "processing",
    "completed": "done",
    "failed": "failed",
}

_SIMPLE_STATUS_MAP_REVERSE: dict[str, list[ProcessingStatus]] = {
    "unprocessed": [ProcessingStatus.pending],
    "processing": [ProcessingStatus.parsing, ProcessingStatus.building_hierarchy, ProcessingStatus.embedding],
    "done": [ProcessingStatus.completed],
    "failed": [ProcessingStatus.failed],
}


def _escape_ilike(value: str) -> str:
    """Escape % and _ wildcards for PostgreSQL ilike patterns."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _build_type_filter(file_type: str) -> list:
    """Map shorthand file_type to SQLAlchemy MIME/extension conditions."""
    ft = file_type.lower()
    conditions = []
    if ft == "pdf":
        conditions.append(FileModel.mime_type.ilike("%pdf%"))
        conditions.append(FileModel.name.ilike("%.pdf"))
    elif ft == "docx":
        conditions.append(FileModel.mime_type.ilike("%word%"))
        conditions.append(FileModel.mime_type.ilike("%document%"))
        conditions.append(FileModel.name.ilike("%.docx"))
        conditions.append(FileModel.name.ilike("%.doc"))
    elif ft == "txt":
        conditions.append(FileModel.mime_type.ilike("%text/plain%"))
        conditions.append(FileModel.name.ilike("%.txt"))
    return conditions


def _processing_status_str(file: FileModel) -> Optional[str]:
    if file.is_directory:
        return None
    ps = file.processing_status
    if ps is None:
        return None
    return ps.value if hasattr(ps, "value") else str(ps)


def _file_to_response(file: FileModel, owner_name_map: Optional[dict[int, str]] = None) -> FileResponse:
    ps = _processing_status_str(file)
    ss = _SIMPLE_STATUS_MAP.get(ps) if ps else None
    err: Optional[str] = None
    if ss == "failed":
        err = file.processing_error
    owner_name: Optional[str] = None
    if owner_name_map is not None:
        owner_name = owner_name_map.get(file.owner_id)
    elif hasattr(file, "owner") and file.owner is not None:
        owner_name = file.owner.username
    return FileResponse(
        id=file.id,
        uri=file.uri,
        name=file.name,
        owner_id=file.owner_id,
        owner_name=owner_name,
        parent_id=file.parent_id,
        is_directory=file.is_directory,
        size=file.size,
        mime_type=file.mime_type,
        document_type=getattr(file, "document_type", None) or DEFAULT_DOCUMENT_TYPE,
        tag=file.tag,
        created_at=file.created_at.isoformat(),
        updated_at=file.updated_at.isoformat(),
        processing_status=ps,
        simple_status=ss,
        error_message=err,
    )


def _file_to_upload_response(
    file: FileModel, task_id: Optional[int]
) -> FileUploadResponse:
    base = _file_to_response(file)
    return FileUploadResponse(**base.model_dump(), task_id=task_id)


def cleanup_file_processing_data(
    file: FileModel,
    workspace_slug: str,
    db: Session
) -> None:
    """Cleanup existing processing data for a file"""
    try:
        minio_storage = MinioStorage()
        minio_storage.remove_document_hierarchy(workspace_slug, file.uri)
        logger.info(f"Cleaned up MinIO hierarchy for file {file.id}")
    except Exception as e:
        logger.warning(f"Failed to cleanup MinIO hierarchy: {e}")

    try:
        storage = HierarchyStorage()
        file_dir = storage._get_uri_dir(file.uri, create=False)
        if file_dir.exists():
            shutil.rmtree(file_dir, ignore_errors=True)
            logger.info(f"Cleaned up local hierarchy files for file {file.id}")
    except Exception as e:
        logger.warning(f"Failed to cleanup local hierarchy files: {e}")

    try:
        db.query(DocumentChunk).filter(DocumentChunk.file_id == file.id).delete(
            synchronize_session=False
        )
        logger.info(f"Cleaned up document_chunks for file {file.id}")
    except Exception as e:
        logger.warning(f"Failed to cleanup document_chunks: {e}")

    delete_milvus_vectors_for_file(file.id)

    # Cleanup vector data if exists
    if file.l0_vector_id:
        # TODO: Delete from vector database when integrated
        file.l0_vector_id = None

    # Reset file processing status
    file.processing_status = ProcessingStatus.pending
    file.processing_error = None
    file.l0_path = None
    file.l1_path = None
    file.l2_path = None
    file.total_chunks = 0
    file.total_tokens = 0
    # Note: Caller is responsible for db.commit()


@router.post(
    "/upload", response_model=FileUploadResponse, status_code=status.HTTP_201_CREATED
)
async def upload_file(
    workspace_id: int = Form(..., description="Workspace ID"),
    file: UploadFile = File(...),
    path: str = Form(default="/"),
    parser_type: str = Form(
        default="auto", description=f"Parser type: {', '.join(SUPPORTED_PARSER_TYPES)}"
    ),
    document_type: str = Form(
        default=DEFAULT_DOCUMENT_TYPE,
        description="Document type: general, manual, laws",
    ),
    tag: Optional[str] = Form(default=None, description="Unique tag within the workspace"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Upload a file and trigger async processing

    Args:
        workspace_id: Workspace ID (required)
        file: Uploaded file
        path: Target directory path
        parser_type: Document parser type (default: auto)
        current_user: Current authenticated user
        db: Database session

    Returns:
        File metadata and processing task ID
    """
    from openrag.services.workspace_service import WorkspaceService

    ws_service = WorkspaceService(db)
    if not ws_service.check_user_permission(workspace_id, current_user.id, "write"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Write permission required for this workspace",
        )

    file_content = await file.read()
    workspace = ws_service.get_workspace(workspace_id)
    if not workspace:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workspace {workspace_id} not found",
        )
    set_trace_context(
        trace_type="upload",
        workspace_id=workspace_id,
        user_id=current_user.id,
        sampling_reason="file_upload",
    )

    file_record, task_record = ingest_new_file(
        db,
        workspace,
        current_user.id,
        parent_logical_path=path,
        upload_filename=file.filename or "unnamed",
        file_content=file_content,
        content_type=file.content_type,
        parser_type=parser_type,
        document_type=document_type,
        require_parent_dir=False,
        duplicate_status_code=status.HTTP_400_BAD_REQUEST,
        tag=tag,
    )

    return _file_to_upload_response(
        file_record, task_record.id if task_record else None
    )


@router.get("/", response_model=FileListResponse)
async def list_files(
    skip: int = 0,
    limit: int = 100,
    workspace_id: Optional[int] = None,
    parent_path: Optional[str] = Query(
        None,
        description="仅返回该逻辑路径下的直接子项（一层），用于目录树懒加载",
    ),
    under_path: Optional[str] = Query(
        None,
        description="返回 uri 在该路径之下（含自身）的所有行，用于列表区子树展示",
    ),
    filename: Optional[str] = Query(None, description="文件名 substring 匹配（大小写不敏感）"),
    file_type: Optional[str] = Query(None, description="MIME 类型组筛选：pdf / docx / txt"),
    owner_username: Optional[str] = Query(None, description="上传人 username substring 匹配（大小写不敏感）"),
    simple_status: Optional[str] = Query(None, description="处理状态筛选：unprocessed / processing / done / failed"),
    created_after: Optional[str] = Query(None, description="创建时间晚于此 ISO datetime"),
    created_before: Optional[str] = Query(None, description="创建时间早于此 ISO datetime"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    List user's accessible files with pagination and filtering

    Args:
        skip: Number of records to skip
        limit: Maximum number of records to return
        workspace_id: Optional workspace ID to filter files
        parent_path: 与 under_path 二选一；同时传则 400。
        under_path: 子树过滤，用于右侧列表与「当前目录」范围。
        filename: 文件名 substring 匹配
        file_type: MIME 类型组筛选
        owner_username: 上传人 username substring 匹配
        simple_status: 处理状态筛选
        created_after: 创建时间晚于此
        created_before: 创建时间早于此
        current_user: Current authenticated user
        db: Database session

    Returns:
        Paginated list of files
    """
    if parent_path is not None and under_path is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Specify only one of parent_path and under_path",
        )
    if simple_status is not None and simple_status not in _SIMPLE_STATUS_MAP_REVERSE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid simple_status: {simple_status}. Valid values: {', '.join(_SIMPLE_STATUS_MAP_REVERSE.keys())}",
        )

    # Get workspaces user has read access to
    from openrag.services.workspace_service import WorkspaceService
    from datetime import datetime

    ws_service = WorkspaceService(db)
    accessible_workspaces = ws_service.get_user_workspaces_with_permission(
        current_user.id, permission="read"
    )
    accessible_workspace_ids = [ws.id for ws in accessible_workspaces]

    if not accessible_workspace_ids:
        return FileListResponse(items=[], total=0, skip=skip, limit=limit)

    # If workspace_id is specified, verify user has access to it
    if workspace_id is not None:
        if workspace_id not in accessible_workspace_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to access this workspace",
            )
        workspace_ids = [workspace_id]
    else:
        workspace_ids = accessible_workspace_ids

    # Determine if any search filter is active
    has_filters = any([filename, file_type, owner_username, simple_status, created_after, created_before])

    # Build SQLAlchemy query with DB-level filters
    query = db.query(FileModel).filter(
        FileModel.workspace_id.in_(workspace_ids),
        FileModel.deleted_at.is_(None),
    )

    if has_filters:
        # Exclude directories when search filters are active
        query = query.filter(FileModel.is_directory == False)

    # Filename filter — case-insensitive substring
    if filename:
        query = query.filter(FileModel.name.ilike(f"%{_escape_ilike(filename)}%"))

    # File type filter — MIME type group mapping
    if file_type:
        type_conditions = _build_type_filter(file_type)
        if type_conditions:
            query = query.filter(or_(*type_conditions))
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid file_type: {file_type}. Valid values: pdf, docx, txt",
            )

    # Owner username filter — join User table
    if owner_username:
        query = query.join(User, FileModel.owner_id == User.id).filter(
            User.username.ilike(f"%{_escape_ilike(owner_username)}%")
        )

    # Processing status filter
    if simple_status:
        valid_statuses = _SIMPLE_STATUS_MAP_REVERSE[simple_status]
        query = query.filter(FileModel.processing_status.in_(valid_statuses))

    # Date range filters
    if created_after:
        try:
            after_dt = datetime.fromisoformat(created_after)
            query = query.filter(FileModel.created_at >= after_dt)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid created_after datetime format. Use ISO format.",
            )
    if created_before:
        try:
            before_dt = datetime.fromisoformat(created_before)
            query = query.filter(FileModel.created_at <= before_dt)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid created_before datetime format. Use ISO format.",
            )

    # Execute query to get filtered rows
    files = query.all()

    # Apply path-based filtering (still in-memory for complex normalization logic)
    if parent_path is not None:
        pp = _normalize_uri_path(parent_path)
        files = [f for f in files if _is_direct_child_uri(f.uri, pp)]
    elif under_path is not None:
        up = _normalize_uri_path(under_path)
        files = [f for f in files if _is_under_path_uri(f.uri, up)]

    # Apply pagination
    total = len(files)
    paginated_files = files[skip : skip + limit]

    # Batch-resolve owner names
    owner_ids = {f.owner_id for f in paginated_files}
    owner_name_map: dict[int, str] = {}
    if owner_ids:
        users = db.query(User.id, User.username).filter(User.id.in_(owner_ids)).all()
        owner_name_map = {u.id: u.username for u in users}

    # Convert to response format
    items = [_file_to_response(f, owner_name_map) for f in paginated_files]

    return FileListResponse(items=items, total=total, skip=skip, limit=limit)


def _get_readable_file_or_404(
    file_id: int, current_user: User, db: Session
) -> FileModel:
    file = db.query(FileModel).filter(FileModel.id == file_id, FileModel.deleted_at.is_(None)).first()
    if not file:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="File not found"
        )
    from openrag.services.workspace_service import WorkspaceService

    ws_service = WorkspaceService(db)
    if not ws_service.check_user_permission(file.workspace_id, current_user.id, "read"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to access this file",
        )
    return file


@router.get("/{file_id}/content")
async def get_file_content(
    file_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    流式返回 MinIO 中的原始文件内容（用于浏览器内 PDF/图片等预览）。
    """
    file = _get_readable_file_or_404(file_id, current_user, db)
    if file.is_directory:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot stream directory content",
        )

    from openrag.services.workspace_service import WorkspaceService

    ws_service = WorkspaceService(db)
    workspace = ws_service.get_workspace(file.workspace_id)
    if not workspace:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workspace {file.workspace_id} not found",
        )

    minio_storage = MinioStorage()
    # bucket = workspace.slug；object key = DB 中 uri 去掉前导 /（与 put_file 一致）
    object_key = file.uri.lstrip("/")
    try:
        obj = minio_storage.open_object_stream(workspace.slug, object_key)
    except Exception as e:
        logger.warning(
            "MinIO open_object_stream failed bucket=%s object_key=%s: %s",
            workspace.slug,
            object_key,
            e,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="File object not found in storage"
        ) from e

    media_type = file.mime_type or "application/octet-stream"

    def iterfile():
        try:
            for chunk in obj.stream(64 * 1024):
                yield chunk
        finally:
            obj.close()
            obj.release_conn()

    return StreamingResponse(
        iterfile(),
        media_type=media_type,
        headers={
            "Content-Disposition": _content_disposition_inline(file.name or "file"),
            "Cache-Control": "private, max-age=300",
        },
    )


@router.get("/{file_id}/preview", response_model=FilePreviewResponse)
async def get_file_preview(
    file_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    将 Office / 部分文本类文件转为 HTML 或纯文本，供前端模态窗展示（体积受上传上限约束）。
    """
    file = _get_readable_file_or_404(file_id, current_user, db)
    if file.is_directory:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot preview a directory",
        )

    from openrag.services.workspace_service import WorkspaceService

    ws_service = WorkspaceService(db)
    workspace = ws_service.get_workspace(file.workspace_id)
    if not workspace:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workspace {file.workspace_id} not found",
        )

    minio_storage = MinioStorage()
    object_key = file.uri.lstrip("/")
    try:
        data = minio_storage.read_object_bytes(workspace.slug, object_key)
    except Exception as e:
        logger.warning(
            "MinIO read failed for preview bucket=%s object_key=%s: %s",
            workspace.slug,
            object_key,
            e,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="File object not found in storage"
        ) from e

    try:
        fmt, body = build_file_preview(data, file.mime_type, file.name or "")
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=str(e),
        ) from e
    except Exception as e:
        logger.exception("Preview generation failed for file_id=%s", file_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate preview",
        ) from e

    return FilePreviewResponse(format=fmt, content=body)


@router.get("/{file_id}", response_model=FileResponse)
async def get_file(
    file_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Get file details

    Args:
        file_id: File ID
        current_user: Current authenticated user
        db: Database session

    Returns:
        File metadata
    """
    file = _get_readable_file_or_404(file_id, current_user, db)

    return _file_to_response(file)


@router.delete("/{file_id}")
async def delete_file(
    file_id: int,
    background: bool = Query(
        True,
        description="若 true：排队由 Worker 异步删除（HTTP 立即返回 202）；若 false：同步删除（可能较慢）",
    ),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Delete a file (requires workspace write permission)

    Args:
        file_id: File ID
        background: 默认异步删除（与上传一致），避免 Milvus/MinIO 阻塞 HTTP。
        current_user: Current authenticated user
        db: Database session

    Returns:
        202 + task_id（异步）或 200 + message（同步）
    """
    try:
        file = db.query(FileModel).filter(FileModel.id == file_id, FileModel.deleted_at.is_(None)).first()
        if not file:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="File not found"
            )

        from openrag.services.workspace_service import WorkspaceService

        ws_service = WorkspaceService(db)
        if not ws_service.check_user_permission(
            file.workspace_id, current_user.id, "write"
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Write permission required for this workspace",
            )

        workspace = ws_service.get_workspace(file.workspace_id)
        if not workspace:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Workspace {file.workspace_id} not found",
            )

        if file.is_directory and file.uri.rstrip("/") == "":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot delete workspace root",
            )

        if background:
            from openrag.services.file_deletion import (
                _release_tag_and_soft_delete,
                soft_delete_subtree_and_enqueue,
            )

            if file.is_directory:
                # subtree soft-delete + DELETE_PATH_PREFIX in one commit (rollback on failure)
                task_record = soft_delete_subtree_and_enqueue(
                    db, file.workspace_id, file.uri.rstrip("/"), user_id=current_user.id
                )
            else:
                task_record = _release_tag_and_soft_delete(
                    db, file, user_id=current_user.id
                )
            return JSONResponse(
                status_code=status.HTTP_202_ACCEPTED,
                content={
                    "message": "File deletion queued",
                    "task_id": task_record.id,
                    "async": True,
                },
            )

        delete_file_with_storage(db, file, workspace)
        return MessageResponse(message="File deleted successfully")
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        logger.exception(
            "delete_file database error file_id=%s background=%s",
            file_id,
            background,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_sqlalchemy_error_detail(exc),
        ) from exc


@router.post("/delete-path-prefix")
async def delete_path_prefix(
    body: PathPrefixDeleteRequest,
    background: bool = Query(
        True,
        description="若 true：由 Worker 异步级联删除（推荐）；若 false：同步删除",
    ),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """删除给定 URI 前缀下的全部文件与目录（含仅因路径推断出的「虚拟」层级）。

    每条记录仍走与单文件删除相同的 MinIO / 层级存储 / Milvus / 数据库清理逻辑。
    """
    from openrag.services.workspace_service import WorkspaceService

    ws_service = WorkspaceService(db)
    if not ws_service.check_user_permission(
        body.workspace_id, current_user.id, "write"
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Write permission required for this workspace",
        )

    workspace = ws_service.get_workspace(body.workspace_id)
    if not workspace:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workspace {body.workspace_id} not found",
        )

    validated = validate_path(body.path)
    prefix = validated.rstrip("/")
    if not prefix:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete workspace root path",
        )

    from openrag.services.file_deletion import (
        _escape_like,
        soft_delete_subtree_and_enqueue,
    )

    n = (
        db.query(FileModel)
        .filter(
            FileModel.workspace_id == body.workspace_id,
            or_(
                FileModel.uri == prefix,
                FileModel.uri.like(f"{_escape_like(prefix)}/%", escape="\\"),
            ),
            FileModel.deleted_at.is_(None),
        )
        .count()
    )
    if n == 0:
        return MessageResponse(message="No files or directories under this path")

    if background:
        # subtree soft-delete + DELETE_PATH_PREFIX(watermark) in one commit (rollback on failure)
        task_record = soft_delete_subtree_and_enqueue(
            db, body.workspace_id, prefix, user_id=current_user.id
        )
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "message": "Path prefix deletion queued",
                "task_id": task_record.id,
                "async": True,
                "path": prefix,
            },
        )

    deleted_ids = delete_files_under_uri_prefix(
        db, body.workspace_id, prefix, workspace
    )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "message": "Path prefix deleted",
            "async": False,
            "deleted_count": len(deleted_ids),
            "deleted_ids": deleted_ids,
        },
    )


@router.put("/{file_id}/move", response_model=FileResponse)
async def move_file(
    file_id: int,
    request: FileMoveRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Move or rename a file

    Args:
        file_id: File ID
        request: Move request with new path
        current_user: Current authenticated user
        db: Database session

    Returns:
        Updated file metadata
    """
    # Get file
    file = db.query(FileModel).filter(FileModel.id == file_id, FileModel.deleted_at.is_(None)).first()
    if not file:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="File not found"
        )

    # Check workspace write permission
    from openrag.services.workspace_service import WorkspaceService

    ws_service = WorkspaceService(db)
    if not ws_service.check_user_permission(
        file.workspace_id, current_user.id, "write"
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Write permission required for this workspace",
        )

    new_path = validate_path(request.new_path)

    from openrag.services.file_ingest import assert_no_pending_deleted_ancestor
    assert_no_pending_deleted_ancestor(db, file.workspace_id, new_path)

    # Check if target already exists
    existing_file = (
        db.query(FileModel)
        .filter(FileModel.uri == new_path, FileModel.workspace_id == file.workspace_id)
        .first()
    )
    if existing_file:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File already exists at {new_path}",
        )

    # Move file in storage
    minio_storage = MinioStorage()
    workspace = ws_service.get_workspace(file.workspace_id)

    if not workspace:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workspace {file.workspace_id} not found",
        )

    if file.is_directory:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Moving directories in minio is not currently supported via API",
        )
    else:
        old_uri = file.uri
        minio_storage.move_file(workspace.slug, old_uri, new_path)
        try:
            minio_storage.move_document_hierarchy(
                workspace.slug, old_uri, new_path
            )
        except Exception as exc:
            logger.warning(
                "Failed to move MinIO hierarchy for %s -> %s: %s",
                old_uri,
                new_path,
                exc,
            )

    # Update file record
    file.uri = new_path
    file.name = os.path.basename(new_path)

    for row in db.query(DocumentChunk).filter(DocumentChunk.file_id == file.id).all():
        row.object_key = chunk_object_key(new_path, row.chunk_index)
        row.object_url = minio_storage.path_style_http_url(
            workspace.slug, row.object_key
        )
        row.local_chunk_path = None

    db.commit()
    db.refresh(file)

    return _file_to_response(file)


@router.post(
    "/directories", response_model=FileResponse, status_code=status.HTTP_201_CREATED
)
async def create_directory(
    path: str = Form(..., description="Directory path (e.g., /documents/reports)"),
    workspace_id: int = Form(..., description="Workspace ID"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Create a directory

    Args:
        path: Logical directory path under the workspace
        workspace_id: Workspace ID (required)
        current_user: Current authenticated user
        db: Database session

    Returns:
        Directory metadata
    """
    # Verify workspace write permission
    from openrag.services.workspace_service import WorkspaceService

    ws_service = WorkspaceService(db)
    if not ws_service.check_user_permission(workspace_id, current_user.id, "write"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Write permission required for this workspace",
        )

    # Get workspace slug for URI generation
    workspace = ws_service.get_workspace(workspace_id)
    if not workspace:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workspace {workspace_id} not found",
        )

    # Validate path and create workspace URI
    dir_name = os.path.basename(path.rstrip("/"))
    parent_path = os.path.dirname(path.rstrip("/"))
    if not parent_path or parent_path == ".":
        parent_path = "/"
    validated_parent_path = validate_path(parent_path)
    dir_path = build_file_uri(validated_parent_path, dir_name)

    from openrag.services.file_ingest import assert_no_pending_deleted_ancestor
    assert_no_pending_deleted_ancestor(db, workspace_id, dir_path)

    existing_dir = (
        db.query(FileModel)
        .filter(FileModel.uri == dir_path, FileModel.workspace_id == workspace_id)
        .first()
    )
    if existing_dir:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Directory already exists at {dir_path}",
        )

    minio_storage = MinioStorage()
    minio_storage.ensure_bucket(workspace.slug)

    # Create directory record in database only (virtual directory)
    # No actual object is created in MinIO, the directory exists only as a record in the database
    # Extract directory name without file extension
    dir_name = os.path.basename(dir_path)
    # Remove file extension if present (e.g., "file.doc" -> "file")
    dir_name = os.path.splitext(dir_name)[0] or dir_name

    directory = FileModel(
        uri=dir_path,
        name=dir_name,
        owner_id=current_user.id,
        workspace_id=workspace_id,
        is_directory=True,
        size=0,
        mime_type=None,
    )

    db.add(directory)
    db.commit()
    db.refresh(directory)

    return _file_to_response(directory)


@router.post("/{file_id}/reprocess", response_model=FileUploadResponse)
async def reprocess_file(
    file_id: int,
    request: ReprocessRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Reprocess a file - clears existing chunks/vectors and re-triggers processing"""
    # Get file
    file = db.query(FileModel).filter(FileModel.id == file_id, FileModel.deleted_at.is_(None)).first()
    if not file:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="File not found"
        )

    # Check workspace write permission
    from openrag.services.workspace_service import WorkspaceService

    ws_service = WorkspaceService(db)
    if not ws_service.check_user_permission(
        file.workspace_id, current_user.id, "write"
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Write permission required for this workspace",
        )

    # Cannot reprocess directories
    if file.is_directory:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot reprocess directories",
        )

    # Get workspace
    workspace = ws_service.get_workspace(file.workspace_id)
    if not workspace:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workspace {file.workspace_id} not found",
        )

    # Validate MIME type
    effective_mime = resolve_effective_mime_type(
        file.mime_type, file.name or "", file.parser_type
    )
    if not is_processing_supported(file.mime_type, file.name or "", file.parser_type):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File type {effective_mime} is not supported for processing"
        )

    # Determine parser type to use (before cleanup)
    parser_type = request.parser_type if request.parser_type else file.parser_type
    if not parser_type:
        parser_type = "auto"

    if request.document_type is not None:
        try:
            file.document_type = normalize_document_type(request.document_type)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
    else:
        file.document_type = normalize_document_type(
            getattr(file, "document_type", None)
        )

    # Update file record with new parser/document type FIRST
    file.parser_type = parser_type if parser_type != "auto" else None

    # Cleanup existing processing data (modifies file status)
    cleanup_file_processing_data(file, workspace.slug, db)

    # Commit the file status change FIRST
    db.commit()

    # Create task record for worker to pick up (instead of calling Celery directly)
    from openrag.services.task_service import TaskService

    task_service = TaskService(db)
    task_record = task_service.create_task(
        workspace_id=file.workspace_id,
        user_id=current_user.id,
        file_id=file.id,
        task_type="process_document",
        queue="normal",  # Could be dynamic based on file size
        priority=5,
        max_retries=3,
        status=TaskStatus.PENDING,
    )

    return _file_to_upload_response(file, task_record.id)
