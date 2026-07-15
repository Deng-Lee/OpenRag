"""A2: 子文档解析完成后，自底向上聚合父目录 L0/L1 并写入存储与 Milvus layers。"""

import logging
from typing import Optional

from sqlalchemy.orm import Session

from openrag.hierarchy.directory_hierarchy_manager import DirectoryHierarchyManager
from openrag.hierarchy.hierarchy_storage import HierarchyStorage
from openrag.models.file import File, ProcessingStatus
from openrag.storage.minio_storage import MinioStorage

logger = logging.getLogger(__name__)


def _read_file_l0_text(
    f: File,
    bucket: str,
    minio: Optional[MinioStorage],
    hstorage: HierarchyStorage,
) -> str:
    if minio and bucket:
        text = minio.read_hierarchy_abstract(bucket, f.uri)
        if text and text.strip():
            return text.strip()
    alt = hstorage.load_l0(file_uri=f.uri)
    return (alt or "").strip()


def propagate_parent_directory_hierarchies(
    db: Session,
    bucket: str,
    minio: Optional[MinioStorage],
    layer_store,
    embedding_engine,
    leaf_file_id: int,
    hstorage: Optional[HierarchyStorage] = None,
) -> int:
    """
    沿 leaf 的 parent_id 向上，对每个目录节点：
    - 收集直接子项（已完成文件 + 已有 L0 的子目录）的摘要；
    - 聚合为目录 L0/L1，写入 MinIO（或本地 HierarchyStorage）；
    - 更新 File.l0_path / l1_path，并 upsert openrag_layers。

    Returns:
        成功更新的祖先目录层数。
    """
    hstorage = hstorage or HierarchyStorage()
    leaf = db.query(File).filter(File.id == leaf_file_id).first()
    if not leaf or leaf.is_directory:
        return 0

    mgr = DirectoryHierarchyManager()
    updated = 0
    pid = leaf.parent_id

    while pid is not None:
        parent = db.query(File).filter(File.id == pid).first()
        if parent is None or not parent.is_directory:
            break

        children = (
            db.query(File)
            .filter(File.parent_id == parent.id)
            .order_by(File.name.asc())
            .all()
        )
        children_l0s: list[str] = []
        children_names: list[str] = []

        for c in children:
            if c.is_directory:
                t = _read_file_l0_text(c, bucket, minio, hstorage)
                if not t:
                    continue
                children_l0s.append(t)
                children_names.append(f"{c.name}/")
            else:
                if c.processing_status != ProcessingStatus.completed:
                    continue
                t = _read_file_l0_text(c, bucket, minio, hstorage)
                if not t:
                    continue
                children_l0s.append(t)
                children_names.append(c.name)

        if not children_l0s:
            pid = parent.parent_id
            continue

        dir_path = parent.uri or parent.name
        dh = mgr.aggregate_directory(dir_path, children_l0s, children_names)
        layer_embeddings = []
        if layer_store is not None:
            layer_texts = [
                (layer, text.strip())
                for layer, text in (("l0", dh.l0), ("l1", dh.l1))
                if isinstance(text, str) and text.strip()
            ]
            layer_vectors = embedding_engine.embed_batch(
                [text for _, text in layer_texts]
            )
            if len(layer_vectors) != len(layer_texts):
                raise RuntimeError("Parent directory layer embedding count mismatch")
            layer_embeddings = [
                (layer, text, vector)
                for (layer, text), vector in zip(layer_texts, layer_vectors)
            ]

        try:
            if minio and bucket:
                urls = minio.put_document_hierarchy(
                    bucket,
                    parent.uri,
                    dh.l0,
                    dh.l1,
                    None,
                )
                parent.l0_path = urls.get("l0_url") or parent.l0_path
                parent.l1_path = urls.get("l1_url") or parent.l1_path
                parent.l2_path = None
            else:
                hstorage.save_directory_hierarchy(
                    file_uri=parent.uri,
                    l0=dh.l0,
                    l1=dh.l1,
                )
                parent.l0_path = hstorage.get_l0_path(parent.uri)
                parent.l1_path = hstorage.get_l1_path(parent.uri)
                parent.l2_path = None

            if layer_store is not None:
                try:
                    layer_store.upsert_file_layers(parent.id, layer_embeddings)
                except Exception as exc:
                    logger.warning(
                        "Milvus L0/L1 for directory file_id=%s failed: %s",
                        parent.id,
                        exc,
                    )

            db.add(parent)
            db.commit()
            updated += 1
        except Exception as exc:
            logger.warning(
                "Parent directory hierarchy update failed for parent_id=%s: %s",
                parent.id,
                exc,
            )
            db.rollback()

        pid = parent.parent_id

    return updated
