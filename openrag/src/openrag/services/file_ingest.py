"""Shared file upload / replace logic for JWT and service-token APIs."""

from __future__ import annotations

import mimetypes
import posixpath
import re
import uuid
from typing import Optional, Tuple

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from openrag.chunking.document_type import DEFAULT_DOCUMENT_TYPE, normalize_document_type
from openrag.models.document_chunk import DocumentChunk
from openrag.models.file import File as FileModel, ProcessingStatus
from openrag.models.task import Task, TaskStatus
from openrag.models.workspace import Workspace
from openrag.parsers.selection import resolve_pdf_default_parser_type
from openrag.services.document_retry_status import document_conflict_detail
from openrag.services.file_deletion import delete_vectors_for_file_across_generations
from openrag.services.task_service import TaskService
from openrag.services.trace_service import TraceService
from openrag.services.upload_policy import validate_upload_size
from openrag.storage.minio_storage import MinioStorage
from openrag.tracing.context import get_trace_context, reset_trace_context, set_trace_context

# Leaf filename byte budget. The worker downloads each file to a local temp path
# named ``doc_{file_id}_{basename}`` (worker/task_worker.py), where a single path
# component is capped at 255 bytes by the OS (ENAMETOOLONG). 200 leaves headroom
# for the ``doc_{id}_`` prefix and keeps ``name`` within the 255-char DB column.
# Counted in BYTES (not chars): non-ASCII names cost >1 byte/char in UTF-8.
# Mirror this value in web/src/utils/folderUpload.ts (MAX_FILENAME_BYTES).
MAX_FILENAME_BYTES = 200

_TAG_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _normalize_tag(tag):
    """Trim; blank -> None; else must match the tag charset (raises 400)."""
    tag = (tag or "").strip()
    if not tag:
        return None
    if not _TAG_RE.match(tag):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid tag. Allowed: ^[A-Za-z0-9._:-]{1,128}$",
        )
    return tag


def _violated_unique_constraint(exc):
    """Return 'tag' | 'uri' | None for a unique-violation IntegrityError.

    Works for PostgreSQL (constraint name in message) and SQLite (column list).
    """
    msg = str(getattr(exc, "orig", exc) or "")
    if "uq_files_workspace_tag" in msg or "files.tag" in msg:
        return "tag"
    if "uq_files_workspace_uri" in msg or "files.uri" in msg:
        return "uri"
    return None
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
    "deepdoc",
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
    "deepdoc": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "json": "application/json",
    "epub": "application/epub+zip",
}
_PDF_MAGIC = b"%PDF-"


def _is_pdf_filename(filename: str) -> bool:
    return posixpath.splitext((filename or "").lower())[1] == ".pdf"


def _has_pdf_magic(file_content: bytes) -> bool:
    return bool(file_content) and file_content.startswith(_PDF_MAGIC)


def _assert_paddleocr_pdf_content(file_content: bytes) -> None:
    if _has_pdf_magic(file_content):
        return
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="PaddleOCR parser only supports PDF files",
    )


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
    effective_mime = resolve_effective_mime_type(content_type, filename, parser_type)
    parser_hint = (parser_type or "").strip().lower()
    if parser_hint in {"pdf", "deepdoc"}:
        return _is_pdf_filename(filename)
    return effective_mime in ALLOWED_MIME_TYPES


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


def _get_or_create_directory_row(
    db: Session,
    workspace: Workspace,
    uri: str,
    name: str,
) -> FileModel:
    """Return the directory row at ``uri`` in ``workspace``, creating it if absent.

    Mirrors ``files_api.create_directory`` (DB-only virtual directory; no MinIO
    object). Raises 409 if ``uri`` already exists as a *file* rather than a
    directory. May raise ``IntegrityError`` on a concurrent insert race; the
    public wrapper handles that by rolling back and retrying.
    """
    row = (
        db.query(FileModel)
        .filter(FileModel.workspace_id == workspace.id, FileModel.uri == uri)
        .first()
    )
    if row is not None:
        if row.deleted_at is not None:
            # Soft-deleted directory still occupies its uri (uq_files_workspace_uri);
            # don't reuse it and don't try to recreate (would hit IntegrityError).
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Parent path pending deletion; retry after cleanup completes",
            )
        if not row.is_directory:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Path component already exists as a file: {uri}",
            )
        return row
    row = FileModel(
        uri=uri,
        name=name,
        owner_id=workspace.owner_id,
        workspace_id=workspace.id,
        is_directory=True,
        size=0,
    )
    db.add(row)
    db.flush()  # assigns row.id; may raise IntegrityError on a race
    return row


