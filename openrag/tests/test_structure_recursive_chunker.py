from openrag.chunking.chunk_engine import ChunkEngine, ChunkStrategy
from openrag.chunking.structure_recursive_chunker import (
    chunk_general_structured_recursive,
)
from openrag.parsers.base import DocumentBlock


def _block(
    text,
    *,
    offset,
    level=0,
    block_type="text",
    hard_boundary=False,
    page=1,
):
    metadata = {
        "compat_source": "pdf",
        "structured_pdf": True,
        "hard_boundary": hard_boundary,
    }
    if level:
        metadata["heading_role"] = "section"
        metadata["heading_confidence"] = "hard" if hard_boundary else "supported"
        metadata["heading_path"] = [text]
    return DocumentBlock(
        text=text,
        page=page,
        offset=offset,
        block_type=block_type,
        level=level,
        block_id=f"block-{offset}",
        char_start=offset,
        char_end=offset + len(text),
        metadata=metadata,
    )


def test_chunks_and_overlap_never_cross_h1(monkeypatch):
    monkeypatch.setattr(
        "openrag.chunking.structure_recursive_chunker.num_tokens_from_string",
        len,
    )
    blocks = [
        _block("H1-A", offset=0, level=1, block_type="heading", hard_boundary=True),
        _block("aaaaaa", offset=10),
        _block("bbbbbb", offset=20),
        _block(
            "H1-B",
            offset=30,
            level=1,
            block_type="heading",
            hard_boundary=True,
            page=2,
        ),
        _block("cccccc", offset=40, page=2),
    ]

    chunks = chunk_general_structured_recursive(
        blocks,
        chunk_size=12,
        chunk_overlap=4,
        min_chunk_tokens=0,
    )

    assert chunks
    assert all(not ("H1-A" in chunk.text and "H1-B" in chunk.text) for chunk in chunks)
    assert all(chunk.metadata["hard_boundary_respected"] for chunk in chunks)
    assert len({chunk.metadata["h1_region_id"] for chunk in chunks}) == 2


def test_no_h1_uses_document_root_and_recursive_separators(monkeypatch):
    monkeypatch.setattr(
        "openrag.chunking.structure_recursive_chunker.num_tokens_from_string",
        len,
    )
    blocks = [_block("第一句很长。第二句也很长。第三句结束。", offset=0)]

    chunks = chunk_general_structured_recursive(
        blocks,
        chunk_size=9,
        chunk_overlap=0,
        min_chunk_tokens=0,
    )

    assert len(chunks) >= 2
    assert all(len(chunk.text) <= 9 for chunk in chunks)
    assert all(chunk.metadata["h1_region_id"] == "document-root" for chunk in chunks)
    assert all(
        chunk.metadata["structure_quality"] == "no_trusted_h1" for chunk in chunks
    )
    assert "".join(chunk.text for chunk in chunks) == blocks[0].text


def test_h2_is_soft_and_short_sections_can_merge(monkeypatch):
    monkeypatch.setattr(
        "openrag.chunking.structure_recursive_chunker.num_tokens_from_string",
        len,
    )
    blocks = [
        _block("第一章", offset=0, level=1, block_type="heading", hard_boundary=True),
        _block("1.1", offset=10, level=2, block_type="heading"),
        _block("短文A", offset=20),
        _block("1.2", offset=30, level=2, block_type="heading"),
        _block("短文B", offset=40),
    ]

    chunks = chunk_general_structured_recursive(
        blocks,
        chunk_size=40,
        chunk_overlap=0,
        min_chunk_tokens=0,
    )

    assert len(chunks) == 1
    assert "1.1" in chunks[0].text and "1.2" in chunks[0].text


def test_chunk_engine_routes_structured_pdf_general_without_new_enum(monkeypatch):
    monkeypatch.setenv("OPENRAG_GENERAL_STRUCTURE_RECURSIVE_ENABLED", "true")
    monkeypatch.setattr(
        "openrag.chunking.structure_recursive_chunker.num_tokens_from_string",
        len,
    )
    blocks = [_block("正文", offset=0)]

    chunks = ChunkEngine(ChunkStrategy.SEMANTIC).chunk(
        blocks,
        chunk_size=20,
        chunk_overlap=0,
        document_type="general",
    )

    assert chunks[0].metadata["chunk_strategy"] == "structured_recursive_v1"


def test_chunk_engine_routes_structured_paddleocr_general(monkeypatch):
    monkeypatch.setenv("OPENRAG_GENERAL_STRUCTURE_RECURSIVE_ENABLED", "true")
    monkeypatch.setattr(
        "openrag.chunking.structure_recursive_chunker.num_tokens_from_string",
        len,
    )
    block = _block("正文", offset=0)
    block.metadata.update(
        {
            "compat_source": "paddleocr",
            "parser_backend": "paddleocr",
        }
    )

    chunks = ChunkEngine(ChunkStrategy.SEMANTIC).chunk(
        [block],
        chunk_size=20,
        chunk_overlap=0,
        document_type="general",
    )

    assert chunks[0].metadata["chunk_strategy"] == "structured_recursive_v1"


def test_structured_paddleocr_manual_keeps_profile_route(monkeypatch):
    monkeypatch.setenv("OPENRAG_GENERAL_STRUCTURE_RECURSIVE_ENABLED", "true")
    block = _block("操作步骤", offset=0)
    block.metadata.update(
        {
            "compat_source": "paddleocr",
            "parser_backend": "paddleocr",
        }
    )

    chunks = ChunkEngine(ChunkStrategy.SEMANTIC).chunk(
        [block],
        chunk_size=20,
        chunk_overlap=0,
        document_type="manual",
    )

    assert chunks
    assert all(
        chunk.metadata.get("chunk_strategy") != "structured_recursive_v1"
        for chunk in chunks
    )
