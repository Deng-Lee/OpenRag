"""文件删除：对象存储、层级、向量与 DB 行（供 API 同步删除与 Worker 异步删除复用）。"""

import logging
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from openrag.hierarchy.hierarchy_storage import HierarchyStorage
from openrag.models.file import File as FileModel
from openrag.models.index_generation import IndexGeneration, IndexGenerationState
from openrag.models.task import Task
from openrag.models.workspace import Workspace
from openrag.indexing.runtime import (
    IndexRuntimeResolver,
    build_vector_stores_from_snapshot,
)
from openrag.services.task_service import TaskService
from openrag.storage.minio_storage import MinioStorage

logger = logging.getLogger(__name__)


class GenerationDeletePropagationError(RuntimeError):
    code = "GENERATION_DELETE_INCOMPLETE"
    public_message = "File index deletion is incomplete"
    retryable = True

    def __init__(self, result: dict):
        super().__init__(self.public_message)
        self.result = result


_DELETE_TARGET_STATES = {
    IndexGenerationState.BUILDING.value,
    IndexGenerationState.RECONCILING.value,
    IndexGenerationState.VALIDATING.value,
    IndexGenerationState.READY.value,
    IndexGenerationState.ACTIVE.value,
    IndexGenerationState.RETIRED.value,
}


def resolve_generations_containing_file(
    db: Session, file_id: int
) -> list[IndexGeneration]:
    del file_id  # physical membership is conservatively assumed for every retained target
    return (
        db.query(IndexGeneration)
        .filter(
            or_(
                IndexGeneration.state.in_(_DELETE_TARGET_STATES),
                and_(
                    IndexGeneration.state == IndexGenerationState.FAILED.value,
                    IndexGeneration.build_started_at.is_not(None),
                ),
            )
        )
        .order_by(IndexGeneration.created_at)
        .all()
    )


def delete_file_from_generation(
    db: Session,
    generation: IndexGeneration,
    file_id: int,
    *,
    resolver: IndexRuntimeResolver | None = None,
) -> None:
    resolver = resolver or IndexRuntimeResolver()
    snapshot = resolver.get_generation_snapshot(db, generation.id)
    chunk_store, layer_store = build_vector_stores_from_snapshot(snapshot)
    chunk_store.delete_by_file_id(file_id)
    if layer_store is not None:
        layer_store.delete_by_file_id(file_id)


def record_generation_delete_result(
    result: dict,
    generation_id: str,
    *,
    success: bool,
) -> None:
    key = "succeeded_generation_ids" if success else "failed_generation_ids"
    result[key].append(generation_id)


def delete_vectors_for_file_across_generations(
    db: Session,
    file_id: int,
    *,
    resolver: IndexRuntimeResolver | None = None,
) -> dict:
    generations = resolve_generations_containing_file(db, file_id)
    result = {
        "file_id": file_id,
        "target_generation_ids": [item.id for item in generations],
        "succeeded_generation_ids": [],
        "failed_generation_ids": [],
        "failed_subsystems": [],
    }
    if not generations:
        result["failed_subsystems"].append("generation_registry")
        raise GenerationDeletePropagationError(result)
    resolver = resolver or IndexRuntimeResolver()
    for generation in generations:
        try:
            delete_file_from_generation(
                db, generation, file_id, resolver=resolver
            )
            record_generation_delete_result(result, generation.id, success=True)
        except Exception:
            logger.exception(
                "generation_vector_delete_failed generation_id=%s file_id=%s",
                generation.id,
                file_id,
            )
            record_generation_delete_result(result, generation.id, success=False)
    if not _delete_elasticsearch_chunks_for_file_best_effort(file_id):
        result["failed_subsystems"].append("elasticsearch_chunks")
    if result["failed_generation_ids"] or result["failed_subsystems"]:
        raise GenerationDeletePropagationError(result)
    return result


def _delete_elasticsearch_chunks_for_file_best_effort(file_id: int) -> bool:
    try:
        from openrag.database import SessionLocal
        from openrag.models.file import File as FileModel
        from openrag.models.workspace import Workspace
        from openrag.config import get_config
        from openrag.search.es_chunk_store import create_es_chunk_store_from_config
        from openrag.search.workspace_es_slug import (
            build_workspace_chunks_index_name,
            build_workspace_chunks_write_alias,
        )

        store = create_es_chunk_store_from_config()
        if store is None:
            return True
        db = SessionLocal()
        try:
            f = db.query(FileModel).filter(FileModel.id == file_id).first()
            if f is None:
                return True
            ws = db.query(Workspace).filter(Workspace.id == f.workspace_id).first()
            if ws is None:
                return True
            legacy_index = build_workspace_chunks_index_name(ws.slug, ws.id)
            write_alias = build_workspace_chunks_write_alias(ws.slug, ws.id)
            target = store.resolve_write_target(
                legacy_index=legacy_index,
                write_alias=write_alias,
                chunk_index_mode=get_config().elasticsearch.chunk_index_mode,
            )
            store.delete_by_file_id(target, file_id)
            return True
        finally:
            db.close()
    except Exception as e:
        logger.warning("Elasticsearch chunk delete failed for file %s: %s", file_id, e)
        return False


