"""Shared file upload / replace logic for JWT and service-token APIs."""

from __future__ import annotations

import mimetypes
import posixpath
import uuid
from typing import Optional, Tuple

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from openrag.chunking.document_type import DEFAULT_DOCUMENT_TYPE, normalize_document_type
from openrag.models.file import File as FileModel
from openrag.models.task import Task, TaskStatus
from openrag.models.workspace import Workspace
from openrag.services.task_service import TaskService
from openrag.services.trace_service import TraceService
from openrag.storage.minio_storage import MinioStorage
from openrag.tracing.context import get_trace_context, reset_trace_context, set_trace_context

MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB
ALLOWED_MIME_TYPES = [
    "text/plain",
    "text/markdown",
    "text/html",
    "text/csv",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.ms-powerpoint",
    "application/json",
    "application/epub+zip",
]

SUPPORTED_PARSER_TYPES = [
    "auto",
    "pdf",
    "docx",
    "xlsx",
    "pptx",
    "txt",
    "md",
    "html",
    "json",
    "csv",
    "epub",
]

_GENERIC_MIME_TYPES = {"application/octet-stream"}
_EXTENSION_MIME_OVERRIDES = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".html": "text/html",
    ".htm": "text/html",
    ".csv": "text/csv",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".ppt": "application/vnd.ms-powerpoint",
    ".json": "application/json",
    ".epub": "application/epub+zip",
}
_PARSER_TYPE_MIME_MAP = {
    "txt": "text/plain",
    "md": "text/markdown",
    "html": "text/html",
    "csv": "text/csv",
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "json": "application/json",
    "epub": "application/epub+zip",
}


def resolve_effective_mime_type(
    content_type: Optional[str], filename: str, parser_type: Optional[str] = None
) -> str:
    """Resolve an effective MIME type, falling back to extension-based detection."""
    raw = (content_type or "").strip().lower()
    if raw and raw not in _GENERIC_MIME_TYPES:
        return raw
    parser_hint = (parser_type or "").strip().lower()
    if parser_hint and parser_hint != "auto":
        mapped = _PARSER_TYPE_MIME_MAP.get(parser_hint)
        if mapped:
            return mapped
    ext = posixpath.splitext((filename or "").lower())[1]
    override = _EXTENSION_MIME_OVERRIDES.get(ext)
    if override:
        return override
    guessed, _ = mimetypes.guess_type(filename or "")
    if guessed:
        return guessed.lower()
    return "application/octet-stream"


def is_processing_supported(
    content_type: Optional[str], filename: str, parser_type: Optional[str] = None
) -> bool:
    """Check whether a file can enter processing pipeline."""
    return (
        resolve_effective_mime_type(content_type, filename, parser_type)
        in ALLOWED_MIME_TYPES
    )


def validate_path(path: str) -> str:
    """Normalize logical path; reject traversal (same rules as legacy ``files_api``)."""
    normalized = posixpath.normpath(path or "/")
    if ".." in normalized or normalized.startswith("/.."):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid path: path traversal detected",
        )
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    return normalized


def build_file_uri(path: str, filename: str) -> str:
    normalized_path = path.rstrip("/")
    if normalized_path and not normalized_path.startswith("/"):
        normalized_path = "/" + normalized_path
    if normalized_path and normalized_path != "/":
        return f"{normalized_path}/{filename}"
    return f"/{filename}"


def _safe_start_upload_run(
    db: Session,
    workspace: Workspace,
    owner_user_id: int,
) -> tuple[TraceService, Optional[str]]:
    trace_service = TraceService(db)
    ctx = get_trace_context()
    trace_id = ctx["trace_id"] or uuid.uuid4().hex
    set_trace_context(
        trace_id=trace_id,
        trace_type="upload",
        workspace_id=workspace.id,
        user_id=owner_user_id,
        sampling_reason=ctx["sampling_reason"] or "file_upload",
    )
    try:
        trace_service.start_run(
            trace_id=trace_id,
            trace_type="upload",
            workspace_id=workspace.id,
            user_id=owner_user_id,
            sampling_reason=get_trace_context()["sampling_reason"],
        )
    except Exception:
        pass
    return trace_service, trace_id


