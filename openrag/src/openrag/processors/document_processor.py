"""Document processor orchestrating the complete processing pipeline."""

import logging
import math
import time
from typing import TYPE_CHECKING, Any, Callable, Optional

from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from openrag.search.es_chunk_store import EsChunkStore
    from openrag.vectorstore.milvus_layer_store import MilvusLayerStore

from openrag.chunking.chunk_engine import ChunkEngine
from openrag.chunking.document_type import DEFAULT_DOCUMENT_TYPE, normalize_document_type
from openrag.chunking.chunk_params import chunk_size_overlap_from_env, min_chunk_tokens_from_env, resolve_chunk_method
from openrag.embedding.embedding_engine import EmbeddingEngine
from openrag.hierarchy.document_hierarchy_builder import DocumentHierarchyBuilder
from openrag.hierarchy.hierarchy_storage import HierarchyStorage
from openrag.models.document_chunk import DocumentChunk
from openrag.models.file import File, ProcessingStatus
from openrag.models.workspace import Workspace
from openrag.parsers.parser_registry import ParserRegistry
from openrag.services.canonical_chunk_source import (
    build_canonical_chunk_source,
    canonical_source_metadata,
    supports_canonical_chunk_source,
)
from openrag.services.parse_artifact_service import ParseArtifactService
from openrag.services.trace_service import TraceService
from openrag.storage.minio_storage import MinioStorage, chunk_object_key
from ..vectorstore.errors import VectorWriteIncompleteError

logger = logging.getLogger(__name__)

_TEXT_PREVIEW_MAX = 16000

try:
    from common.token_utils import num_tokens_from_string
