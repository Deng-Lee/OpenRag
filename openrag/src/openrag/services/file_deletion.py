"""文件删除：对象存储、层级、向量与 DB 行（供 API 同步删除与 Worker 异步删除复用）。"""

import logging
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from openrag.hierarchy.hierarchy_storage import HierarchyStorage
from openrag.models.file import File as FileModel
from openrag.models.task import Task
from openrag.models.workspace import Workspace
from openrag.services.task_service import TaskService
from openrag.storage.minio_storage import MinioStorage

logger = logging.getLogger(__name__)


def delete_milvus_vectors_for_file(file_id: int) -> list[str]:
    """Remove chunk + L0/L1 layer vectors for a file from Milvus; best-effort ES chunk index.

    Returns:
        A list of failed subsystems, used for alerting/observability.
    """
    failures: list[str] = []
    try:
        from openrag.embedding.embedding_engine import EmbeddingEngine
        from openrag.vectorstore.milvus_store import MilvusStore

        MilvusStore(dimension=EmbeddingEngine().dimension).delete_by_file_id(file_id)
    except Exception as e:
        logger.warning("Milvus chunk delete failed for file %s: %s", file_id, e)
        failures.append("milvus_chunk")
    try:
        from openrag.embedding.embedding_engine import EmbeddingEngine
        from openrag.vectorstore.milvus_layer_store import MilvusLayerStore

        MilvusLayerStore(dimension=EmbeddingEngine().dimension).delete_by_file_id(
            file_id
        )
    except Exception as e:
        logger.warning("Milvus L0/L1 delete failed for file %s: %s", file_id, e)
        failures.append("milvus_l0_l1")
    if not _delete_elasticsearch_chunks_for_file_best_effort(file_id):
        failures.append("elasticsearch_chunks")
    if failures:
        logger.error(
            "ALERT: vector/chunk index deletion partially failed for file_id=%s, failed_subsystems=%s",
            file_id,
            ",".join(failures),
        )
    return failures


def _delete_elasticsearch_chunks_for_file_best_effort(file_id: int) -> bool:
    try:
        from openrag.database import SessionLocal
        from openrag.models.file import File as FileModel
        from openrag.models.workspace import Workspace
        from openrag.search.es_chunk_store import create_es_chunk_store_from_config
        from openrag.search.workspace_es_slug import build_workspace_chunks_index_name

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
            index_name = build_workspace_chunks_index_name(ws.slug, ws.id)
            store.delete_by_file_id(index_name, file_id)
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
) -> None:
    """删除存储与向量，移除任务记录，再删除 file 行并 commit。"""
    minio_storage = MinioStorage()
    hierarchy_storage = HierarchyStorage()
    slug = workspace.slug

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
            db.query(Task).filter(Task.file_id == child.id).delete(
                synchronize_session=False
            )
            hierarchy_storage.delete_document_hierarchy(file_uri=child.uri)
            minio_storage.remove_document_hierarchy(slug, child.uri)
            delete_milvus_vectors_for_file(child.id)
        minio_storage.remove_directory(slug, file.uri)
    else:
        minio_storage.remove_document_hierarchy(slug, file.uri)
        minio_storage.remove_file(slug, file.uri)

    hierarchy_storage.delete_document_hierarchy(file_uri=file.uri)
    delete_milvus_vectors_for_file(file.id)

    db.query(Task).filter(Task.file_id == file.id).delete(synchronize_session=False)
    db.delete(file)
    db.commit()


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


def _physically_delete_row_only(db: Session, file: FileModel, workspace: Workspace) -> None:
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
    if file.is_directory:
        # DB-only virtual node: NO MinIO object, NO remove_directory (prefix-recursive).
        pass
    else:
        minio_storage = MinioStorage()
        minio_storage.remove_document_hierarchy(workspace.slug, file.uri)
        minio_storage.remove_file(workspace.slug, file.uri)
        HierarchyStorage().delete_document_hierarchy(file_uri=file.uri)
        delete_milvus_vectors_for_file(file.id)
    db.query(Task).filter(Task.file_id == file.id).delete(synchronize_session=False)
    db.delete(file)
    db.commit()


def physically_delete_soft_deleted_under_prefix(
    db: Session,
    workspace_id: int,
    prefix: str,
    deleted_before: datetime,
    workspace: Workspace,
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
        _physically_delete_row_only(db, fresh, workspace)
        deleted_ids.append(f.id)
    return deleted_ids