def _ensure_directory_path_one_pass(
    db: Session, workspace: Workspace, path: str
) -> FileModel:
    """Create root + each path component as a directory; return the deepest row."""
    deepest = _get_or_create_directory_row(db, workspace, "/", "root")
    if path != "/":
        cumulative = ""
        for part in [p for p in path.split("/") if p]:
            cumulative = f"{cumulative}/{part}"
            deepest = _get_or_create_directory_row(db, workspace, cumulative, part)
    return deepest


def ensure_directory_path(
    db: Session, workspace: Workspace, logical_path: str
) -> FileModel:
    """Idempotently create ``logical_path`` and all missing ancestors (``mkdir -p``).

    Returns the deepest directory row. Seeds the workspace root ``/`` if missing.
    Safe under concurrency: a lost insert race rolls back and retries; after a
    racing writer commits a level, our retry finds it and performs no insert.

    Commits in its own transaction: directories are durable like ``mkdir -p`` (an
    empty KB dir is a valid end state), and isolating the commit keeps these rows
    safe from a caller's internal rollbacks.
    """
    path = validate_path(logical_path)
    last_exc: Optional[IntegrityError] = None
    for _ in range(3):
        try:
            deepest = _ensure_directory_path_one_pass(db, workspace, path)
            db.commit()
            return deepest
        except IntegrityError as exc:  # concurrent create of the same level
            last_exc = exc
            db.rollback()
    # Exhausted retries under sustained contention: surface a clean, retryable
    # error rather than leaking a raw IntegrityError (which would become a 500).
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Directory creation contended; please retry",
    ) from last_exc


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
                FileModel.deleted_at.is_(None),
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
                FileModel.deleted_at.is_(None),
            )
            .first()
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Parent directory does not exist",
        )