def delete_file_with_storage(
    db: Session,
    file: FileModel,
    workspace: Workspace,
) -> dict:
    """删除存储与向量，移除任务记录，再删除 file 行并 commit。"""
    minio_storage = MinioStorage()
    hierarchy_storage = HierarchyStorage()
    slug = workspace.slug
    delete_results = []

    if file.is_directory:
        directory_prefix = file.uri.rstrip("/")
        if not directory_prefix:
            raise ValueError("Refusing to delete workspace root path")
        children = (
            db.query(FileModel)
            .filter(
                FileModel.workspace_id == file.workspace_id,
                FileModel.uri.like(f"{_escape_like(directory_prefix)}/%", escape="\\"),
            )
            .all()
        )
        for child in children:
            delete_results.append(
                delete_vectors_for_file_across_generations(db, child.id)
            )
        delete_results.append(
            delete_vectors_for_file_across_generations(db, file.id)
        )
        for child in children:
            hierarchy_storage.delete_document_hierarchy(file_uri=child.uri)
            minio_storage.remove_document_hierarchy(slug, child.uri)
        minio_storage.remove_directory(slug, file.uri)
    else:
        delete_results.append(
            delete_vectors_for_file_across_generations(db, file.id)
        )
        minio_storage.remove_document_hierarchy(slug, file.uri)
        minio_storage.remove_file(slug, file.uri)

    hierarchy_storage.delete_document_hierarchy(file_uri=file.uri)

    db.delete(file)
    db.commit()
    return _merge_generation_delete_results(delete_results)


def normalize_uri_prefix_for_delete(path: str) -> str:
    """规范化待删除目录路径：禁止 ..、禁止删根目录 '/'。返回无尾部斜杠形式（如 /a/b）。"""
    import posixpath

    p = path.strip()
    normalized = posixpath.normpath(p)
    if ".." in normalized or normalized.startswith("/.."):
        raise ValueError("Invalid path: traversal not allowed")
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    normalized = normalized.rstrip("/")
    if not normalized:
        raise ValueError("Refusing to delete workspace root path")
    return normalized


def delete_files_under_uri_prefix(
    db: Session,
    workspace_id: int,
    uri_prefix: str,
    workspace: Workspace,
) -> list[int]:
    """删除某工作区内 uri 等于前缀或位于其下的所有文件/目录记录（含 MinIO、层级目录、Milvus、关联_task 等）。

    与单文件删除复用 ``delete_file_with_storage``。按 URI 长度从大到小删除，避免父记录先于子记录删除时产生残留。
    """
    prefix = normalize_uri_prefix_for_delete(uri_prefix)
    q = db.query(FileModel).filter(
        FileModel.workspace_id == workspace_id,
        or_(
            FileModel.uri == prefix,
            FileModel.uri.like(f"{_escape_like(prefix)}/%", escape="\\"),
        ),
    )
    rows = list(q.all())
    rows.sort(key=lambda f: (len(f.uri), f.id), reverse=True)
    deleted_ids: list[int] = []
    for f in rows:
        fresh = db.query(FileModel).filter(FileModel.id == f.id).first()
        if fresh is None:
            continue
        delete_file_with_storage(db, fresh, workspace)
        deleted_ids.append(f.id)
    return deleted_ids


def utcnow() -> datetime:
    """Naive UTC (Codex #5): match the project's naive DateTime/TIMESTAMP columns so
    ``deleted_at <= deleted_before`` compares identically on PostgreSQL and SQLite."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _escape_like(value: str) -> str:
    """Escape SQL LIKE wildcard chars in a logical URI prefix."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _release_tag_and_soft_delete(db: Session, file: FileModel, *, user_id: int) -> Task:
    """Single-row soft delete: set deleted_at + free tag + enqueue DELETE_FILE, ONE commit.

    On failure rolls back so the row never enters a half-deleted state (tag still
    released but no cleanup task, or vice versa).
    """
    try:
        file.deleted_at = utcnow()
        file.tag = None
        task = TaskService(db).add_task(
            workspace_id=file.workspace_id,
            user_id=user_id,
            file_id=file.id,
            task_type="delete_file",
            queue="normal",
            priority=6,
            max_retries=3,
        )
        db.commit()
        db.refresh(task)
        return task
    except Exception:
        db.rollback()
        raise


def soft_delete_subtree(db: Session, workspace_id: int, prefix: str) -> datetime:
    """Mark every ACTIVE row at/under ``prefix`` as soft-deleted (deleted_at + tag=None).

    Returns the watermark timestamp used (caller enqueues a DELETE_PATH_PREFIX task
    carrying it, then commits once). Does NOT commit.
    """
    p = prefix.rstrip("/")
    if not p:
        # Codex round-3 #2: an empty/"/" prefix would match the whole workspace
        # (uri LIKE "/%"). Refuse so no caller can soft-delete an entire workspace.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Refusing to soft-delete workspace root",
        )
    watermark = utcnow()
    rows = (
        db.query(FileModel)
        .filter(
            FileModel.workspace_id == workspace_id,
            or_(
                FileModel.uri == p,
                FileModel.uri.like(f"{_escape_like(p)}/%", escape="\\"),
            ),
            FileModel.deleted_at.is_(None),
        )
        .all()
    )
    for f in rows:
        f.deleted_at = watermark
        f.tag = None
    return watermark


