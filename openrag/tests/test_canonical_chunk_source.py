from openrag.parsers.base import DocumentBlock
from openrag.services.canonical_chunk_source import (
    canonical_source_metadata,
    build_canonical_chunk_source,
    supports_canonical_chunk_source,
)


def test_build_canonical_chunk_source_rewrites_offsets_without_reordering_blocks():
    blocks = [
        DocumentBlock(
            text="  Title  ",
            page=1,
            offset=100,
            block_type="heading",
            level=1,
            block_id="md:para:0",
            char_start=10,
            char_end=19,
        ),
        DocumentBlock(
            text="\nBody paragraph\n",
            page=1,
            offset=200,
            block_type="text",
            block_id="md:para:1",
            char_start=30,
            char_end=46,
        ),
        DocumentBlock(
            text=" | A | B | ",
            page=1,
            offset=300,
            block_type="table",
            table_data={"rows": [["A", "B"]]},
            block_id="md:table:0",
            char_start=90,
            char_end=101,
        ),
    ]

    result = build_canonical_chunk_source(blocks)

    assert result.text == "Title\nBody paragraph\n| A | B |"
    assert [block.block_id for block in result.blocks] == [
        "md:para:0",
        "md:para:1",
        "md:table:0",
    ]
    assert [
        result.text[block.char_start : block.char_end]
        for block in result.blocks
    ] == ["Title", "Body paragraph", "| A | B |"]
    assert [block.offset for block in result.blocks] == [0, 6, 21]
    assert result.blocks[2].block_type == "table"
    assert result.blocks[2].table_data == {"rows": [["A", "B"]]}


def test_build_canonical_chunk_source_skips_empty_blocks():
    blocks = [
        DocumentBlock(text="A", page=1, offset=0),
        DocumentBlock(text=" \n\t ", page=1, offset=1),
        DocumentBlock(text="B", page=1, offset=2),
    ]

    result = build_canonical_chunk_source(blocks)

    assert result.text == "A\nB"
    assert [block.text for block in result.blocks] == ["A", "B"]
    assert [(block.char_start, block.char_end) for block in result.blocks] == [
        (0, 1),
        (2, 3),
    ]


def test_supports_canonical_chunk_source_only_for_text_like_parsers():
    assert supports_canonical_chunk_source("MarkdownParserAdapter")
    assert supports_canonical_chunk_source("TxtParserAdapter")
    assert supports_canonical_chunk_source("DocxParserAdapter")
    assert not supports_canonical_chunk_source("PDFParserAdapter")


def test_canonical_source_metadata_documents_rule_version():
    metadata = canonical_source_metadata()

    assert metadata["version"] == "chunk_source_v1"
    assert metadata["block_text"] == "strip"
    assert metadata["joiner"] == "\\n"
    assert metadata["preserve_parser_block_order"] is True
    assert metadata["preserve_parser_table_position"] is True
    assert metadata["trailing_newline"] is False
