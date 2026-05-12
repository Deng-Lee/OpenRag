"""字符流偏移与段落切片。"""
from openrag.chunking.chunk_engine import ChunkEngine, ChunkStrategy
from openrag.parsers.base import DocumentBlock
from openrag.parsers.char_spans import paragraph_absolute_spans, paragraph_blocks_from_plaintext


def test_paragraph_blocks_from_plaintext():
    raw = "aa\n\nbb\n\ncc"
    blocks = paragraph_blocks_from_plaintext(raw, id_prefix="t")
    assert len(blocks) == 3
    assert blocks[0].text == "aa" and blocks[0].char_start == 0 and blocks[0].char_end == 2
    assert blocks[1].char_start == 4 and blocks[1].char_end == 6
    assert blocks[0].block_id == "t:p:0"


def test_paragraph_absolute_spans_with_char_start():
    block = DocumentBlock(
        text="x\n\nyz",
        page=1,
        offset=0,
        char_start=100,
    )
    spans = paragraph_absolute_spans(block.text, block.char_start)
    assert spans == [(100, 101), (103, 105)]


def test_chunk_engine_propagates_source_block_id():
    eng = ChunkEngine(ChunkStrategy.PARAGRAPH)
    blocks = [
        DocumentBlock(
            text="a\n\nb",
            page=1,
            offset=0,
            block_id="blk:1",
            char_start=10,
        )
    ]
    chunks = eng.chunk(blocks)
    assert len(chunks) == 2
    assert chunks[0].source_block_id == "blk:1"
    assert chunks[0].source_char_start == 10
    assert chunks[0].source_char_end == 11
    assert chunks[1].source_char_start == 13
    assert chunks[1].source_char_end == 14
