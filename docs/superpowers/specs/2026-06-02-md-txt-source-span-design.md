# MD/TXT Source Span Mapping Design

## Status

Design for review. No implementation has been applied in this spec.

## Goal

Make Markdown and plain text chunks map accurately back to the uploaded original document content.

For MD/TXT files, every parser-produced `DocumentBlock` with position metadata must satisfy:

```python
block.text == raw_text[block.char_start:block.char_end]
```

Here `raw_text` means the user-uploaded file decoded into the same text coordinate system used by source preview. The coordinate system is Python string character index after UTF-8 decoding, not byte offset and not rendered Markdown DOM position.

## Current Problem

TXT currently reads the uploaded file and splits it into paragraph blocks, so its position model is close to the uploaded source. The main mismatch risk is that the parser uses `errors="ignore"`, while preview uses replacement on invalid UTF-8. If invalid bytes exist, parser and preview can produce different character streams.

Markdown currently reads the uploaded source but then calls `RAGFlowMarkdownParser.extract_tables_and_remainder(md_text)`. It splits the processed `remainder`, strips each paragraph, and uses `md_text.find(...)` to locate the processed text back in the original source. Tables are extracted separately and appended after normal paragraphs. This causes several mapping risks:

- processed text can differ from uploaded source text;
- table order can differ from source order;
- `strip()` changes block boundaries;
- repeated text can match the wrong occurrence;
- `find()` fallback can create approximate positions.

## Scope

In scope:

- MD parser source-span accuracy.
- TXT parser source-span accuracy.
- Parser-level helper functions for source text decoding, line spans, trimming, and block span creation.
- Focused backend tests for parser spans and chunk propagation.

Out of scope:

- DOC/DOCX mapping fixes.
- PDF position mapping changes.
- Frontend rendering changes.
- Search result navigation changes.
- New external parser dependencies.
- Mapping to rendered Markdown visual positions.

## Recommended Approach

Use a source scanner in the parser layer.

The parser should scan the uploaded decoded source directly and produce `DocumentBlock` objects whose text is a direct slice of that source. The parser must not generate block positions by searching processed text back into the original document.

This keeps the mapping simple and verifiable: if the block text is always a direct source slice, chunk positions derived from the block spans remain in the uploaded source coordinate system.

## Alternative Approaches Considered

### AST Parser With Source Ranges

Use a Markdown parser such as tree-sitter-markdown that exposes source ranges.

This can produce more semantically precise Markdown blocks, but it adds dependency and build risk, especially on Windows. It also returns byte ranges, requiring careful byte-to-character conversion. This is not recommended for the first fix.

### Processed Text Alignment

Keep the RAGFlow Markdown output and align it back to the source using diff or fuzzy matching.

This preserves existing parser output, but repeated text, stripped whitespace, reordered tables, and normalized syntax make the mapping inherently fragile. This is not recommended for source-accurate highlighting.

## Design

### Source Text Helper

Add a small helper module, proposed path:

```text
openrag/src/openrag/parsers/source_spans.py
```

Responsibilities:

- Read uploaded text bytes.
- Decode with UTF-8. If strict decoding fails, decode with `errors="replace"` so parser coordinates match preview behavior.
- Build line spans using `splitlines(keepends=True)` so every line has a stable `[start, end)` range in the decoded source.
- Trim only block outer whitespace when needed, while preserving the invariant that `text == raw[start:end]`.

Expected helper concepts:

- `read_source_text(file_path: str) -> str`
- `LineSpan(text: str, start: int, end: int)`
- `iter_line_spans(raw: str) -> list[LineSpan]`
- `trim_span(raw: str, start: int, end: int) -> tuple[int, int]`
- `iter_plain_paragraph_spans(raw: str) -> Iterator[tuple[int, int]]`

The helper should not know about Markdown semantics. Markdown block detection stays in the Markdown adapter.

### TXT Parser

Modify:

```text
openrag/src/openrag/parsers/adapters/txt_adapter.py
```

Behavior:

- Use `read_source_text(file_path)`.
- Split plain text into paragraph spans using `iter_plain_paragraph_spans(raw)`.
- For each paragraph span, create a `DocumentBlock` where:
  - `text = raw[start:end]`
  - `char_start = start`
  - `char_end = end`
  - `block_type = "text"`
  - `layout_type = "text"`
  - `block_id = "txt:p:{idx}"`

Paragraph splitting should treat one or more blank lines as paragraph separators. CRLF and LF inputs must both produce source offsets against the original decoded string.

### Markdown Parser

Modify:

```text
openrag/src/openrag/parsers/adapters/markdown_adapter.py
```

Behavior:

- Use `read_source_text(file_path)`.
- Stop using `extract_tables_and_remainder()` for MD block construction.
- Scan the original Markdown source line by line.
- Emit blocks in source order.
- Every emitted block must use `raw[start:end]` as its text.

Markdown block rules for the first implementation:

- Blank lines separate normal paragraph/list blocks.
- ATX headings (`#` through `######` followed by whitespace) become one heading block.
- Fenced code blocks starting with triple backticks or tildes become one `code` block from opening fence through closing fence if present; if not closed, the block runs to EOF.
- GFM-style pipe tables become one `table` block when a header line is followed by a separator line such as `| --- | --- |`; subsequent pipe-like rows remain in the same table block.
- Other consecutive nonblank lines become a `text` block.

Heading level:

- ATX heading level is the number of leading `#` characters.
- Other blocks use level `0`.

Block metadata:

- `page = 1` for MD/TXT.
- `offset` remains the sequential parser block index for compatibility.
- `block_id` uses stable parser-local IDs:
  - `md:heading:{idx}` for headings
  - `md:p:{idx}` for text/list paragraphs
  - `md:table:{idx}` for tables
  - `md:code:{idx}` for fenced code

### Chunk Propagation

The existing chunking path should continue to consume `DocumentBlock.char_start/char_end`.

For a chunk that covers one parser block, the chunk source span should stay inside that block span.

For a chunk that merges multiple adjacent parser blocks, the chunk source span may cover from the first source block start to the last source block end. This can include blank lines or Markdown syntax between blocks. That is acceptable because the target coordinate system is uploaded source text.

The implementation should avoid broad changes to chunking unless tests show the existing propagation violates the source-span invariant for MD/TXT. If a chunking change is required, keep it limited to using existing `source_blocks` or covered block spans instead of text search fallback.

## Data Flow

1. Upload stores original MD/TXT bytes in MinIO.
2. Worker downloads the original file to a temporary local path.
3. Parser reads and decodes the original file into `raw_text`.
4. Parser scans `raw_text` and emits source-sliced `DocumentBlock` objects.
5. Chunk engine builds chunks and propagates source spans.
6. `DocumentProcessor` writes `source_char_start/source_char_end` into `document_chunks`.
7. Search and document chunk detail APIs return those fields unchanged.
8. Frontend source preview highlights `raw_text[source_char_start:source_char_end]`.

## Error Handling

- Invalid UTF-8 should not fail parsing by default. Parser should decode with replacement if strict UTF-8 fails, matching preview behavior.
- Empty files should return an empty block list.
- Markdown fenced code blocks without a closing fence should produce one code block through EOF.
- Malformed table-like lines should fall back to normal text blocks unless a valid GFM separator line is present.

## Testing Plan

Add or update focused backend tests.

Suggested test files:

- `openrag/tests/test_char_spans_chunk.py`
- `openrag/tests/test_parser_integration.py` or a new focused parser test file such as `openrag/tests/test_md_txt_source_spans.py`
- `openrag/tests/test_chunk_engine.py` only if chunk propagation needs adjustment

Required test cases:

- TXT parser preserves source spans for LF paragraphs.
- TXT parser preserves source spans for CRLF paragraphs.
- TXT parser handles leading/trailing blank lines without shifting spans.
- Markdown heading block satisfies `raw[start:end] == block.text` and has the expected heading level.
- Markdown paragraph/list block satisfies the source slice invariant.
- Markdown fenced code block includes fence lines and satisfies the source slice invariant.
- Markdown table block remains in source order and satisfies the source slice invariant.
- Markdown repeated text does not rely on `find()` and maps to the correct source occurrence.
- Semantic chunking persists `source_char_start/source_char_end` that can slice the original raw text.

Targeted verification command:

```powershell
python -m pytest openrag/tests/test_char_spans_chunk.py openrag/tests/test_parser_integration.py openrag/tests/test_chunk_engine.py -q
```

If a new focused test file is created, include it in the targeted command.

## Acceptance Criteria

- For every MD/TXT parser block with source metadata, `raw[char_start:char_end] == block.text`.
- Markdown tables are not moved after paragraphs during parser output.
- Markdown parser does not use `md_text.find(...)` to recover positions from processed text.
- TXT parser no longer uses `errors="ignore"`.
- Existing upload, worker, search, and frontend APIs do not need contract changes.
- Existing PDF mapping behavior is unchanged.
- New tests cover source span accuracy for MD and TXT.

## Risks

- The Markdown scanner will be less semantically rich than a full Markdown AST parser. This is acceptable because the current goal is accurate source mapping, not complete Markdown rendering semantics.
- Chunk text may include Markdown syntax such as heading markers, table pipes, and code fences. This is intentional for source-coordinate mapping.
- If downstream retrieval quality depended on RAGFlow Markdown table extraction, removing it from the MD adapter may alter chunk text shape. If this becomes an issue, a later enhancement can add normalized retrieval text in metadata while keeping `DocumentBlock.text` source-sliced.

## Implementation Notes

Keep the implementation surgical:

- Do not modify upload APIs.
- Do not modify frontend preview.
- Do not add external dependencies.
- Do not change DOC/DOCX behavior in this round.
- Do not change chunking broadly unless parser-source spans alone are insufficient.

The most important invariant is source-slice equality. It should be asserted in tests because it is the simplest guard against future drift.