def assert_no_pending_deleted_ancestor(db: Session, workspace_id: int, target_uri: str) -> None:
    """409 if ``target_uri`` or any ancestor path has a soft-deleted row (pending
    physical cleanup). Prevents writing/moving active rows into a deleting subtree
    (Codex #2). Soft-delete frees tag but not uri, so a soft-deleted ancestor row
    still occupies its uri until the worker cleans it.
    """
    candidates: list[str] = []
    cumulative = ""
    for part in [p for p in target_uri.split("/") if p]:
        cumulative = f"{cumulative}/{part}"
        candidates.append(cumulative)
    if not candidates:
        return
    clash = (
        db.query(FileModel)
        .filter(
            FileModel.workspace_id == workspace_id,
            FileModel.uri.in_(candidates),
            FileModel.deleted_at.is_not(None),
        )
        .first()
    )
    if clash is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Path is pending deletion; retry after cleanup completes",
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
    tag: Optional[str] = None,
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

        parser_type = resolve_pdf_default_parser_type(upload_filename, parser_type)

        normalized_tag = _normalize_tag(tag)

        # Reject over-long file names up front (before MinIO/DB writes) so the
        # caller gets a clear 400 instead of a silent worker failure: the worker
        # writes the file to a local temp path and the OS caps a single path
        # component at 255 bytes (ENAMETOOLONG). Check the leaf component in bytes.
        leaf_name = posixpath.basename((upload_filename or "").replace("\\", "/"))
        filename_bytes = len(leaf_name.encode("utf-8"))
        if filename_bytes > MAX_FILENAME_BYTES:
            _safe_span(
                trace_service,
                "upload.validate",
                input_summary={
                    "filename": upload_filename,
                    "workspace_id": workspace.id,
                    "parser_type": parser_type,
                    "document_type": normalized_document_type,
                    "filename_bytes": filename_bytes,
                },
                error_message="filename_too_long",
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"File name too long: {filename_bytes} bytes "
                    f"(max {MAX_FILENAME_BYTES}). Please shorten the file name."
                ),
            )

        file_size = len(file_content)
        try:
            validate_upload_size(file_size)
        except HTTPException:
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
            raise

        if normalized_tag is not None:
            tag_clash = (
                db.query(FileModel)
                .filter(
                    FileModel.workspace_id == workspace.id,
                    FileModel.tag == normalized_tag,
                )
                .first()
            )
            if tag_clash is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Tag already in use",
                )

        if require_parent_dir:
            _assert_parent_directory_exists(db, workspace.id, parent_logical_path)

        validated_path = validate_path(parent_logical_path)
        # Codex #2: refuse writing into a subtree pending physical deletion.
        assert_no_pending_deleted_ancestor(db, workspace.id, validated_path)
        if not require_parent_dir:
            # Auto-create the parent directory chain so the directory tree (web
            # lazy-load and the service-token /tree, /children endpoints) can show
            # these folders. Idempotent + concurrency-safe; mirrors the create_dirs
            # path in service_api. Runs before the file row is written, so a failure
            # here aborts the upload without leaving a parentless "orphan" file.
            ensure_directory_path(db, workspace, validated_path)
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
            detail = f"File already exists at {file_uri}"
            if duplicate_status_code == status.HTTP_409_CONFLICT:
                detail = document_conflict_detail(db, existing_file, file_uri)
            raise HTTPException(
                status_code=duplicate_status_code,
                detail=detail,
            )

        parser_hint = (parser_type or "").strip().lower()
        ct = resolve_effective_mime_type(content_type, upload_filename, parser_type)
        if parser_hint == "pdf":
            _assert_paddleocr_pdf_content(file_content)
            ct = "application/pdf"
            processing_supported = True
        else:
            processing_supported = is_processing_supported(
                content_type, upload_filename, parser_type
            )
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
                "supported_for_processing": processing_supported,
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
            tag=normalized_tag,
        )
        db.add(file_record)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            kind = _violated_unique_constraint(exc)
            if kind == "tag":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Tag already in use",
                ) from exc
            if kind == "uri":
                detail = f"File already exists at {file_uri}"
                if duplicate_status_code == status.HTTP_409_CONFLICT:
                    existing_file = (
                        db.query(FileModel)
                        .filter(
                            FileModel.uri == file_uri,
                            FileModel.workspace_id == workspace.id,
                        )
                        .first()
                    )
                    if existing_file is not None:
                        detail = document_conflict_detail(db, existing_file, file_uri)
                raise HTTPException(
                    status_code=duplicate_status_code,
                    detail=detail,
                ) from exc
            raise
        db.refresh(file_record)

        task_record: Optional[Task] = None
        if processing_supported:
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
                "processing_supported": processing_supported,
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

    parser_type = resolve_pdf_default_parser_type(file.name, parser_type)

    file_size = len(new_content)
    validate_upload_size(file_size)

    parser_hint = (parser_type or "").strip().lower()
    effective_mime = resolve_effective_mime_type(
        content_type or file.mime_type, file.name, parser_type
    )
    if parser_hint == "pdf":
        _assert_paddleocr_pdf_content(new_content)
        effective_mime = "application/pdf"
    elif not is_processing_supported(content_type or file.mime_type, file.name, parser_type):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File type {effective_mime} is not supported for processing",
        )

    from openrag.api.files_api import cleanup_file_processing_data

    file.parser_type = parser_type if parser_type != "auto" else None
    file.document_type = normalize_document_type(getattr(file, "document_type", None))
    cleanup_file_processing_data(file, workspace.slug, db)
    db.commit()

    ct = effective_mime
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


def _active_tagged_file(db: Session, workspace_id: int, tag: str) -> Optional[FileModel]:
    """Return the single ACTIVE (deleted_at IS NULL) non-directory row carrying ``tag``."""
    return (
        db.query(FileModel)
        .filter(
            FileModel.workspace_id == workspace_id,
            FileModel.tag == tag,
            FileModel.is_directory.is_(False),
            FileModel.deleted_at.is_(None),
        )
        .first()
    )


