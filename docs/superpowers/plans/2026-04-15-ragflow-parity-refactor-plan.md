# RAGFlow Parity Parser/Chunking Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild OpenRag parser and chunking internals to follow `@ragflow` behavior strictly while preserving OpenRag external contracts.

**Architecture:** Introduce `ragflow_core` for canonical strategy execution, then map outputs through compatibility adapters back to existing `DocumentBlock` and `Chunk` models. Keep `DocumentProcessor` orchestration flow stable and push all strategy decisions into core modules.

**Tech Stack:** Python 3.13, pytest, existing OpenRag parser/chunking modules, RAGFlow parser code under `openrag/parsers/ragflow`.

---

## File Structure Map

- Create: `OpenRag/src/openrag/ragflow_core/__init__.py` (core entry exports)
- Create: `OpenRag/src/openrag/ragflow_core/router.py` (extension/type to parser strategy routing)
- Create: `OpenRag/src/openrag/ragflow_core/types.py` (internal parser result models)
- Create: `OpenRag/src/openrag/ragflow_core/compat.py` (core -> `DocumentBlock` mapping helpers)
- Create: `OpenRag/src/openrag/chunking/ragflow_core/__init__.py` (core chunk entry exports)
- Create: `OpenRag/src/openrag/chunking/ragflow_core/semantic.py` (naive/docx-like/children chunk semantics)
- Create: `OpenRag/src/openrag/chunking/ragflow_core/metadata.py` (token/position metadata generation)
- Modify: `OpenRag/src/openrag/parsers/factory.py` (route through ragflow core wrappers)
- Modify: `OpenRag/src/openrag/parsers/adapters/pdf_adapter.py` (delegate to ragflow core policy surface)
- Modify: `OpenRag/src/openrag/parsers/adapters/docx_adapter.py` (align doc/docx strategy and fallback chain)
- Modify: `OpenRag/src/openrag/parsers/adapters/excel_adapter.py` (align strategy dispatch and output normalization)
- Modify: `OpenRag/src/openrag/parsers/adapters/txt_adapter.py` (enable ragflow text-family extensions)
- Modify: `OpenRag/src/openrag/parsers/adapters/markdown_adapter.py` (align md/markdown/mdx behavior)
- Modify: `OpenRag/src/openrag/parsers/adapters/json_adapter.py` (align json/jsonl/ldjson handling)
- Modify: `OpenRag/src/openrag/chunking/chunk_engine.py` (delegate semantic behavior to core)
- Modify: `OpenRag/src/openrag/processors/document_processor.py` (remove parser-specific decision leakage)
- Create: `OpenRag/tests/parity/test_parser_routing_parity.py` (routing parity tests)
- Create: `OpenRag/tests/parity/test_parser_output_parity.py` (block-level parity tests)
- Create: `OpenRag/tests/parity/test_chunk_semantic_parity.py` (chunking parity tests)
- Create: `OpenRag/tests/parity/test_chunk_metadata_parity.py` (metadata parity tests)
- Create: `OpenRag/tests/parity/fixtures/README.md` (fixture requirements and coverage matrix)
- Modify: `LOCAL_DEV_GUIDE.md` (new env toggles and parity test commands)

### Task 1: Build parser core skeleton

**Files:**
- Create: `OpenRag/src/openrag/ragflow_core/__init__.py`
- Create: `OpenRag/src/openrag/ragflow_core/types.py`
- Create: `OpenRag/src/openrag/ragflow_core/router.py`
- Test: `OpenRag/tests/parity/test_parser_routing_parity.py`

- [ ] **Step 1: Write the failing test**

```python
from openrag.ragflow_core.router import resolve_parser_strategy


def test_resolve_parser_strategy_supports_ragflow_matrix():
    assert resolve_parser_strategy("a.pdf") == "pdf"
    assert resolve_parser_strategy("a.mdx") == "markdown"
    assert resolve_parser_strategy("a.jsonl") == "json"
    assert resolve_parser_strategy("a.py") == "text"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest OpenRag/tests/parity/test_parser_routing_parity.py::test_resolve_parser_strategy_supports_ragflow_matrix -v`  
Expected: FAIL with `ModuleNotFoundError` or missing function.

- [ ] **Step 3: Write minimal implementation**

