"""文件删除：对象存储、层级、向量与 DB 行（供 API 同步删除与 Worker 异步删除复用）。"""

import logging

from sqlalchemy import or_
from sqlalchemy.orm import Session

from openrag.hierarchy.hierarchy_storage import HierarchyStorage
from openrag.models.file import File as FileModel
from openrag.models.task import Task
from openrag.models.workspace import Workspace
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
        children = (
            db.query(FileModel)
            .filter(
                FileModel.workspace_id == file.workspace_id,
                FileModel.uri.like(f"{file.uri}/%"),
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
        or_(FileModel.uri == prefix, FileModel.uri.like(f"{prefix}/%")),
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