def _move_replace_no_intermediate_commit(
    db: Session,
    workspace: Workspace,
    acting_user_id: int,
    file: FileModel,
    target_uri: str,
    *,
    new_content: bytes,
    content_type: Optional[str],
    parser_type: str = "auto",
) -> Optional[Task]:
    """Same-row move+replace with NO intermediate commit (spec §4.8).

    Unlike chaining move_file + replace_file_content (each commits and touches storage
    several times), this keeps the failure window minimal: the File row is NEVER deleted
    and the tag is NEVER released, so on any failure the original tag still resolves to a
    reachable document. Sequence:
      1. validate parser/size/mime up front (no writes yet);
      2. write the NEW content to ``target_uri`` (if this fails, the DB is untouched);
      3. in ONE DB transaction: repoint the SAME row's uri/name/size/mime_type/parser_type/
         processing fields, delete its DocumentChunk rows, enqueue process_document via the
         non-committing add_task, then a single commit;
      4. on commit failure: rollback + best-effort delete the just-written new object;
      5. on commit success: best-effort cleanup of the OLD uri's object/hierarchy/vectors.
    """
    if parser_type not in SUPPORTED_PARSER_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid parser_type. Supported types: {', '.join(SUPPORTED_PARSER_TYPES)}",
        )
    parser_type = resolve_pdf_default_parser_type(target_uri, parser_type)
    file_size = len(new_content)
    validate_upload_size(file_size)
    target_name = posixpath.basename(target_uri)
    parser_hint = (parser_type or "").strip().lower()
    effective_mime = resolve_effective_mime_type(
        content_type or file.mime_type, target_name, parser_type
    )
    if parser_hint == "pdf":
        _assert_paddleocr_pdf_content(new_content)
        effective_mime = "application/pdf"
    elif not is_processing_supported(
        content_type or file.mime_type, target_name, parser_type
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File type {effective_mime} is not supported for processing",
        )

    old_uri = file.uri
    minio_storage = MinioStorage()
    # (2) write NEW object first; if this raises, the DB is still untouched.
    minio_storage.put_file(workspace.slug, target_uri, new_content, content_type=effective_mime)

    # (3) single DB transaction: repoint the same row + clear chunks + enqueue, one commit.
    try:
        file.uri = target_uri
        file.name = posixpath.basename(target_uri)
        file.size = file_size
        file.mime_type = effective_mime
        file.parser_type = parser_type if parser_type != "auto" else None
        file.processing_status = ProcessingStatus.pending
        file.processing_error = None
        file.l0_path = None
        file.l1_path = None
        file.l2_path = None
        file.l0_vector_id = None
        file.total_chunks = 0
        file.total_tokens = 0
        db.query(DocumentChunk).filter(DocumentChunk.file_id == file.id).delete(
            synchronize_session=False
        )
        task = TaskService(db).add_task(
            workspace_id=file.workspace_id,
            user_id=acting_user_id,
            file_id=file.id,
            task_type="process_document",
            queue="normal",
            priority=5,
            max_retries=3,
            status=TaskStatus.PENDING,
        )
        db.commit()
        db.refresh(file)
    except Exception:
        db.rollback()  # (4) row reverts to old uri/tag; never deleted, tag never released
        try:
            minio_storage.remove_file(workspace.slug, target_uri)  # best-effort: drop just-written object
        except Exception:
            pass
        raise

    # (5) commit succeeded -> best-effort cleanup of the OLD uri's storage + vectors.
    try:
        minio_storage.remove_file(workspace.slug, old_uri)
    except Exception:
        pass
    try:
        minio_storage.remove_document_hierarchy(workspace.slug, old_uri)
    except Exception:
        pass
    try:
        delete_vectors_for_file_across_generations(db, file.id)
    except Exception:
        pass
    return task


