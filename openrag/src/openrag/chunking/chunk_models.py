"""Chunk data models."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Chunk:
    """Represents a text chunk with complete position information."""
    text: str
    chunk_id: str  # Unique identifier
    parent_chunk_id: Optional[str] = None  # For hierarchical chunks

    # Position information
    page: int = 0
    start_offset: int = 0
    end_offset: int = 0
    bbox: Optional[tuple[float, float, float, float]] = None
    #: 与 DocumentBlock.block_id 对齐，持久化至 document_chunks.source_block_id
    source_block_id: Optional[str] = None
    #: 整文件字符流中的切片边界（与 start_offset/end_offset 在有位移时一致）
    source_char_start: Optional[int] = None
    source_char_end: Optional[int] = None

    # Hierarchy information
    level: int = 0  # Heading level
    block_type: str = "text"

    # Metadata
    metadata: dict = field(default_factory=dict)

    def to_openviking_format(self) -> dict:
        """Convert to OpenViking compatible format."""
        return {
            "text": self.text,
            "chunk_id": self.chunk_id,
            "parent_chunk_id": self.parent_chunk_id,
            "page": self.page,
            "start_offset": self.start_offset,
            "end_offset": self.end_offset,
            "bbox": self.bbox,
            "source_block_id": self.source_block_id,
            "source_char_start": self.source_char_start,
            "source_char_end": self.source_char_end,
            "level": self.level,
            "block_type": self.block_type,
            "metadata": self.metadata
        }