except ImportError:
    def num_tokens_from_string(text: str) -> int:
        return max(1, len(text) // 4)


def _parser_name(parser) -> str:
    return parser.__class__.__name__


def _parser_version(parser) -> str:
    return str(
        getattr(parser, "parser_version", None)
        or getattr(parser, "version", None)
        or getattr(parser, "__version__", None)
        or "unknown"
    )


def _safe_start_span(
    trace_service: TraceService,
    stage: str,
    *,
    input_summary: Optional[dict] = None,
):
    try:
        return trace_service.start_span(stage, input_summary=input_summary)
    except Exception:
        return None


def _safe_finish_span(
    trace_service: TraceService,
    span,
    *,
    output_summary: Optional[dict] = None,
    metrics: Optional[dict] = None,
    artifact_refs: Optional[dict] = None,
) -> None:
    if span is None:
        return
    try:
        trace_service.finish_span(
            span_id=span.span_id,
            output_summary=output_summary,
            metrics=metrics,
            artifact_refs=artifact_refs,
        )
    except Exception:
        pass


def _safe_fail_span(
    trace_service: TraceService,
    span,
    error_message: str,
    *,
    metrics: Optional[dict] = None,
    output_summary: Optional[dict] = None,
) -> None:
    if span is None:
        return
    try:
        trace_service.fail_span(
            span_id=span.span_id,
            error_message=error_message,
            metrics=metrics,
            output_summary=output_summary,
        )
    except Exception:
        pass


def _safe_fail_run(
    trace_service: TraceService,
    *,
    error_stage: str,
    error_message: str,
) -> None:
    try:
        trace_service.fail_run(error_stage=error_stage, error_message=error_message)
    except Exception:
        pass


def _chunk_token_stats(chunks) -> dict:
    token_counts = [num_tokens_from_string(getattr(chunk, "text", str(chunk)) or "") for chunk in chunks]
    if not token_counts:
        return {
            "token_min": 0,
            "token_max": 0,
            "token_mean": 0.0,
        }
    return {
        "token_min": min(token_counts),
        "token_max": max(token_counts),
        "token_mean": round(sum(token_counts) / len(token_counts), 2),
    }


def _coerce_int_list(value: Any) -> Optional[list[int]]:
    if value is None or not isinstance(value, (list, tuple)):
        return None
    try:
        return [int(item) for item in value]
    except (TypeError, ValueError):
        return None


def _coerce_position_int(value: Any) -> Optional[list[list[int]]]:
    if value is None or not isinstance(value, (list, tuple)):
        return None
    positions: list[list[int]] = []
    try:
        for item in value:
            if not isinstance(item, (list, tuple)) or len(item) != 5:
                return None
            positions.append([int(part) for part in item])
    except (TypeError, ValueError):
        return None
    return positions


def _extract_chunk_position_fields(chunk) -> dict:
    metadata = getattr(chunk, "metadata", None) or {}
    if not isinstance(metadata, dict):
        metadata = {}
    return {
        "page_num_int": _coerce_int_list(metadata.get("page_num_int")),
        "position_int": _coerce_position_int(metadata.get("position_int")),
        "top_int": _coerce_int_list(metadata.get("top_int")),
    }


def _parse_span_profile(parser) -> Optional[dict]:
    """Clean per-stage timing subset to attach to the parse.document span.

    Reads ``parser.last_parse_profile`` (set by PDFParserAdapter for PDFs;
    absent for other parsers). Returns None when there is no usable profile,
    so the span's output_summary only gains the field for instrumented PDFs.
    """
    profile = getattr(parser, "last_parse_profile", None)
    if not isinstance(profile, dict):
        return None
    stages = profile.get("stages")
    if not stages:
        return None
    return {
        "total_ms": profile.get("total_ms"),
        "page_count": profile.get("page_count"),
        "n_tables": profile.get("n_tables"),
        "status": profile.get("status"),
        "stages": stages,
    }


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
        require_layer_vectors: bool = False,
    ):
        if vector_store is None:
            raise VectorWriteIncompleteError(
                "Milvus chunk vector store is required for document processing"
            )
        if require_layer_vectors and layer_store is None:
            raise VectorWriteIncompleteError(
                "Milvus layer vector store is required when L0/L1 retrieval is enabled"
            )
        self.db = db
        self.parser_registry = parser_registry
        self.chunk_engine = chunk_engine
        self.embedding_engine = embedding_engine
        self.hierarchy_storage = hierarchy_storage or HierarchyStorage()
        self.minio_storage = minio_storage
        self.vector_store = vector_store
        self.layer_store = layer_store
        self.require_layer_vectors = require_layer_vectors
        self.chunk_fulltext_store = chunk_fulltext_store
        self.hierarchy_builder = DocumentHierarchyBuilder(
            l0_max_tokens=l0_max_tokens,
            l1_max_tokens=l1_max_tokens,
            l1_section_preview_tokens=l1_section_preview_tokens,
        )

    @staticmethod
    def _validate_chunks(chunks: list) -> None:
        if not chunks:
            raise VectorWriteIncompleteError("Document processing produced no chunks")
        chunk_ids: list[str] = []
        for chunk in chunks:
            chunk_id = str(getattr(chunk, "chunk_id", "") or "").strip()
            text = getattr(chunk, "text", None)
            if not chunk_id:
                raise VectorWriteIncompleteError("Every document chunk must have a chunk ID")
            if not isinstance(text, str) or not text.strip():
                raise VectorWriteIncompleteError("Every document chunk must contain text")
            chunk_ids.append(chunk_id[:64])
        if len(set(chunk_ids)) != len(chunk_ids):
            raise VectorWriteIncompleteError("Document chunk IDs must be unique")

    def _build_layer_embeddings(self, hierarchy_result) -> list[tuple[str, str, list[float]]]:
        if not self.require_layer_vectors:
            return []
        layer_texts = [
            (layer, text.strip())
            for layer, text in (("l0", hierarchy_result.l0), ("l1", hierarchy_result.l1))
            if isinstance(text, str) and text.strip()
        ]
        if not layer_texts:
            raise VectorWriteIncompleteError("L0/L1 retrieval requires non-empty layer text")
        vectors = self.embedding_engine.embed_batch([text for _, text in layer_texts])
        if len(vectors) != len(layer_texts):
            raise VectorWriteIncompleteError(
                "Layer embedding count does not match non-empty layer count"
            )
        return [
            (layer, text, vector)
            for (layer, text), vector in zip(layer_texts, vectors)
        ]

    @staticmethod
    def _assert_processing_complete(
        *,
        chunk_count: int,
        chunk_embedding_count: int,
        milvus_chunk_insert_count: int,
        document_chunk_metadata_count: int,
        expected_layer_count: int,
        milvus_layer_insert_count: int,
    ) -> None:
        counts = {
            "chunk_count": chunk_count,
            "chunk_embedding_count": chunk_embedding_count,
            "milvus_chunk_insert_count": milvus_chunk_insert_count,
            "document_chunk_metadata_count": document_chunk_metadata_count,
        }
        if chunk_count <= 0 or any(value != chunk_count for value in counts.values()):
            raise VectorWriteIncompleteError(
                "Document chunk, embedding, Milvus and metadata counts must match"
            )
        if milvus_layer_insert_count != expected_layer_count:
            raise VectorWriteIncompleteError(
                "Layer embedding and Milvus insert counts must match"
            )

    def process_document(
        self,
        file_path: str,
        file_id: int,
        user_id: int,
        parser_type: str = "auto",
        document_type: str = DEFAULT_DOCUMENT_TYPE,
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
        normalized_document_type = normalize_document_type(document_type)
        file_record.document_type = normalized_document_type
        trace_service = TraceService(self.db)

        # Step 1: Parse（策略仅在 ParserRegistry / Factory；此处只编排）
        file_record.processing_status = ProcessingStatus.parsing
        self.db.commit()
        parser = self.parser_registry.get_parser(file_path, parser_type)
        print(
            "  [PIPELINE] Step 1 — "
            f"selected_parser={parser.__class__.__module__}.{parser.__class__.__name__}, "
            f"parser_type={parser_type}, file_path={file_path}"
        )
        parse_span = _safe_start_span(
            trace_service,
            "parse.document",
            input_summary={
                "file_id": file_id,
                "parser_type": parser_type,
                "parser_name": _parser_name(parser),
                "parser_version": _parser_version(parser),
            },
        )
        parse_started = time.perf_counter()
        try:
            text_blocks = parser.parse(file_path)
        except Exception as exc:
            fail_profile = _parse_span_profile(parser)
            _safe_fail_span(
                trace_service,
                parse_span,
                str(exc),
                metrics={"duration_ms": int((time.perf_counter() - parse_started) * 1000)},
                output_summary=(
                    {"pdf_stage_profile": fail_profile} if fail_profile else None
                ),
            )
            _safe_fail_run(
                trace_service,
                error_stage="parse.document",
                error_message=str(exc),
            )
            raise
        canonical_text_override = None
        canonical_source = None
        if supports_canonical_chunk_source(_parser_name(parser)):
            canonical_result = build_canonical_chunk_source(text_blocks)
            text_blocks = canonical_result.blocks
            canonical_text_override = canonical_result.text
            canonical_source = canonical_source_metadata()
        parse_duration_ms = int((time.perf_counter() - parse_started) * 1000)
        parse_output_summary = {
            "parser_name": _parser_name(parser),
            "parser_version": _parser_version(parser),
            "block_count": len(text_blocks),
            "page_count": len(
                {
                    getattr(block, "page", None)
                    for block in text_blocks
                    if getattr(block, "page", None) is not None
                }
            ),
            "duration_ms": parse_duration_ms,
        }
        stage_profile = _parse_span_profile(parser)
        if stage_profile:
            parse_output_summary["pdf_stage_profile"] = stage_profile
        _safe_finish_span(
            trace_service,
            parse_span,
            output_summary=parse_output_summary,
            metrics={"duration_ms": parse_duration_ms},
        )
        print(f"  [PIPELINE] Step 1 — Parsed {len(text_blocks)} text blocks")
        for i, b in enumerate(text_blocks[:3]):
            print(f"    block[{i}]: level={b.level}, type={b.block_type}, text={b.text[:80]!r}")
        if progress_callback:
            progress_callback(20)

        workspace = (
            self.db.query(Workspace)
            .filter(Workspace.id == file_record.workspace_id)
            .first()
        )
        bucket = workspace.slug if workspace else None
        try:
            with open(file_path, "rb") as fh:
                source_doc_bytes = fh.read()
        except Exception as exc:
            logger.warning("Failed to read source bytes for parse artifact: %s", exc)
            source_doc_bytes = b""

        persist_span = _safe_start_span(
            trace_service,
            "parsed_artifacts.persist",
            input_summary={
                "file_id": file_id,
                "bucket": bucket,
                "parser_name": _parser_name(parser),
                "parser_version": _parser_version(parser),
            },
        )
        if self.minio_storage and bucket:
            try:
                artifact_result = ParseArtifactService(
                    self.db,
                    self.minio_storage,
                ).persist_parse_artifacts(
                    workspace_id=file_record.workspace_id,
                    file_id=file_id,
                    bucket_name=bucket,
                    file_uri=file_record.uri,
                    source_doc_bytes=source_doc_bytes,
                    blocks=text_blocks,
                    parser_name=_parser_name(parser),
                    parser_version=_parser_version(parser),
                    canonical_text_override=canonical_text_override,
                    canonical_source=canonical_source,
                )
                _safe_finish_span(
                    trace_service,
                    persist_span,
                    output_summary={
                        "canonical_json_object_key": artifact_result.canonical_json_object_key,
                        "canonical_md_object_key": artifact_result.canonical_md_object_key,
                        "canonical_text_hash": artifact_result.canonical_text_hash,
                        "block_count": artifact_result.block_count,
                        "page_count": artifact_result.page_count,
                        "size_bytes": (
                            artifact_result.canonical_json_size_bytes
                            + artifact_result.canonical_md_size_bytes
                        ),
                        "status": artifact_result.status,
                    },
                )
            except Exception as exc:
                _safe_fail_span(trace_service, persist_span, str(exc))
                raise
        else:
            _safe_finish_span(
                trace_service,
                persist_span,
                output_summary={
                    "status": "skipped",
                    "skip_reason": "minio_or_bucket_unavailable",
                    "block_count": len(text_blocks),
                    "page_count": len(
                        {
                            getattr(block, "page", None)
                            for block in text_blocks
                            if getattr(block, "page", None) is not None
                        }
                    ),
                    "size_bytes": 0,
                },
            )

        # Step 2: Chunk（chunk_method / size 由 chunk_params + env 决定，不在此写类型分支）
        chunk_size, chunk_overlap = chunk_size_overlap_from_env()
        chunk_method = resolve_chunk_method(file_path, parser_type)
        min_chunk_tokens = min_chunk_tokens_from_env()
        chunk_span = _safe_start_span(
            trace_service,
            "chunk.build",
            input_summary={
                "chunk_method": chunk_method,
                "document_type": normalized_document_type,
                "chunk_size": chunk_size,
                "overlap": chunk_overlap,
                "min_chunk_tokens": min_chunk_tokens,
                "block_count": len(text_blocks),
            },
        )
        try:
            try:
                chunks = self.chunk_engine.chunk(
                    text_blocks,
                    chunk_size=chunk_size,
                    chunk_overlap=chunk_overlap,
                    chunk_method=chunk_method,
                    min_chunk_tokens=min_chunk_tokens,
                    document_type=normalized_document_type,
                )
            except TypeError as exc:
                if "document_type" not in str(exc):
                    raise
                chunks = self.chunk_engine.chunk(
                    text_blocks,
                    chunk_size=chunk_size,
                    chunk_overlap=chunk_overlap,
                    chunk_method=chunk_method,
                    min_chunk_tokens=min_chunk_tokens,
                )
        except Exception as exc:
            _safe_fail_span(trace_service, chunk_span, str(exc))
            raise
        _safe_finish_span(
            trace_service,
            chunk_span,
            output_summary={
                "chunk_method": chunk_method,
                "chunk_size": chunk_size,
                "overlap": chunk_overlap,
                "min_chunk_tokens": min_chunk_tokens,
                "chunk_count": len(chunks),
                **_chunk_token_stats(chunks),
                "short_chunk_count": (
                    sum(
                        1
                        for chunk in chunks
                        if num_tokens_from_string(getattr(chunk, "text", str(chunk)) or "")
                        < min_chunk_tokens
                    )
                    if min_chunk_tokens > 0
                    else 0
                ),
                "empty_chunk_count": sum(
                    1
                    for chunk in chunks
                    if not (getattr(chunk, "text", str(chunk)) or "").strip()
                ),
            },
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
        self._validate_chunks(chunks)
        file_record.processing_status = ProcessingStatus.embedding
        self.db.commit()
        embedding_span = _safe_start_span(
            trace_service,
            "embedding.chunks",
            input_summary={
                "embedding_model": getattr(self.embedding_engine, "model_name", None)
                or self.embedding_engine.__class__.__name__,
                "chunk_count": len(chunks),
            },
        )
        try:
            chunk_embeddings = self.embedding_engine.embed_chunks(chunks)
            if len(chunk_embeddings) != len(chunks):
                raise VectorWriteIncompleteError(
                    "Chunk embedding count does not match chunk count"
                )
            layer_embeddings = self._build_layer_embeddings(hierarchy_result)
        except Exception as exc:
            _safe_fail_span(
                trace_service,
                embedding_span,
                str(exc),
                metrics={
                    "chunk_count": len(chunks),
                    "success_count": 0,
                    "failure_count": len(chunks),
                },
            )
            raise
        _safe_finish_span(
            trace_service,
            embedding_span,
            output_summary={
                "embedding_model": getattr(self.embedding_engine, "model_name", None)
                or self.embedding_engine.__class__.__name__,
                "dimension": getattr(self.embedding_engine, "dimension", None),
                "batch_size": len(chunks),
                "batch_count": math.ceil(
                    len(chunks) / max(1, getattr(self.embedding_engine, "batch_size", len(chunks)))
                ),
                "chunk_count": len(chunks),
                "success_count": len(chunk_embeddings),
                "failure_count": max(0, len(chunks) - len(chunk_embeddings)),
            },
        )
        print(f"  [PIPELINE] Step 4 — Generated {len(chunk_embeddings)} embeddings")
        if progress_callback:
            progress_callback(65)

        # Step 5: Store vectors in Milvus
        vectors_stored = 0
        vector_span = _safe_start_span(
            trace_service,
            "vector.milvus_insert",
            input_summary={
                "chunk_count": len(chunk_embeddings),
                "collection": (
                    getattr(self.vector_store, "collection_name", None)
                    if self.vector_store is not None
                    else None
                ),
            },
        )
        try:
            self.vector_store.delete_by_file_id(file_id)
            vectors_stored = self.vector_store.insert_chunks(file_id, chunk_embeddings)
            if vectors_stored != len(chunk_embeddings):
                raise VectorWriteIncompleteError(
                    "Milvus chunk insert count does not match embedding count"
                )
        except Exception as exc:
            _safe_fail_span(
                trace_service,
                vector_span,
                str(exc),
                metrics={"insert_count": vectors_stored},
            )
            raise
        print(f"  [PIPELINE] Step 5 — Stored {vectors_stored} vectors in Milvus")
        _safe_finish_span(
            trace_service,
            vector_span,
            output_summary={
                "insert_count": vectors_stored,
                "collection": (
                    getattr(self.vector_store, "collection_name", None)
                    if self.vector_store is not None
                    else None
                ),
                "failure_reason": None,
            },
        )

        # Step 5a: Elasticsearch 全文（与 Milvus 同一批 chunk；失败仅告警）
        es_span = _safe_start_span(
            trace_service,
            "fulltext.es_index",
            input_summary={
                "chunk_count": len(chunk_embeddings),
                "enabled": (
                    vectors_stored > 0
                    and self.chunk_fulltext_store is not None
                    and self.vector_store is not None
                ),
            },
        )
        es_index_name = None
        es_doc_count = 0
        es_upsert_count = 0
        es_failure_reason = None
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
                    es_index_name = index_name
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
                    es_doc_count = len(es_docs)
                    es_upsert_count = n_es
                    print(f"  [PIPELINE] Step 5a — Indexed {n_es} chunks in Elasticsearch")
                except Exception as exc:
                    es_failure_reason = str(exc)
                    logger.warning("Elasticsearch chunk index failed: %s", exc)
            else:
                es_failure_reason = "workspace_not_found"
        else:
            es_failure_reason = (
                "not_enabled_or_no_vectors"
                if self.chunk_fulltext_store is None or self.vector_store is None
                else None
            )
        _safe_finish_span(
            trace_service,
            es_span,
            output_summary={
                "index_name": es_index_name,
                "doc_count": es_doc_count,
                "upsert_count": es_upsert_count,
                "failure_reason": es_failure_reason,
            },
        )

        # Step 5b: L0/L1 向量写入 Milvus（与 openrag_layers 集合对齐）
        n_layers = 0
        if self.require_layer_vectors:
            n_layers = self.layer_store.upsert_file_layers(file_id, layer_embeddings)
            if n_layers != len(layer_embeddings):
                raise VectorWriteIncompleteError(
                    "Milvus layer insert count does not match embedding count"
                )
            print(f"  [PIPELINE] Step 5b — Stored {n_layers} L0/L1 layer vectors")
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
        storage_span = _safe_start_span(
            trace_service,
            "storage.save_chunks",
            input_summary={
                "file_id": file_id,
                "bucket": bucket,
                "chunk_count": len(chunks),
                "storage_backend": "minio" if self.minio_storage and bucket else "local",
            },
        )
        if self.minio_storage and bucket:
            try:
                urls = self.minio_storage.put_document_hierarchy(
                    bucket,
                    file_record.uri,
                    hierarchy_result.l0,
                    hierarchy_result.l1,
                    hierarchy_result.l2,
                )
            except Exception as exc:
                _safe_fail_span(trace_service, storage_span, str(exc))
                raise
            file_record.l0_path = urls.get("l0_url") or None
            file_record.l1_path = urls.get("l1_url") or None
            file_record.l2_path = urls.get("l2_url") or None
        else:
            try:
                self.hierarchy_storage.save_document_hierarchy(
                    file_uri=file_record.uri,
                    l0=hierarchy_result.l0,
                    l1=hierarchy_result.l1,
                    l2=hierarchy_result.l2,
                )
            except Exception as exc:
                _safe_fail_span(trace_service, storage_span, str(exc))
                raise
            file_record.l0_path = self.hierarchy_storage.get_l0_path(file_record.uri)
            file_record.l1_path = self.hierarchy_storage.get_l1_path(file_record.uri)
            file_record.l2_path = self.hierarchy_storage.get_l2_path(file_record.uri)
        _safe_finish_span(
            trace_service,
            storage_span,
            output_summary={
                "storage_backend": "minio" if self.minio_storage and bucket else "local",
                "chunk_count": len(chunks),
                "l0_saved": bool(file_record.l0_path),
                "l1_saved": bool(file_record.l1_path),
                "l2_saved": bool(file_record.l2_path),
            },
        )
        if progress_callback:
            progress_callback(90)

        # Step 6b: 持久化切片元数据（与 Milvus chunk_id、MinIO object_key 对齐）
        metadata_span = _safe_start_span(
            trace_service,
            "metadata.persist_chunks",
            input_summary={
                "file_id": file_id,
                "chunk_count": len(chunks),
            },
        )
        self.db.query(DocumentChunk).filter(DocumentChunk.file_id == file_id).delete(
            synchronize_session=False
        )
        document_chunks_written = 0
        for i, chunk in enumerate(chunks):
            cid = str(getattr(chunk, "chunk_id", "") or "")[:64]
            if not cid:
                raise VectorWriteIncompleteError(
                    f"Document chunk at index {i} has no chunk ID"
                )
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
            chunk_kwargs = {}
            try:
                if self.db.get_bind().dialect.name == "sqlite":
                    chunk_kwargs["id"] = file_id * 1_000_000 + i
            except Exception:
                pass
            self.db.add(
                DocumentChunk(
                    **chunk_kwargs,
                    **_extract_chunk_position_fields(chunk),
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
            document_chunks_written += 1

        self.db.flush()
        document_chunk_metadata_count = (
            self.db.query(DocumentChunk)
            .filter(DocumentChunk.file_id == file_id)
            .count()
        )
        self._assert_processing_complete(
            chunk_count=len(chunks),
            chunk_embedding_count=len(chunk_embeddings),
            milvus_chunk_insert_count=vectors_stored,
            document_chunk_metadata_count=document_chunk_metadata_count,
            expected_layer_count=len(layer_embeddings),
            milvus_layer_insert_count=n_layers,
        )

        # Step 7: Update file metadata
        file_record.processing_error = None
        file_record.processing_status = ProcessingStatus.completed
        file_record.total_chunks = len(chunks)
        file_record.total_tokens = sum(
            len(getattr(c, "text", str(c))) // 4 for c in chunks
        )
        self.db.commit()
        _safe_finish_span(
            trace_service,
            metadata_span,
            output_summary={
                "document_chunks_written": document_chunks_written,
                "chunk_count": len(chunks),
                "milvus_insert_count": vectors_stored,
                "es_doc_count": es_doc_count,
            },
        )

        return {
            "file_id": file_id,
            "file_path": file_path,
            "parser_type": parser_type,
            "document_type": normalized_document_type,
            "text_blocks": len(text_blocks),
            "chunks": len(chunks),
            "embeddings": len(chunk_embeddings),
            "vectors_stored": vectors_stored,
            "l0_tokens": len(hierarchy_result.l0) // 4 if hierarchy_result.l0 else 0,
            "l1_overview_chars": len(hierarchy_result.l1) if hierarchy_result.l1 else 0,
            "status": "completed",
        }