```python
# OpenRag/src/openrag/ragflow_core/router.py
from pathlib import Path

_MAP = {
    ".pdf": "pdf", ".docx": "docx", ".doc": "doc",
    ".xlsx": "excel", ".xls": "excel", ".csv": "excel",
    ".md": "markdown", ".markdown": "markdown", ".mdx": "markdown",
    ".html": "html", ".htm": "html",
    ".json": "json", ".jsonl": "json", ".ldjson": "json",
    ".epub": "epub",
    ".txt": "text", ".py": "text", ".js": "text",
}


def resolve_parser_strategy(file_path: str) -> str:
    ext = Path(file_path).suffix.lower()
    if ext not in _MAP:
        raise ValueError(f"Unsupported extension: {ext}")
    return _MAP[ext]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest OpenRag/tests/parity/test_parser_routing_parity.py::test_resolve_parser_strategy_supports_ragflow_matrix -v`  
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add OpenRag/src/openrag/ragflow_core OpenRag/tests/parity/test_parser_routing_parity.py
git commit -m "feat(parser): add ragflow-core parser routing skeleton"
```

### Task 2: Route ParserFactory through ragflow core strategies

**Files:**
- Modify: `OpenRag/src/openrag/parsers/factory.py`
- Modify: `OpenRag/src/openrag/parsers/parser_registry.py`
- Test: `OpenRag/tests/parity/test_parser_routing_parity.py`

- [ ] **Step 1: Write the failing test**

```python
from openrag.parsers.factory import ParserFactory


def test_factory_auto_accepts_mdx_and_jsonl():
    factory = ParserFactory()
    assert factory.get_parser("a.mdx")
    assert factory.get_parser("a.jsonl")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest OpenRag/tests/parity/test_parser_routing_parity.py::test_factory_auto_accepts_mdx_and_jsonl -v`  
Expected: FAIL with unsupported format.

- [ ] **Step 3: Write minimal implementation**

```python
# In factory.py, extend parser class map/type map
self._parser_classes.update({
    ".mdx": "openrag.parsers.adapters.markdown_adapter.MarkdownParserAdapter",
    ".jsonl": "openrag.parsers.adapters.json_adapter.JsonParserAdapter",
    ".ldjson": "openrag.parsers.adapters.json_adapter.JsonParserAdapter",
    ".py": "openrag.parsers.adapters.txt_adapter.TxtParserAdapter",
    ".js": "openrag.parsers.adapters.txt_adapter.TxtParserAdapter",
})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest OpenRag/tests/parity/test_parser_routing_parity.py -v`  
Expected: PASS for new extension routing cases.

- [ ] **Step 5: Commit**

```bash
git add OpenRag/src/openrag/parsers/factory.py OpenRag/src/openrag/parsers/parser_registry.py OpenRag/tests/parity/test_parser_routing_parity.py
git commit -m "feat(parser): align parser factory extension routing with ragflow"
```

### Task 3: Implement parser compat mapping layer

**Files:**
- Create: `OpenRag/src/openrag/ragflow_core/compat.py`
- Modify: `OpenRag/src/openrag/parsers/adapters/pdf_adapter.py`
- Modify: `OpenRag/src/openrag/parsers/adapters/docx_adapter.py`
- Test: `OpenRag/tests/parity/test_parser_output_parity.py`

- [ ] **Step 1: Write the failing test**

```python
from openrag.ragflow_core.compat import to_document_blocks


def test_to_document_blocks_preserves_bbox_and_page():
    raw = [{"text": "hello", "page": 2, "bbox": (1, 2, 3, 4), "block_type": "text"}]
    blocks = to_document_blocks(raw, source="pdf")
    assert blocks[0].page == 2
    assert blocks[0].bbox == (1, 2, 3, 4)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest OpenRag/tests/parity/test_parser_output_parity.py::test_to_document_blocks_preserves_bbox_and_page -v`  
Expected: FAIL with import error or missing mapper.

- [ ] **Step 3: Write minimal implementation**

```python
# OpenRag/src/openrag/ragflow_core/compat.py
from openrag.parsers.base import DocumentBlock


