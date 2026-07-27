import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from openrag.chunking.chunk_engine import ChunkEngine, ChunkStrategy
from openrag.api.files_api import ReprocessRequest
from openrag.models import Base, File, Task, User, Workspace
from openrag.parsers.base import DocumentBlock
from openrag.parsers.factory import ParserFactory
from openrag.parsers.parser_registry import ParserRegistry
from openrag.parsers.adapters.pdf_adapter import PDFParserAdapter
from openrag.parsers.adapters.paddleocr_pdf_adapter import PaddleOCRPDFParserAdapter
from openrag.parsers.adapters import paddleocr_pdf_adapter as paddleocr_adapter
from openrag.parsers.selection import resolve_pdf_default_parser_type
from openrag.ragflow_core.compat import to_document_blocks
from openrag.services.file_ingest import (
    SUPPORTED_PARSER_TYPES,
    ingest_new_file,
    is_processing_supported,
)


@pytest.fixture()
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    Base.metadata.drop_all(engine)


@pytest.fixture()
def wsowner(db: Session):
    user = User(
        username="u",
        email="u@example.com",
        password_hash="h",
        full_name="U",
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    workspace = Workspace(name="W", slug="w", owner_id=user.id)
    db.add(workspace)
    db.commit()
    db.refresh(workspace)
    return workspace, user


def test_public_pdf_parser_types_exclude_legacy_paddleocr_value():
    assert "pdf" in SUPPORTED_PARSER_TYPES
    assert "deepdoc" in SUPPORTED_PARSER_TYPES
    assert "paddleocr" not in SUPPORTED_PARSER_TYPES
    assert ReprocessRequest(parser_type="pdf").parser_type == "pdf"
    assert ReprocessRequest(parser_type="deepdoc").parser_type == "deepdoc"
    with pytest.raises(ValueError):
        ReprocessRequest(parser_type="paddleocr")


@pytest.mark.parametrize(
    ("content_type", "filename"),
    [
        ("application/pdf", "report.pdf"),
        ("application/octet-stream", "report.pdf"),
    ],
)
@pytest.mark.parametrize("parser_type", ["pdf", "deepdoc"])
def test_pdf_parsers_allow_pdf(content_type, filename, parser_type):
    assert is_processing_supported(content_type, filename, parser_type) is True


@pytest.mark.parametrize(
    ("content_type", "filename"),
    [
        (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "report.docx",
        ),
        ("text/plain", "notes.txt"),
        ("application/pdf", "notes.txt"),
        (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "data.xlsx",
        ),
    ],
)
@pytest.mark.parametrize("parser_type", ["pdf", "deepdoc"])
def test_pdf_parsers_reject_non_pdf(content_type, filename, parser_type):
    assert is_processing_supported(content_type, filename, parser_type) is False


def test_auto_pdf_remains_supported():
    assert is_processing_supported("application/pdf", "report.pdf", "auto") is True


def test_pdf_parser_pdf_remains_supported():
    assert is_processing_supported("application/pdf", "report.pdf", "pdf") is True


def test_parser_factory_routes_explicit_pdf_to_paddleocr_adapter():
    parser = ParserFactory().get_parser_by_type("pdf")

    assert isinstance(parser, PaddleOCRPDFParserAdapter)


def test_parser_factory_routes_explicit_deepdoc_to_deepdoc_adapter():
    parser = ParserFactory().get_parser_by_type("deepdoc")

    assert isinstance(parser, PDFParserAdapter)


def test_parser_factory_routes_auto_pdf_to_paddleocr_adapter():
    parser = ParserFactory().get_parser("sample.pdf")

    assert isinstance(parser, PaddleOCRPDFParserAdapter)


def test_parser_registry_routes_auto_pdf_to_paddleocr_adapter():
    parser = ParserRegistry().get_parser("sample.pdf", "auto")

    assert isinstance(parser, PaddleOCRPDFParserAdapter)


def test_parser_factory_rejects_removed_paddleocr_public_value():
    with pytest.raises(ValueError):
        ParserFactory().get_parser_by_type("paddleocr")


def test_pdf_default_resolution_only_expands_auto_pdf():
    assert resolve_pdf_default_parser_type("report.pdf", "auto") == "pdf"
    assert resolve_pdf_default_parser_type("report.docx", "auto") == "auto"
    assert resolve_pdf_default_parser_type("report.pdf", "deepdoc") == "deepdoc"


def test_parser_factory_pdf_type_map_still_points_to_pdf_extension():
    factory = ParserFactory()

    assert factory._parser_type_map["pdf"] == ".pdf"


def test_paddleocr_document_block_chunks_keep_pdf_position_fields():
    blocks = [
        DocumentBlock(
            text="PaddleOCR paragraph",
            page=2,
            offset=0,
            bbox=(10.0, 20.0, 110.0, 70.0),
            block_type="text",
            level=0,
            block_id="paddleocr:page:2:block:0",
            char_start=30,
            char_end=49,
        )
    ]

    chunks = ChunkEngine(ChunkStrategy.SEMANTIC).chunk(
        blocks,
        chunk_size=512,
        chunk_overlap=0,
        chunk_method="pdf_manual",
    )

    assert len(chunks) == 1
    assert chunks[0].page == 2
    assert chunks[0].bbox == (10.0, 20.0, 110.0, 70.0)
    assert chunks[0].source_block_id == "paddleocr:page:2:block:0"
    assert chunks[0].source_char_start == 30
    assert chunks[0].source_char_end == 49


def test_scaled_paddleocr_bbox_survives_document_block_and_chunking():
    rows, _stats = paddleocr_adapter._result_to_rows(
        {
            "pages": [
                {
                    "width": 1920,
                    "height": 1080,
                    "doc_preprocessor_res": {"angle": 0},
                    "parsing_res_list": [
                        {
                            "label": "text",
                            "content": "PaddleOCR paragraph",
                            "bbox": [543, 335, 1446, 609],
                        }
                    ],
                }
            ]
        },
        [(960.0, 540.0)],
    )
    blocks = to_document_blocks(rows, source="paddleocr")

    chunks = ChunkEngine(ChunkStrategy.SEMANTIC).chunk(
        blocks,
        chunk_size=512,
        chunk_overlap=0,
        chunk_method="pdf_manual",
    )

    assert len(chunks) == 1
    assert chunks[0].bbox == (271.5, 167.5, 723.0, 304.5)
    assert chunks[0].metadata["position_int"] == [
        (1, 271, 723, 167, 304)
    ]


def test_upload_rejects_pdf_parser_non_pdf_before_creating_document(
    db, wsowner, monkeypatch
):
    class FakeMinioStorage:
        def put_file(self, *args, **kwargs):
            return None

    monkeypatch.setattr(
        "openrag.services.file_ingest.MinioStorage", FakeMinioStorage
    )

    workspace, user = wsowner
    with pytest.raises(HTTPException) as exc:
        ingest_new_file(
            db,
            workspace,
            user.id,
            parent_logical_path="/",
            upload_filename="report.docx",
            file_content=b"docx",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            parser_type="pdf",
        )

    assert exc.value.status_code == 400
    assert db.query(File).filter(File.is_directory.is_(False)).count() == 0
    assert db.query(Task).count() == 0


def test_upload_rejects_pdf_parser_spoofed_pdf_content_type(
    db, wsowner, monkeypatch
):
    class FakeMinioStorage:
        def put_file(self, *args, **kwargs):
            return None

    monkeypatch.setattr(
        "openrag.services.file_ingest.MinioStorage", FakeMinioStorage
    )

    workspace, user = wsowner
    with pytest.raises(HTTPException) as exc:
        ingest_new_file(
            db,
            workspace,
            user.id,
            parent_logical_path="/",
            upload_filename="notes.txt",
            file_content=b"not pdf",
            content_type="application/pdf",
            parser_type="pdf",
        )

    assert exc.value.status_code == 400
    assert db.query(File).filter(File.is_directory.is_(False)).count() == 0
    assert db.query(Task).count() == 0


def test_upload_accepts_pdf_parser_with_pdf_magic(db, wsowner, monkeypatch):
    class FakeMinioStorage:
        def put_file(self, *args, **kwargs):
            return None

    monkeypatch.setattr(
        "openrag.services.file_ingest.MinioStorage", FakeMinioStorage
    )

    workspace, user = wsowner
    file_record, task = ingest_new_file(
        db,
        workspace,
        user.id,
        parent_logical_path="/",
        upload_filename="report.pdf",
        file_content=b"%PDF-1.7\n",
        content_type="application/octet-stream",
        parser_type="pdf",
    )

    assert file_record.mime_type == "application/pdf"
    assert file_record.parser_type == "pdf"
    assert task is not None


def test_upload_auto_pdf_persists_effective_pdf_parser(db, wsowner, monkeypatch):
    class FakeMinioStorage:
        def put_file(self, *args, **kwargs):
            return None

    monkeypatch.setattr(
        "openrag.services.file_ingest.MinioStorage", FakeMinioStorage
    )

    workspace, user = wsowner
    file_record, task = ingest_new_file(
        db,
        workspace,
        user.id,
        parent_logical_path="/",
        upload_filename="auto.pdf",
        file_content=b"%PDF-1.7\n",
        content_type="application/pdf",
        parser_type="auto",
    )

    assert file_record.parser_type == "pdf"
    assert task is not None


def test_upload_explicit_deepdoc_persists_deepdoc(db, wsowner, monkeypatch):
    class FakeMinioStorage:
        def put_file(self, *args, **kwargs):
            return None

    monkeypatch.setattr(
        "openrag.services.file_ingest.MinioStorage", FakeMinioStorage
    )

    workspace, user = wsowner
    file_record, task = ingest_new_file(
        db,
        workspace,
        user.id,
        parent_logical_path="/",
        upload_filename="deepdoc.pdf",
        file_content=b"%PDF-1.7\n",
        content_type="application/pdf",
        parser_type="deepdoc",
    )

    assert file_record.parser_type == "deepdoc"
    assert task is not None