def soft_delete_subtree_and_enqueue(
    db: Session, workspace_id: int, prefix: str, *, user_id: int
) -> Task:
    """Subtree soft delete in ONE commit (Codex #4): mark all active rows at/under
    ``prefix`` deleted + tag=None, enqueue a DELETE_PATH_PREFIX task carrying the
    watermark, commit once; rollback on any failure so no half-deleted state leaks.
    Shared by internal DELETE /files/{id} (directory) and delete-path-prefix.
    """
    p = prefix.rstrip("/")
    try:
        watermark = soft_delete_subtree(db, workspace_id, p)
        task = TaskService(db).add_task(
            workspace_id=workspace_id,
            user_id=user_id,
            file_id=None,
            task_type="delete_path_prefix",
            queue="normal",
            priority=6,
            max_retries=3,
            payload={"path": p, "deleted_before": watermark.isoformat()},
        )
        db.commit()
        db.refresh(task)
        return task
    except Exception:
        db.rollback()
        raise


def _physically_delete_row_only(
    db: Session, file: FileModel, workspace: Workspace
) -> dict:
    """Physically delete ONE row's storage/vectors/DB, WITHOUT touching any other
    row's objects (Codex round-2 #1 + round-3 #1).

    Two cascade traps avoided:
    - DB children: ``delete_file_with_storage`` deletes a directory's children by URI
      prefix ignoring ``deleted_at``; we never call it here.
    - MinIO objects: ``MinioStorage.remove_directory(prefix)`` is a *prefix-recursive*
      object delete (``list_objects(prefix, recursive=True)``), so calling it for a
      soft-deleted directory would wipe active children's objects that appeared after
      the soft-delete. Directory rows are DB-only virtual nodes with no MinIO object,
      so the directory branch touches **no** object storage at all.

    The watermark cleanup enumerates every soft-deleted row (children included) and
    deletes deepest-first, so each file's own objects are removed by its own row.
    """
    result = delete_vectors_for_file_across_generations(db, file.id)
    if file.is_directory:
        # DB-only virtual node: NO MinIO object, NO remove_directory (prefix-recursive).
        pass
    else:
        minio_storage = MinioStorage()
        minio_storage.remove_document_hierarchy(workspace.slug, file.uri)
        minio_storage.remove_file(workspace.slug, file.uri)
        HierarchyStorage().delete_document_hierarchy(file_uri=file.uri)
    db.delete(file)
    db.commit()
    return result


def physically_delete_soft_deleted_under_prefix(
    db: Session,
    workspace_id: int,
    prefix: str,
    deleted_before: datetime,
    workspace: Workspace,
    delete_results: list[dict] | None = None,
) -> list[int]:
    """Physically delete ONLY soft-deleted rows at/under ``prefix`` whose
    ``deleted_at <= deleted_before`` (deepest URI first). Active rows created after
    the task was enqueued are never touched. Uses the no-cascade single-row helper
    so a soft-deleted directory never drags active children with it (Codex #1).
    """
    p = (prefix or "").rstrip("/")
    if not p:
        # Codex round-4 #1: a root/empty/"/" prefix collapses to "" and the query below
        # becomes ``uri LIKE "/%"`` — i.e. EVERY row in the workspace. The soft-delete
        # side (soft_delete_subtree) already rejects this, but the worker can be handed a
        # legacy/manual/malformed task; fail closed here too so physical cleanup can NEVER
        # wipe an entire workspace's soft-deleted rows. ValueError (not HTTPException):
        # this runs in the worker, not an HTTP handler — the task fails loudly, deletes 0.
        raise ValueError("Refusing workspace-wide physical cleanup (empty/root prefix)")
    rows = (
        db.query(FileModel)
        .filter(
            FileModel.workspace_id == workspace_id,
            or_(
                FileModel.uri == p,
                FileModel.uri.like(f"{_escape_like(p)}/%", escape="\\"),
            ),
            FileModel.deleted_at.is_not(None),
            FileModel.deleted_at <= deleted_before,
        )
        .all()
    )
    rows.sort(key=lambda f: (len(f.uri), f.id), reverse=True)
    deleted_ids: list[int] = []
    for f in rows:
        fresh = db.query(FileModel).filter(FileModel.id == f.id).first()
        if fresh is None:
            continue
        result = _physically_delete_row_only(db, fresh, workspace)
        if delete_results is not None:
            delete_results.append(result)
        deleted_ids.append(f.id)
    return deleted_ids


def _merge_generation_delete_results(results: list[dict]) -> dict:
    merged = {
        "target_generation_ids": [],
        "succeeded_generation_ids": [],
        "failed_generation_ids": [],
        "failed_subsystems": [],
    }
    for result in results:
        for key in merged:
            merged[key].extend(
                value for value in result.get(key, []) if value not in merged[key]
            )
    return merged
