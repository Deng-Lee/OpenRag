"""Parity tests: ingest orchestration helpers (no type branching in DocumentProcessor)."""

import os
from pathlib import Path

from openrag.chunking.chunk_params import (
    chunk_size_overlap_from_env,
    resolve_chunk_method,
)


def test_resolve_chunk_method_pdf_and_ppt():
    assert resolve_chunk_method("x.pdf", "auto") == "pdf_manual"
    assert resolve_chunk_method("x.PDF", "auto") == "pdf_manual"
    assert resolve_chunk_method("x.pdf", "txt") == "manual"
    assert resolve_chunk_method("x.pptx", "auto") == "presentation"
    assert resolve_chunk_method("x.docx", "auto") == "manual"


def test_resolve_chunk_method_pdf_parsers_use_pdf_manual():
    assert resolve_chunk_method("x.pdf", "pdf") == "pdf_manual"
    assert resolve_chunk_method("x.pdf", "deepdoc") == "pdf_manual"


def test_chunk_size_overlap_from_env_defaults():
    for k in ("OPENRAG_CHUNK_SIZE", "OPENRAG_CHUNK_OVERLAP"):
        os.environ.pop(k, None)
    size, overlap = chunk_size_overlap_from_env()
    assert size == 600
    assert overlap == 80


def test_parity_fixtures_readme_exists():
    readme = Path(__file__).resolve().parent / "fixtures" / "README.md"
    assert readme.is_file(), f"Missing parity fixtures guide: {readme}"
