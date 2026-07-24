"""Read canonical document sources without mutating online content."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from openrag.chunking.chunk_models import Chunk
from openrag.hierarchy.hierarchy_storage import HierarchyStorage
from openrag.models.document_chunk import DocumentChunk
from openrag.models.file import File, ProcessingStatus
from openrag.models.workspace import Workspace
from openrag.storage.minio_storage import MinioStorage


class GenerationSourceError(RuntimeError):
    code = "GENERATION_SOURCE_UNAVAILABLE"
    retryable = True


@dataclass(frozen=True)
class GenerationSourceSnapshot:
    file_id: int
    workspace_id: int
    source_updated_at: datetime
    source_content_hash: str
    chunks: tuple[Chunk, ...]
    layers: tuple[tuple[str, str], ...]


class GenerationSourceReader:
    def __init__(
        self,
        db: Session,
        *,
        minio_storage: MinioStorage | None = None,
        hierarchy_storage: HierarchyStorage | None = None,
    ):
        self.db = db
        self.minio_storage = minio_storage
        self.hierarchy_storage = hierarchy_storage or HierarchyStorage()

    def list_active_source_files(self) -> list[File]:
        return (
            self.db.query(File)
            .filter(
                File.is_directory.is_(False),
                File.deleted_at.is_(None),
                File.processing_status == ProcessingStatus.completed,
            )
            .order_by(File.id)
            .all()
        )

    def load_chunk_sources(self, file: File, bucket: str) -> list[Chunk]:
        rows = (
            self.db.query(DocumentChunk)
            .filter(DocumentChunk.file_id == file.id)
            .order_by(DocumentChunk.chunk_index)
            .all()
        )
        if not rows:
            raise GenerationSourceError("Canonical document chunks are unavailable")
        chunks = []
        for row in rows:
            text = None
            if self.minio_storage is not None and row.object_key:
                text = self.minio_storage.get_object_text(bucket, row.object_key)
            if text is None and row.local_chunk_path:
                path = Path(row.local_chunk_path)
                if path.is_file():
                    text = path.read_text(encoding="utf-8")
            if text is None:
                raise GenerationSourceError(
                    f"Canonical content is unavailable for chunk {row.chunk_id}"
                )
            bbox = (
                (row.bbox_x0, row.bbox_y0, row.bbox_x1, row.bbox_y1)
                if None not in (row.bbox_x0, row.bbox_y0, row.bbox_x1, row.bbox_y1)
                else None
            )
            chunks.append(
                Chunk(
                    text=text,
                    chunk_id=row.chunk_id,
                    page=row.page,
                    start_offset=row.start_offset,
                    end_offset=row.end_offset,
                    bbox=bbox,
                    source_block_id=row.source_block_id,
                    source_char_start=row.source_char_start,
                    source_char_end=row.source_char_end,
                    level=row.level,
                    block_type=row.block_type,
                    metadata={
                        "page_num_int": row.page_num_int,
                        "position_int": row.position_int,
                        "top_int": row.top_int,
                    },
                )
            )
        return chunks

    def load_layer_sources(self, file: File, bucket: str) -> list[tuple[str, str]]:
        l0 = l1 = None
        if self.minio_storage is not None:
            l0 = self.minio_storage.read_hierarchy_abstract(bucket, file.uri)
            l1 = self.minio_storage.read_hierarchy_overview(bucket, file.uri)
        if l0 is None:
            l0 = self.hierarchy_storage.load_l0(file_uri=file.uri)
        if l1 is None:
            l1 = self.hierarchy_storage.load_l1(file_uri=file.uri)
        return [
            (name, text.strip())
            for name, text in (("l0", l0), ("l1", l1))
            if isinstance(text, str) and text.strip()
        ]

    @staticmethod
    def compute_source_content_hash(
        chunks: list[Chunk], layers: list[tuple[str, str]]
    ) -> str:
        canonical = json.dumps(
            {
                "chunks": [
                    {"chunk_id": chunk.chunk_id, "text": chunk.text} for chunk in chunks
                ],
                "layers": layers,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def get_source_revision(self, file_id: int) -> GenerationSourceSnapshot:
        file = self.db.get(File, file_id)
        if file is None or file.deleted_at is not None or file.is_directory:
            raise GenerationSourceError("Source file is unavailable")
        workspace = self.db.get(Workspace, file.workspace_id)
        if workspace is None:
            raise GenerationSourceError("Source workspace is unavailable")
        chunks = self.load_chunk_sources(file, workspace.slug)
        layers = self.load_layer_sources(file, workspace.slug)
        return GenerationSourceSnapshot(
            file_id=file.id,
            workspace_id=file.workspace_id,
            source_updated_at=file.updated_at,
            source_content_hash=self.compute_source_content_hash(chunks, layers),
            chunks=tuple(chunks),
            layers=tuple(layers),
        )
