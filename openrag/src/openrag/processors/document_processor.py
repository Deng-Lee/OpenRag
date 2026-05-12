"""Document processor orchestrating the complete processing pipeline."""

import logging
from typing import TYPE_CHECKING, Callable, Optional

from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from openrag.search.es_chunk_store import EsChunkStore
    from openrag.vectorstore.milvus_layer_store import MilvusLayerStore

from openrag.chunking.chunk_engine import ChunkEngine
from openrag.chunking.chunk_params import chunk_size_overlap_from_env, min_chunk_tokens_from_env, resolve_chunk_method
from openrag.embedding.embedding_engine import EmbeddingEngine
from openrag.hierarchy.document_hierarchy_builder import DocumentHierarchyBuilder
from openrag.hierarchy.hierarchy_storage import HierarchyStorage
from openrag.models.document_chunk import DocumentChunk
from openrag.models.file import File, ProcessingStatus
from openrag.models.workspace import Workspace
from openrag.parsers.parser_registry import ParserRegistry
from openrag.storage.minio_storage import MinioStorage, chunk_object_key

logger = logging.getLogger(__name__)

_TEXT_PREVIEW_MAX = 16000


class DocumentProcessor:
    """Orchestrates the complete document processing pipeline."""

    def __init__(
        self,
        db: Session,
        parser_registry: ParserRegistry,
        chunk_engine: ChunkEngine,
        embedding_engine: EmbeddingEngine,
        hierarchy_storage: Optional[HierarchyStorage] = None,
        minio_storage: Optional[MinioStorage] = None,
        vector_store=None,
        layer_store: Optional["MilvusLayerStore"] = None,
        chunk_fulltext_store: Optional["EsChunkStore"] = None,
        l0_max_tokens: int = 100,
        l1_max_tokens: int = 2000,
        l1_section_preview_tokens: int = 200,
    ):
        self.db = db
        self.parser_registry = parser_registry
        self.chunk_engine = chunk_engine
        self.embedding_engine = embedding_engine
        self.hierarchy_storage = hierarchy_storage or HierarchyStorage()
        self.minio_storage = minio_storage
        self.vector_store = vector_store
        self.layer_store = layer_store
        self.chunk_fulltext_store = chunk_fulltext_store
        self.hierarchy_builder = DocumentHierarchyBuilder(
            l0_max_tokens=l0_max_tokens,
            l1_max_tokens=l1_max_tokens,
            l1_section_preview_tokens=l1_section_preview_tokens,
        )

    def process_document(
        self,
        file_path: str,
        file_id: int,
        user_id: int,
        parser_type: str = "auto",
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> dict:
        """Process a document through the complete pipeline.

        Steps:
        1. Parse document to extract text blocks
        2. Chunk text blocks
        3. Build L0/L1/L2 hierarchy
        4. Generate embeddings for chunks
        5. Store vectors in Milvus
        6. Store hierarchy files
        7. Update file metadata in DB
        """
        file_record = self.db.query(File).filter(File.id == file_id).first()
        if not file_record:
            raise ValueError(f"File not found: {file_id}")

        # Step 1: Parse（策略仅在 ParserRegistry / Factory；此处只编排）
        file_record.processing_status = ProcessingStatus.parsing
        self.db.commit()
        parser = self.parser_registry.get_parser(file_path, parser_type)
        print(
            "  [PIPELINE] Step 1 — "
            f"selected_parser={parser.__class__.__module__}.{parser.__class__.__name__}, "
            f"parser_type={parser_type}, file_path={file_path}"
        )
        text_blocks = parser.parse(file_path)
        print(f"  [PIPELINE] Step 1 — Parsed {len(text_blocks)} text blocks")
        for i, b in enumerate(text_blocks[:3]):
            print(f"    block[{i}]: level={b.level}, type={b.block_type}, text={b.text[:80]!r}")
        if progress_callback:
            progress_callback(20)

        # Step 2: Chunk（chunk_method / size 由 chunk_params + env 决定，不在此写类型分支）
        chunk_size, chunk_overlap = chunk_size_overlap_from_env()
        chunk_method = resolve_chunk_method(file_path, parser_type)
        min_chunk_tokens = min_chunk_tokens_from_env()
        chunks = self.chunk_engine.chunk(
            text_blocks,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            chunk_method=chunk_method,
            min_chunk_tokens=min_chunk_tokens,
        )
        print(
            f"  [PIPELINE] Step 2 — method={chunk_method}, size={chunk_size}, overlap={chunk_overlap}, "
            f"min_chunk_tokens={min_chunk_tokens}, chunks={len(chunks)}"
        )
        for i, c in enumerate(chunks[:3]):
            print(f"    chunk[{i}]: level={c.level}, type={c.block_type}, text={c.text[:80]!r}")
        if progress_callback:
            progress_callback(35)

        # Step 3: Build L0/L1/L2 hierarchy
        file_record.processing_status = ProcessingStatus.building_hierarchy
        self.db.commit()
        hierarchy_result = self.hierarchy_builder.build_hierarchy(chunks)
        print(
            f"  [PIPELINE] Step 3 — Hierarchy: L0={len(hierarchy_result.l0)} chars, "
            f"L1 overview={len(hierarchy_result.l1)} chars, L2={len(hierarchy_result.l2)} chunks"
        )
        if progress_callback:
            progress_callback(50)

        # Step 4: Generate embeddings
        file_record.processing_status = ProcessingStatus.embedding
        self.db.commit()
        chunk_embeddings = self.embedding_engine.embed_chunks(chunks)
        print(f"  [PIPELINE] Step 4 — Generated {len(chunk_embeddings)} embeddings")
        if progress_callback:
            progress_callback(65)

        # Step 5: Store vectors in Milvus (if available)
        vectors_stored = 0
        if self.vector_store is not None:
            self.vector_store.delete_by_file_id(file_id)
            vectors_stored = self.vector_store.insert_chunks(file_id, chunk_embeddings)
            print(f"  [PIPELINE] Step 5 — Stored {vectors_stored} vectors in Milvus")
        else:
            print("  [PIPELINE] Step 5 — SKIPPED (no vector_store)")

        # Step 5a: Elasticsearch 全文（与 Milvus 同一批 chunk；失败仅告警）
        if (
            vectors_stored > 0
            and self.chunk_fulltext_store is not None
            and self.vector_store is not None
        ):
            ws_row = (
                self.db.query(Workspace)
                .filter(Workspace.id == file_record.workspace_id)
                .first()
            )
            if ws_row:
                try:
                    from openrag.search.workspace_es_slug import (
                        build_workspace_chunks_index_name,
                        normalize_workspace_slug_segment,
                    )

                    index_name = build_workspace_chunks_index_name(
                        ws_row.slug, ws_row.id
                    )
                    slug_kw = normalize_workspace_slug_segment(ws_row.slug, ws_row.id)
                    self.chunk_fulltext_store.ensure_index(index_name)
                    es_docs: list[dict] = []
                    for chunk, _emb in chunk_embeddings:
                        cid = str(getattr(chunk, "chunk_id", "") or "")[:64]
                        if not cid:
                            continue
                        text = getattr(chunk, "text", str(chunk))
                        md = getattr(chunk, "metadata", {}) or {}
                        es_docs.append(
                            {
                                "chunk_id": cid,
                                "file_id": file_id,
                                "workspace_id": file_record.workspace_id,
                                "workspace_slug": slug_kw,
                                "content": text[:65000],
                                "doc_type_kwd": str(md.get("doc_type_kwd", "text"))[:16],
                                "content_with_weight": str(
                                    md.get("content_with_weight", text)
                                )[:65000],
                                "content_ltks": str(md.get("content_ltks", ""))[:65000],
                                "content_sm_ltks": str(md.get("content_sm_ltks", ""))[
                                    :65000
                                ],
                                "mom_with_weight": str(md.get("mom_with_weight", ""))[
                                    :65000
                                ],
                            }
                        )
                    n_es = self.chunk_fulltext_store.bulk_upsert_chunks(
                        index_name, es_docs
                    )
                    print(f"  [PIPELINE] Step 5a — Indexed {n_es} chunks in Elasticsearch")
                except Exception as exc:
                    logger.warning("Elasticsearch chunk index failed: %s", exc)

        # Step 5b: L0/L1 向量写入 Milvus（与 openrag_layers 集合对齐）
        if self.layer_store is not None:
            try:
                n_layers = self.layer_store.upsert_file_layers(
                    file_id,
                    hierarchy_result.l0,
                    hierarchy_result.l1,
                    self.embedding_engine.embed_text,
                )
                print(f"  [PIPELINE] Step 5b — Stored {n_layers} L0/L1 layer vectors")
            except Exception as exc:
                logger.warning("Layer vector upsert failed: %s", exc)
        if progress_callback:
            progress_callback(80)

        # Step 6: Store hierarchy (MinIO 为主，与源文件同 bucket；无 MinIO 时回退本地)
        print(f"  [PIPELINE] Step 6 — Saving hierarchy to uri={file_record.uri!r}")
        workspace = (
            self.db.query(Workspace)
            .filter(Workspace.id == file_record.workspace_id)
            .first()
        )
        bucket = workspace.slug if workspace else None
        if self.minio_storage and bucket:
            urls = self.minio_storage.put_document_hierarchy(
                bucket,
                file_record.uri,
                hierarchy_result.l0,
                hierarchy_result.l1,
                hierarchy_result.l2,
            )
            file_record.l0_path = urls.get("l0_url") or None
            file_record.l1_path = urls.get("l1_url") or None
            file_record.l2_path = urls.get("l2_url") or None
        else:
            self.hierarchy_storage.save_document_hierarchy(
                file_uri=file_record.uri,
                l0=hierarchy_result.l0,
                l1=hierarchy_result.l1,
                l2=hierarchy_result.l2,
            )
            file_record.l0_path = self.hierarchy_storage.get_l0_path(file_record.uri)
            file_record.l1_path = self.hierarchy_storage.get_l1_path(file_record.uri)
            file_record.l2_path = self.hierarchy_storage.get_l2_path(file_record.uri)
        if progress_callback:
            progress_callback(90)

        # Step 6b: 持久化切片元数据（与 Milvus chunk_id、MinIO object_key 对齐）
        self.db.query(DocumentChunk).filter(DocumentChunk.file_id == file_id).delete(
            synchronize_session=False
        )
        for i, chunk in enumerate(chunks):
            cid = str(getattr(chunk, "chunk_id", "") or "")[:64]
            if not cid:
                logger.warning("Skipping chunk without chunk_id at index %s", i)
                continue
            key = chunk_object_key(file_record.uri, i)
            if self.minio_storage and bucket:
                obj_url = self.minio_storage.path_style_http_url(bucket, key)
                local_path = None
            else:
                obj_url = None
                local_path = self.hierarchy_storage.get_chunk_file_path(
                    file_record.uri, i
                )
            text = getattr(chunk, "text", str(chunk))
            preview = text if len(text) <= _TEXT_PREVIEW_MAX else text[:_TEXT_PREVIEW_MAX]
            bx = getattr(chunk, "bbox", None)
            bbox_x0 = bbox_y0 = bbox_x1 = bbox_y1 = None
            if bx is not None and len(bx) == 4:
                bbox_x0, bbox_y0, bbox_x1, bbox_y1 = (
                    float(bx[0]),
                    float(bx[1]),
                    float(bx[2]),
                    float(bx[3]),
                )
            sbid = getattr(chunk, "source_block_id", None)
            scs = getattr(chunk, "source_char_start", None)
            sce = getattr(chunk, "source_char_end", None)
            self.db.add(
                DocumentChunk(
                    file_id=file_id,
                    workspace_id=file_record.workspace_id,
                    chunk_id=cid,
                    chunk_index=i,
                    object_key=key,
                    object_url=obj_url,
                    local_chunk_path=local_path,
                    text_preview=preview,
                    page=getattr(chunk, "page", 0),
                    level=getattr(chunk, "level", 0),
                    block_type=str(getattr(chunk, "block_type", "text"))[:32],
                    start_offset=getattr(chunk, "start_offset", 0),
                    end_offset=getattr(chunk, "end_offset", 0),
                    bbox_x0=bbox_x0,
                    bbox_y0=bbox_y0,
                    bbox_x1=bbox_x1,
                    bbox_y1=bbox_y1,
                    source_block_id=str(sbid)[:128] if sbid else None,
                    source_char_start=scs if scs is not None else None,
                    source_char_end=sce if sce is not None else None,
                )
            )

        # Step 7: Update file metadata
        file_record.processing_error = None
        file_record.processing_status = ProcessingStatus.completed
        file_record.total_chunks = len(chunks)
        file_record.total_tokens = sum(
            len(getattr(c, "text", str(c))) // 4 for c in chunks
        )
        self.db.commit()

        return {
            "file_id": file_id,
            "file_path": file_path,
            "parser_type": parser_type,
            "text_blocks": len(text_blocks),
            "chunks": len(chunks),
            "embeddings": len(chunk_embeddings),
            "vectors_stored": vectors_stored,
            "l0_tokens": len(hierarchy_result.l0) // 4 if hierarchy_result.l0 else 0,
            "l1_overview_chars": len(hierarchy_result.l1) if hierarchy_result.l1 else 0,
            "status": "completed",
        }