def upsert_file_by_tag(
    db: Session,
    workspace: Workspace,
    owner_user_id: int,
    *,
    tag: str,
    target_path: str,
    file_content: bytes,
    content_type: Optional[str],
    parser_type: str = "auto",
    create_dirs: bool = False,
) -> Tuple[FileModel, Optional[Task], str]:
    """Idempotent upsert keyed by per-workspace ``tag`` (spec §4.8).

    ``target_path`` is the FULL file logical path (e.g. ``/dir/name.pdf``); the uri is
    NEVER derived from the multipart filename. Branches on the single ACTIVE row carrying
    ``tag``:
    - none                       -> CREATE at target_path           (action "created")
    - exists, existing.uri == target_uri -> UPDATE content in place (action "updated")
    - exists, existing.uri != target_uri -> same-row MOVE + replace  (action "moved")

    Returns ``(file, task, action)``. Raises HTTPException(400) on an invalid/empty tag.
    """
    normalized_tag = _normalize_tag(tag)
    if normalized_tag is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="upsert-by-tag requires a non-empty tag",
        )

    target_uri = validate_path(target_path)
    basename = posixpath.basename(target_uri)
    if not basename:
        # target_path must point at a file, not the root / a directory (no basename).
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="target_path must be a full file path (file name required)",
        )

    # Unified pre-validation BEFORE branching (spec §4.8: validate parser/size/mime first)
    # so create/update/move reject the same bad inputs. Without this, the create branch's
    # ingest_new_file would store an unsupported-MIME file and skip the task WITHOUT a 400,
    # diverging from the update/move branches (which 400 on unsupported MIME).
    if parser_type not in SUPPORTED_PARSER_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid parser_type. Supported types: {', '.join(SUPPORTED_PARSER_TYPES)}",
        )
    parser_type = resolve_pdf_default_parser_type(basename, parser_type)
    validate_upload_size(len(file_content))
    parser_hint = (parser_type or "").strip().lower()
    effective_mime = resolve_effective_mime_type(content_type, basename, parser_type)
    if parser_hint == "pdf":
        _assert_paddleocr_pdf_content(file_content)
        effective_mime = "application/pdf"
    elif not is_processing_supported(content_type, basename, parser_type):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File type {effective_mime} is not supported for processing",
        )

    existing = _active_tagged_file(db, workspace.id, normalized_tag)

    if existing is None:
        # CREATE. Check the FULL target_uri for a pending-deletion row first: ingest_new_file
        # only guards the PARENT path, so a soft-deleted row sitting at target_uri itself
        # would otherwise fall through to its generic "File already exists" (409). spec §4.9
        # wants "pending deletion" semantics so callers can distinguish "occupied by an
        # active doc" from "old doc still being physically cleaned up".
        assert_no_pending_deleted_ancestor(db, workspace.id, target_uri)
        file_record, task = ingest_new_file(
            db,
            workspace,
            owner_user_id,
            parent_logical_path=posixpath.dirname(target_uri) or "/",
            upload_filename=basename,
            file_content=file_content,
            content_type=content_type,
            parser_type=parser_type,
            require_parent_dir=not create_dirs,
            duplicate_status_code=status.HTTP_409_CONFLICT,
            tag=normalized_tag,
        )
        return file_record, task, "created"

    if existing.uri == target_uri:
        task = replace_file_content(
            db,
            workspace,
            owner_user_id,
            existing,
            new_content=file_content,
            content_type=content_type,
            parser_type=parser_type,
        )
        db.refresh(existing)
        return existing, task, "updated"

    # existing.uri != target_uri -> same-row move + replace (no intermediate commit).
    assert_no_pending_deleted_ancestor(db, workspace.id, target_uri)
    occupied = (
        db.query(FileModel)
        .filter(
            FileModel.workspace_id == workspace.id,
            FileModel.uri == target_uri,
            FileModel.deleted_at.is_(None),
        )
        .first()
    )
    if occupied is not None and occupied.id != existing.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Target path already occupied by another document: {target_uri}",
        )
    parent = posixpath.dirname(target_uri) or "/"
    if create_dirs:
        ensure_directory_path(db, workspace, parent)
    else:
        _assert_parent_directory_exists(db, workspace.id, parent)
    task = _move_replace_no_intermediate_commit(
        db,
        workspace,
        owner_user_id,
        existing,
        target_uri,
        new_content=file_content,
        content_type=content_type,
        parser_type=parser_type,
    )
    db.refresh(existing)
    return existing, task, "moved"