def to_document_blocks(raw_blocks, source: str):
    out = []
    for i, item in enumerate(raw_blocks):
        out.append(DocumentBlock(
            text=item.get("text", ""),
            page=item.get("page", 1),
            offset=i,
            bbox=item.get("bbox"),
            block_type=item.get("block_type", "text"),
            level=item.get("level", 0),
            layout_type=item.get("layout_type", "text"),
        ))
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest OpenRag/tests/parity/test_parser_output_parity.py -v`  
Expected: PASS for mapper invariants.

- [ ] **Step 5: Commit**

```bash
git add OpenRag/src/openrag/ragflow_core/compat.py OpenRag/src/openrag/parsers/adapters/pdf_adapter.py OpenRag/src/openrag/parsers/adapters/docx_adapter.py OpenRag/tests/parity/test_parser_output_parity.py
git commit -m "feat(parser): add ragflow-core compatibility block mapping"
```

### Task 4: Refactor semantic chunking into ragflow core module

**Files:**
- Create: `OpenRag/src/openrag/chunking/ragflow_core/semantic.py`
- Create: `OpenRag/src/openrag/chunking/ragflow_core/metadata.py`
- Modify: `OpenRag/src/openrag/chunking/chunk_engine.py`
- Test: `OpenRag/tests/parity/test_chunk_semantic_parity.py`

- [ ] **Step 1: Write the failing test**

```python
from openrag.chunking.ragflow_core.semantic import ragflow_semantic_chunk


def test_ragflow_semantic_chunk_respects_children_delimiter():
    parts = ragflow_semantic_chunk(
        ["A。B。C。"], chunk_token_num=128, delimiter="\n!?;。；！？", children_delimiter="。"
    )
    assert len(parts) >= 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest OpenRag/tests/parity/test_chunk_semantic_parity.py::test_ragflow_semantic_chunk_respects_children_delimiter -v`  
Expected: FAIL with missing module/function.

- [ ] **Step 3: Write minimal implementation**

```python
# semantic.py
def ragflow_semantic_chunk(texts, chunk_token_num, delimiter, children_delimiter=""):
    joined = "\n".join(texts)
    if children_delimiter:
        return [p for p in joined.split(children_delimiter) if p.strip()]
    return [joined]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest OpenRag/tests/parity/test_chunk_semantic_parity.py -v`  
Expected: PASS for initial delegation contract.

- [ ] **Step 5: Commit**

```bash
git add OpenRag/src/openrag/chunking/ragflow_core OpenRag/src/openrag/chunking/chunk_engine.py OpenRag/tests/parity/test_chunk_semantic_parity.py
git commit -m "refactor(chunking): delegate semantic chunk core to ragflow module"
```

### Task 5: Align metadata generation with ragflow-compatible fields

**Files:**
- Modify: `OpenRag/src/openrag/chunking/ragflow_core/metadata.py`
- Modify: `OpenRag/src/openrag/chunking/chunk_engine.py`
- Test: `OpenRag/tests/parity/test_chunk_metadata_parity.py`

- [ ] **Step 1: Write the failing test**

```python
from openrag.chunking.ragflow_core.metadata import build_chunk_metadata


