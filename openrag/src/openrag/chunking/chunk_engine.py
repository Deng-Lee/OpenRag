"""Chunking engine with multiple strategies."""

from enum import Enum
import os
from typing import Optional
import uuid

from openrag.parsers.base import DocumentBlock
from openrag.chunking.chunk_models import Chunk
from openrag.chunking.document_type import DEFAULT_DOCUMENT_TYPE, normalize_document_type
from openrag.chunking.excel_table_chunker import split_excel_blocks
from openrag.parsers.char_spans import paragraph_absolute_spans
from openrag.chunking.ragflow_core.semantic import (
    chunk_semantic_ragflow,
    find_chunk_pos_robust,
)
from openrag.chunking.structure_recursive_chunker import (
    chunk_general_structured_recursive,
)

try:
    from common.token_utils import num_tokens_from_string
except ImportError:
    def num_tokens_from_string(text: str) -> int:
        return max(1, len(text) // 4)

# Backward compatibility for tests importing private helper from this module.
_find_chunk_pos_robust = find_chunk_pos_robust


class ChunkStrategy(str, Enum):
    """Chunking strategy types."""
    PARAGRAPH = "paragraph"  # Split by paragraphs
    SEMANTIC = "semantic"    # RAGFlow-like token-based merge
    FIXED_SIZE = "fixed_size"  # Fixed character length


class ChunkEngine:
    """Enhanced chunking engine with multiple strategies."""

    def __init__(self, strategy: ChunkStrategy = ChunkStrategy.SEMANTIC):
        """Initialize chunking engine.

        Args:
            strategy: Chunking strategy to use
        """
        self.strategy = strategy

    def chunk(
        self,
        text_blocks: list[DocumentBlock],
        chunk_size: int = 128,
        chunk_overlap: int = 50,
        chunk_method: str | None = None,
        min_chunk_tokens: int = 0,
        document_type: str = DEFAULT_DOCUMENT_TYPE,
    ) -> list[Chunk]:
        """Chunk text blocks using configured strategy.

        Args:
            text_blocks: List of DocumentBlock objects to chunk
            chunk_size: Maximum size of each chunk (for fixed_size strategy)
            chunk_overlap: Number of characters to overlap between chunks
            min_chunk_tokens: Minimum token count for a standalone chunk;
                chunks below this are force-merged to avoid fragments.  0 = disabled.

        Returns:
            List of Chunk objects

        Raises:
            ValueError: If invalid parameters or unknown strategy is specified
        """
        if text_blocks is None:
            raise ValueError("text_blocks cannot be None")

        if not text_blocks:
            return []

        normalized_document_type = normalize_document_type(document_type)

        if chunk_size <= 0:
            raise ValueError(f"chunk_size must be greater than 0, got {chunk_size}")

        if chunk_overlap < 0:
            raise ValueError(f"chunk_overlap must be non-negative, got {chunk_overlap}")

        if chunk_overlap >= chunk_size:
            raise ValueError(
                f"chunk_overlap ({chunk_overlap}) must be less than chunk_size ({chunk_size})"
            )

        if chunk_method == "excel_table_token_v1":
            return split_excel_blocks(text_blocks, chunk_size=chunk_size)

        if self.strategy == ChunkStrategy.PARAGRAPH:
            return self._chunk_by_paragraph(text_blocks, min_chunk_tokens)
        elif self.strategy == ChunkStrategy.SEMANTIC:
            return self._chunk_semantic(
                text_blocks,
                chunk_size,
                chunk_overlap,
                chunk_method,
                min_chunk_tokens,
                normalized_document_type,
            )
        elif self.strategy == ChunkStrategy.FIXED_SIZE:
            return self._chunk_fixed_size(text_blocks, chunk_size, chunk_overlap)
        else:
            raise ValueError(f"Unknown strategy: {self.strategy}")

    def _chunk_by_paragraph(
        self,
        text_blocks: list[DocumentBlock],
        min_chunk_tokens: int = 0,
    ) -> list[Chunk]:
        """Split by paragraph boundaries (double newline or block boundaries)."""
        chunks: list[Chunk] = []
        for block in text_blocks:
            paragraphs = block.text.split("\n\n")
            abs_spans = paragraph_absolute_spans(block.text, block.char_start)
            span_i = 0
            current_offset = block.offset

            for para in paragraphs:
                stripped_para = para.strip()
                if stripped_para:
                    cs: Optional[int] = None
                    ce: Optional[int] = None
                    if span_i < len(abs_spans):
                        cs, ce = abs_spans[span_i]
                        span_i += 1
                    so = cs if cs is not None else current_offset
                    eo = ce if ce is not None else current_offset + len(stripped_para)
                    candidate = Chunk(
                        text=stripped_para,
                        chunk_id=str(uuid.uuid4()),
                        page=block.page,
                        start_offset=so,
                        end_offset=eo,
                        bbox=block.bbox,
                        level=block.level,
                        block_type=block.block_type,
                        source_block_id=block.block_id,
                        source_char_start=cs,
                        source_char_end=ce,
                    )
                    if min_chunk_tokens > 0 and chunks:
                        prev_text_len = num_tokens_from_string(chunks[-1].text)
                        candidate_len = num_tokens_from_string(candidate.text)
                        if prev_text_len < min_chunk_tokens or candidate_len < min_chunk_tokens:
                            chunks[-1].text += "\n" + candidate.text
                            chunks[-1].end_offset = candidate.end_offset
                            chunks[-1].source_char_end = candidate.source_char_end
                            if chunks[-1].page == candidate.page:
                                if chunks[-1].bbox is not None and candidate.bbox is not None:
                                    x0 = min(chunks[-1].bbox[0], candidate.bbox[0])
                                    y0 = min(chunks[-1].bbox[1], candidate.bbox[1])
                                    x1 = max(chunks[-1].bbox[2], candidate.bbox[2])
                                    y1 = max(chunks[-1].bbox[3], candidate.bbox[3])
                                    chunks[-1].bbox = (x0, y0, x1, y1)
                            else:
                                chunks[-1].bbox = None
                            continue
                    chunks.append(candidate)
                current_offset += len(para) + 2

        return chunks

    def _chunk_semantic(
        self,
        text_blocks: list[DocumentBlock],
        chunk_size: int,
        chunk_overlap: int,
        chunk_method: str | None = None,
        min_chunk_tokens: int = 0,
        document_type: str = DEFAULT_DOCUMENT_TYPE,
    ) -> list[Chunk]:
        """Semantic chunking with RAGFlow-like naive merge (delegates to ragflow_core)."""
        structured_enabled = os.environ.get(
            "OPENRAG_GENERAL_STRUCTURE_RECURSIVE_ENABLED", "true"
        ).lower() in {"1", "true", "yes"}
        is_structured_pdf = any(
            bool((block.metadata or {}).get("structured_pdf"))
            and (
                (block.metadata or {}).get("parser_backend")
                in {"deepdoc", "paddleocr"}
                or (block.metadata or {}).get("compat_source") == "pdf"
            )
            for block in text_blocks
        )
        if (
            document_type == DEFAULT_DOCUMENT_TYPE
            and structured_enabled
            and is_structured_pdf
        ):
            return chunk_general_structured_recursive(
                text_blocks,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                min_chunk_tokens=min_chunk_tokens,
            )
        return chunk_semantic_ragflow(
            text_blocks,
            chunk_size,
            chunk_overlap,
            chunk_method,
            fixed_size_fallback=self._chunk_fixed_size,
            min_chunk_tokens=min_chunk_tokens,
            document_type=document_type,
        )

    def _chunk_fixed_size(
        self,
        text_blocks: list[DocumentBlock],
        chunk_size: int,
        chunk_overlap: int,
    ) -> list[Chunk]:
        """Fixed-size chunking with overlap."""
        chunks = []

        full_text = ""
        position_map = []

        for block_idx, block in enumerate(text_blocks):
            start_idx = len(full_text)
            full_text += block.text + " "
            base_off = (
                block.char_start if block.char_start is not None else block.offset
            )
            for i in range(len(block.text)):
                position_map.append((block_idx, base_off + i, block))
            position_map.append((block_idx, base_off + len(block.text), block))

        start = 0
        while start < len(full_text):
            end = min(start + chunk_size, len(full_text))
            chunk_text = full_text[start:end].strip()

            if chunk_text:
                if start < len(position_map):
                    block_idx, offset, block = position_map[start]

                    eo = offset + len(chunk_text)
                    chunks.append(
                        Chunk(
                            text=chunk_text,
                            chunk_id=str(uuid.uuid4()),
                            page=block.page,
                            start_offset=offset,
                            end_offset=eo,
                            bbox=block.bbox,
                            level=block.level,
                            block_type=block.block_type,
                            source_block_id=block.block_id,
                            source_char_start=offset,
                            source_char_end=eo,
                        )
                    )

            start = end - chunk_overlap if end < len(full_text) else end

        return chunks