def _safe_finish_upload_run(
    db: Session,
    trace_service: TraceService,
    trace_id: Optional[str],
    *,
    file_id: Optional[int] = None,
    failed_stage: Optional[str] = None,
    error_message: Optional[str] = None,
) -> None:
    if not trace_id:
        return
    try:
        from openrag.models import TraceRun

        run = db.query(TraceRun).filter(TraceRun.trace_id == trace_id).first()
        if run is not None and file_id is not None:
            run.file_id = file_id
            db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    try:
        if error_message:
            trace_service.fail_run(
                trace_id=trace_id,
                error_stage=failed_stage,
                error_message=error_message,
            )
        else:
            trace_service.finish_run(trace_id=trace_id)
    except Exception:
        pass


def _safe_span(
    trace_service: TraceService,
    stage: str,
    *,
    input_summary: Optional[dict] = None,
    output_summary: Optional[dict] = None,
    error_message: Optional[str] = None,
) -> None:
    try:
        span = trace_service.start_span(stage, input_summary=input_summary)
    except Exception:
        return
    if span is None:
        return
    try:
        if error_message:
            trace_service.fail_span(error_message=error_message)
        else:
            trace_service.finish_span(output_summary=output_summary)
    except Exception:
        pass


def _assert_parent_directory_exists(db: Session, workspace_id: int, parent_logical_path: str) -> None:
    parent = validate_path(parent_logical_path)
    if parent == "/":
        row = (
            db.query(FileModel)
            .filter(
                FileModel.workspace_id == workspace_id,
                FileModel.uri == "/",
                FileModel.is_directory.is_(True),
            )
            .first()
        )
    else:
        row = (
            db.query(FileModel)
            .filter(
                FileModel.workspace_id == workspace_id,
                FileModel.uri == parent,
                FileModel.is_directory.is_(True),
            )
            .first()
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Parent directory does not exist",
        )


def ingest_new_file(
    db: Session,
    workspace: Workspace,
    owner_user_id: int,
    *,
    parent_logical_path: str,
    upload_filename: str,
    file_content: bytes,
    content_type: Optional[str],
    parser_type: str = "auto",
    document_type: str = DEFAULT_DOCUMENT_TYPE,
    require_parent_dir: bool = False,
    duplicate_status_code: int = status.HTTP_400_BAD_REQUEST,
) -> Tuple[FileModel, Optional[Task]]:
    """
    Create a new file object under ``parent_logical_path`` / ``upload_filename``,
    write bytes to MinIO, optionally enqueue ``process_document``.
    """
    trace_service, trace_id = _safe_start_upload_run(db, workspace, owner_user_id)
    file_record: Optional[FileModel] = None
    try:
        try:
            normalized_document_type = normalize_document_type(document_type)
        except ValueError as exc:
            _safe_span(
                trace_service,
                "upload.validate",
                input_summary={
                    "filename": upload_filename,
                    "workspace_id": workspace.id,
                    "parser_type": parser_type,
                    "document_type": document_type,
                },
                error_message="invalid_document_type",
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc

        if parser_type not in SUPPORTED_PARSER_TYPES:
            _safe_span(
                trace_service,
                "upload.validate",
                input_summary={
                    "filename": upload_filename,
                    "workspace_id": workspace.id,
                    "parser_type": parser_type,
                    "document_type": normalized_document_type,
                },
                error_message="invalid_parser_type",
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid parser_type. Supported types: {', '.join(SUPPORTED_PARSER_TYPES)}",
            )

        file_size = len(file_content)
        if file_size > MAX_FILE_SIZE:
            _safe_span(
                trace_service,
                "upload.validate",
                input_summary={
                    "filename": upload_filename,
                    "workspace_id": workspace.id,
                    "parser_type": parser_type,
                    "document_type": normalized_document_type,
                    "file_size": file_size,
                },
                error_message="file_too_large",
            )
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"File size exceeds maximum allowed size of {MAX_FILE_SIZE / 1024 / 1024}MB",
            )

        if require_parent_dir:
            _assert_parent_directory_exists(db, workspace.id, parent_logical_path)

        validated_path = validate_path(parent_logical_path)
        file_uri = build_file_uri(validated_path, upload_filename)

        existing_file = (
            db.query(FileModel)
            .filter(FileModel.uri == file_uri, FileModel.workspace_id == workspace.id)
            .first()
        )
        if existing_file:
            _safe_span(
                trace_service,
                "upload.validate",
                input_summary={
                    "filename": upload_filename,
                    "workspace_id": workspace.id,
                    "parser_type": parser_type,
                    "document_type": normalized_document_type,
                    "file_size": file_size,
                    "target_uri": file_uri,
                },
                error_message="duplicate_file",
            )
            raise HTTPException(
                status_code=duplicate_status_code,
                detail=f"File already exists at {file_uri}",
            )

        ct = resolve_effective_mime_type(content_type, upload_filename, parser_type)
        _safe_span(
            trace_service,
            "upload.validate",
            input_summary={
                "filename": upload_filename,
                "workspace_id": workspace.id,
                "parser_type": parser_type,
                "document_type": normalized_document_type,
                "file_size": file_size,
            },
            output_summary={
                "target_uri": file_uri,
                "mime_type": ct,
                "file_size": file_size,
                "supported_for_processing": ct in ALLOWED_MIME_TYPES,
            },
        )
        minio_storage = MinioStorage()
        minio_storage.put_file(workspace.slug, file_uri, file_content, content_type=ct)
        _safe_span(
            trace_service,
            "upload.store_minio",
            input_summary={
                "bucket": workspace.slug,
                "object_key": file_uri,
                "size_bytes": file_size,
                "content_type": ct,
            },
            output_summary={
                "bucket": workspace.slug,
                "object_key": file_uri,
                "size_bytes": file_size,
                "status": "stored",
            },
        )

        file_record = FileModel(
            uri=file_uri,
            name=upload_filename,
            owner_id=owner_user_id,
            workspace_id=workspace.id,
            is_directory=False,
            size=file_size,
            mime_type=ct,
            document_type=normalized_document_type,
            parser_type=parser_type if parser_type != "auto" else None,
        )
        db.add(file_record)
        db.commit()
        db.refresh(file_record)

        task_record: Optional[Task] = None
        if ct in ALLOWED_MIME_TYPES:
            task_service = TaskService(db)
            task_record = task_service.create_task(
                workspace_id=workspace.id,
                user_id=owner_user_id,
                file_id=file_record.id,
                task_type="process_document",
                queue="normal",
                priority=5,
                max_retries=3,
                status=TaskStatus.PENDING,
            )

        _safe_span(
            trace_service,
            "upload.create_records",
            input_summary={
                "workspace_id": workspace.id,
                "file_uri": file_uri,
                "document_type": normalized_document_type,
                "processing_supported": ct in ALLOWED_MIME_TYPES,
            },
            output_summary={
                "file_id": file_record.id,
                "task_id": task_record.id if task_record else None,
                "task_created": task_record is not None,
            },
        )

        _safe_finish_upload_run(
            db,
            trace_service,
            trace_id,
            file_id=file_record.id,
        )
        return file_record, task_record
    except Exception as exc:
        _safe_finish_upload_run(
            db,
            trace_service,
            trace_id,
            file_id=file_record.id if file_record is not None else None,
            failed_stage="upload",
            error_message=str(exc),
        )
        raise
    finally:
        if get_trace_context()["trace_type"] == "upload":
            reset_trace_context()