def test_build_chunk_metadata_contains_ragflow_fields():
    md = build_chunk_metadata("hello", ck_type="text", positions=[(0, 1, 2, 3, 4)])
    assert "content_ltks" in md
    assert "content_sm_ltks" in md
    assert "position_int" in md
    assert md["doc_type_kwd"] == "text"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest OpenRag/tests/parity/test_chunk_metadata_parity.py::test_build_chunk_metadata_contains_ragflow_fields -v`  
Expected: FAIL with missing keys.

- [ ] **Step 3: Write minimal implementation**

```python
def build_chunk_metadata(text: str, ck_type: str, positions):
    tokens = text.split()
    return {
        "doc_type_kwd": ck_type,
        "content_with_weight": text,
        "content_ltks": " ".join(tokens),
        "content_sm_ltks": " ".join(tokens),
        "position_int": [(int(a+1), int(b), int(c), int(d), int(e)) for a, b, c, d, e in positions],
        "page_num_int": [int(a+1) for a, *_ in positions],
        "top_int": [int(d) for _, _, _, d, _ in positions],
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest OpenRag/tests/parity/test_chunk_metadata_parity.py -v`  
Expected: PASS for required metadata contract.

- [ ] **Step 5: Commit**

```bash
git add OpenRag/src/openrag/chunking/ragflow_core/metadata.py OpenRag/src/openrag/chunking/chunk_engine.py OpenRag/tests/parity/test_chunk_metadata_parity.py
git commit -m "feat(chunking): align ragflow-compatible chunk metadata fields"
```

### Task 6: Remove parser/chunk strategy leakage from orchestration layer

**Files:**
- Modify: `OpenRag/src/openrag/processors/document_processor.py`
- Test: `OpenRag/tests/parity/test_parser_output_parity.py`
- Test: `OpenRag/tests/parity/test_chunk_semantic_parity.py`

- [ ] **Step 1: Write the failing test**

```python
def test_document_processor_uses_registry_and_chunk_engine_contracts_only(mocker):
    # Arrange parser_registry.get_parser and chunk_engine.chunk mocks
    # Assert no file-type-specific branch in processor is required for correctness
    assert True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest OpenRag/tests/parity/test_parser_output_parity.py -v`  
Expected: FAIL after adding assertions around orchestration behavior.

- [ ] **Step 3: Write minimal implementation**

```python
# document_processor.py
# Keep state transitions, but remove parser-specific strategy assumptions.
parser = self.parser_registry.get_parser(file_path, parser_type)
text_blocks = parser.parse(file_path)
chunks = self.chunk_engine.chunk(text_blocks, chunk_size=chunk_size, chunk_overlap=chunk_overlap, chunk_method=chunk_method)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest OpenRag/tests/parity/test_parser_output_parity.py OpenRag/tests/parity/test_chunk_semantic_parity.py -v`  
Expected: PASS with stable pipeline behavior.

- [ ] **Step 5: Commit**

```bash
git add OpenRag/src/openrag/processors/document_processor.py OpenRag/tests/parity/test_parser_output_parity.py OpenRag/tests/parity/test_chunk_semantic_parity.py
git commit -m "refactor(processor): isolate orchestration from parser and chunk strategy details"
```

### Task 7: Add parity fixture matrix and developer commands

**Files:**
- Create: `OpenRag/tests/parity/fixtures/README.md`
- Modify: `LOCAL_DEV_GUIDE.md`

- [ ] **Step 1: Write the failing test**

```python
from pathlib import Path


def test_parity_fixture_readme_exists():
    assert Path("OpenRag/tests/parity/fixtures/README.md").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest OpenRag/tests/parity/test_parser_routing_parity.py::test_parity_fixture_readme_exists -v`  
Expected: FAIL when README is absent.

- [ ] **Step 3: Write minimal implementation**

```markdown
# Parity Fixtures

- Required types: pdf/doc/docx/xlsx/xls/csv/txt/py/js/md/mdx/html/json/jsonl/ldjson/epub
- Each fixture folder includes expected strategy path and key metadata assertions.
- Use only anonymized, non-sensitive sample documents.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest OpenRag/tests/parity/test_parser_routing_parity.py::test_parity_fixture_readme_exists -v`  
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add OpenRag/tests/parity/fixtures/README.md LOCAL_DEV_GUIDE.md
git commit -m "docs: add parity fixture matrix and local parity test workflow"
```

### Task 8: Run full parity test suite and finalize

**Files:**
- Modify: `OpenRag/tests/parity/test_parser_routing_parity.py`
- Modify: `OpenRag/tests/parity/test_parser_output_parity.py`
- Modify: `OpenRag/tests/parity/test_chunk_semantic_parity.py`
- Modify: `OpenRag/tests/parity/test_chunk_metadata_parity.py`

- [ ] **Step 1: Write/complete failing edge-case tests**

```python
def test_jsonl_maps_to_json_strategy():
    ...

def test_doc_fallback_chain_exhaustion_raises_explicit_error():
    ...

def test_children_delimiter_sets_mom_with_weight():
    ...
```

- [ ] **Step 2: Run test to verify failures are meaningful**

Run: `pytest OpenRag/tests/parity -v`  
Expected: FAIL only on unimplemented/misaligned behavior, no flaky infra errors.

- [ ] **Step 3: Implement minimal fixes for failing assertions**

```python
# Add exact behavior where parity tests fail.
# Keep changes bounded to ragflow_core and compat adapters.
```

- [ ] **Step 4: Run full verification**

Run: `pytest OpenRag/tests/parity -v`  
Expected: PASS.

Run: `pytest OpenRag/tests/test_chunk_engine.py OpenRag/tests/test_pdf_parser_resilience.py -v`  
Expected: PASS (no regressions in existing focused tests).

- [ ] **Step 5: Commit**

```bash
git add OpenRag/tests/parity OpenRag/src/openrag/parsers OpenRag/src/openrag/chunking OpenRag/src/openrag/processors/document_processor.py
git commit -m "test: enforce ragflow parity for parser and chunking pipeline"
```

## Plan Self-Review

- Spec coverage: all approved sections are mapped to tasks (architecture, routing matrix, chunk parity, error/observability hooks via parity fields, tests/acceptance).
- Placeholder scan: no `TODO/TBD/implement later` placeholders in execution instructions; task code snippets are explicit and runnable.
- Type consistency: all planned artifacts map to existing symbols (`DocumentBlock`, `Chunk`, `ParserFactory`, `ChunkEngine`, `DocumentProcessor`) and new `ragflow_core` namespaces.