def replace_file_content(
    db: Session,
    workspace: Workspace,
    acting_user_id: int,
    file: FileModel,
    *,
    new_content: bytes,
    content_type: Optional[str],
    parser_type: str = "auto",
) -> Optional[Task]:
    """
    Overwrite storage object for ``file.uri``, reset processing pipeline, enqueue task.
    """
    if file.is_directory:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot replace directory content",
        )

    if parser_type not in SUPPORTED_PARSER_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid parser_type. Supported types: {', '.join(SUPPORTED_PARSER_TYPES)}",
        )

    file_size = len(new_content)
    if file_size > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File size exceeds maximum allowed size of {MAX_FILE_SIZE / 1024 / 1024}MB",
        )

    effective_mime = resolve_effective_mime_type(
        content_type or file.mime_type, file.name, parser_type
    )
    if effective_mime not in ALLOWED_MIME_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File type {effective_mime} is not supported for processing",
        )

    from openrag.api.files_api import cleanup_file_processing_data

    file.parser_type = parser_type if parser_type != "auto" else None
    file.document_type = normalize_document_type(getattr(file, "document_type", None))
    cleanup_file_processing_data(file, workspace.slug, db)
    db.commit()

    ct = resolve_effective_mime_type(content_type or file.mime_type, file.name, parser_type)
    minio_storage = MinioStorage()
    minio_storage.put_file(workspace.slug, file.uri, new_content, content_type=ct)

    file.size = file_size
    file.mime_type = ct
    db.add(file)
    db.commit()
    db.refresh(file)

    task_service = TaskService(db)
    task_record = task_service.create_task(
        workspace_id=file.workspace_id,
        user_id=acting_user_id,
        file_id=file.id,
        task_type="process_document",
        queue="normal",
        priority=5,
        max_retries=3,
        status=TaskStatus.PENDING,
    )
    return task_record
