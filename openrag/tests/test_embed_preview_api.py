"""Tests for preview-token scoped embed document preview APIs."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import openrag.config as config_module
import openrag.models  # noqa: F401 - register all mappers on Base.metadata
from openrag.api.deps import get_db
from openrag.api.main import app
from openrag.config import get_config
from openrag.models import Base, DocumentChunk, DocumentParseArtifact, File, User, Workspace
from openrag.models.file import ProcessingStatus
from openrag.security import hash_password
from openrag.services.preview_token_service import create_preview_token


engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(autouse=True)
def reset_preview_config(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SECRET_KEY", "embed-preview-test-secret")
    monkeypatch.setattr(config_module, "_config", None)
    yield
    monkeypatch.setattr(config_module, "_config", None)


@pytest.fixture(scope="function")
def db() -> Session:
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()
    yield session
    session.close()
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def owner(db: Session) -> User:
    user = User(
        username="embed_owner",
        email="embed-owner@example.com",
        password_hash=hash_password("pw"),
        full_name="Embed Owner",
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def workspace(db: Session, owner: User) -> Workspace:
    ws = Workspace(name="EmbedPreviewWS", slug="embed-preview-ws", owner_id=owner.id)
    db.add(ws)
    db.commit()
    db.refresh(ws)
    return ws


@pytest.fixture
def client(db: Session):
    def override_get_db():
        try:
            yield db
        finally:
            pass

    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    if previous is not None:
        app.dependency_overrides[get_db] = previous
    else:
        app.dependency_overrides.pop(get_db, None)


def _file(
    db: Session,
    ws: Workspace,
    owner: User,
    uri: str = "/docs/report.txt",
    *,
    is_directory: bool = False,
    mime_type: str = "text/plain",
    processing_status: ProcessingStatus = ProcessingStatus.completed,
    total_chunks: int = 1,
) -> File:
    file = File(
        uri=uri,
        name=uri.rsplit("/", 1)[-1] or "file",
        owner_id=owner.id,
        workspace_id=ws.id,
        is_directory=is_directory,
        size=0 if is_directory else 12,
        mime_type=None if is_directory else mime_type,
        processing_status=processing_status,
        total_chunks=0 if is_directory else total_chunks,
    )
    db.add(file)
    db.commit()
    db.refresh(file)
    return file


def _chunk(
    db: Session,
    ws: Workspace,
    file: File,
    chunk_id: str = "chunk-embed-1",
    *,
    chunk_index: int = 3,
    text: str = "target chunk text",
) -> DocumentChunk:
    chunk = DocumentChunk(
        id=db.query(DocumentChunk).count() + 1,
        file_id=file.id,
        workspace_id=ws.id,
        chunk_id=chunk_id,
        chunk_index=chunk_index,
        object_key=f"chunks/{chunk_id}.md",
        text_preview=text,
        page=2,
        bbox_x0=10.0,
        bbox_y0=20.0,
        bbox_x1=30.0,
        bbox_y1=40.0,
        source_char_start=100,
        source_char_end=120,
        position_int=[[2, 10, 30, 20, 40]],
    )
    db.add(chunk)
    db.commit()
    db.refresh(chunk)
    return chunk


def _preview_headers(
    *,
    workspace_id: int,
    file_id: int,
    chunk_id: str,
    chunk_index: int | None,
    ttl_seconds: int | None = 900,
) -> dict[str, str]:
    token, _ = create_preview_token(
        workspace_id=workspace_id,
        file_id=file_id,
        chunk_id=chunk_id,
        chunk_index=chunk_index,
        ttl_seconds=ttl_seconds,
    )
    return {"X-OpenRag-Preview-Token": token}


def _expired_preview_token(
    *,
    workspace_id: int,
    file_id: int,
    chunk_id: str,
    chunk_index: int,
) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "scope": "document_preview",
        "workspace_id": workspace_id,
        "file_id": file_id,
        "chunk_id": chunk_id,
        "chunk_index": chunk_index,
        "iat": int((now - timedelta(minutes=30)).timestamp()),
        "exp": int((now - timedelta(minutes=1)).timestamp()),
    }
    config = get_config()
    return jwt.encode(payload, config.security.secret_key, algorithm=config.security.algorithm)


def test_embed_document_preview_requires_preview_token_even_with_other_tokens(
    client: TestClient,
) -> None:
    response = client.get(
        "/embed/v1/document-preview",
        headers={
            "X-OpenRag-Token": "sk-long-lived-token",
            "Authorization": "Bearer openrag-jwt",
        },
    )

    assert response.status_code == 401


def test_embed_document_preview_returns_bound_file_chunk_and_expiry(
    client: TestClient,
    db: Session,
    workspace: Workspace,
    owner: User,
) -> None:
    file = _file(db, workspace, owner, "/docs/report.pdf", mime_type="application/pdf")
    chunk = _chunk(db, workspace, file, "chunk-context-ok", chunk_index=7)
    headers = _preview_headers(
        workspace_id=workspace.id,
        file_id=file.id,
        chunk_id=chunk.chunk_id,
        chunk_index=chunk.chunk_index,
    )

    response = client.get("/embed/v1/document-preview", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["file"]["id"] == file.id
    assert body["file"]["workspace_id"] == workspace.id
    assert body["file"]["name"] == "report.pdf"
    assert body["file"]["uri"] == "/docs/report.pdf"
    assert body["file"]["mime_type"] == "application/pdf"
    assert body["file"]["simple_status"] == "done"
    assert body["chunk"]["file_id"] == file.id
    assert body["chunk"]["workspace_id"] == workspace.id
    assert body["chunk"]["chunk_id"] == "chunk-context-ok"
    assert body["chunk"]["chunk_index"] == 7
    assert body["chunk"]["text"] == "target chunk text"
    assert body["chunk"]["positions"] == [[2, 10, 30, 20, 40]]
    assert body["expires_at"]


def test_embed_document_preview_rejects_tampered_and_expired_tokens(
    client: TestClient,
    db: Session,
    workspace: Workspace,
    owner: User,
) -> None:
    file = _file(db, workspace, owner)
    chunk = _chunk(db, workspace, file, "chunk-invalid-token", chunk_index=1)
    valid_token = _preview_headers(
        workspace_id=workspace.id,
        file_id=file.id,
        chunk_id=chunk.chunk_id,
        chunk_index=chunk.chunk_index,
    )["X-OpenRag-Preview-Token"]
    tampered = valid_token[:-1] + ("a" if valid_token[-1] != "a" else "b")
    expired = _expired_preview_token(
        workspace_id=workspace.id,
        file_id=file.id,
        chunk_id=chunk.chunk_id,
        chunk_index=chunk.chunk_index,
    )

    for token in (tampered, expired):
        response = client.get(
            "/embed/v1/document-preview",
            headers={"X-OpenRag-Preview-Token": token},
        )
        assert response.status_code == 401


def test_embed_document_preview_rejects_broken_token_binding(
    client: TestClient,
    db: Session,
    workspace: Workspace,
    owner: User,
) -> None:
    file = _file(db, workspace, owner, "/docs/bound.txt")
    chunk = _chunk(db, workspace, file, "chunk-index-broken", chunk_index=5)
    headers = _preview_headers(
        workspace_id=workspace.id,
        file_id=file.id,
        chunk_id=chunk.chunk_id,
        chunk_index=6,
    )

    response = client.get("/embed/v1/document-preview", headers=headers)

    assert response.status_code == 400
    assert response.json()["detail"] == "chunk_index does not match chunk"


def test_embed_file_content_streams_bound_file_inline(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    db: Session,
    workspace: Workspace,
    owner: User,
) -> None:
    file = _file(db, workspace, owner, "/docs/report.txt", mime_type="text/plain")
    chunk = _chunk(db, workspace, file, "chunk-content-ok", chunk_index=2)
    headers = _preview_headers(
        workspace_id=workspace.id,
        file_id=file.id,
        chunk_id=chunk.chunk_id,
        chunk_index=chunk.chunk_index,
    )
    calls: list[tuple[str, str]] = []

    class FakeStream:
        closed = False
        released = False

        def stream(self, chunk_size: int):
            assert chunk_size == 64 * 1024
            yield b"hello "
            yield b"world"

        def close(self) -> None:
            self.closed = True

        def release_conn(self) -> None:
            self.released = True

    fake_stream = FakeStream()

    class FakeStorage:
        def open_object_stream(self, bucket: str, object_key: str):
            calls.append((bucket, object_key))
            return fake_stream

    monkeypatch.setattr("openrag.api.workspace_file_api.MinioStorage", FakeStorage)

    response = client.get("/embed/v1/files/content", headers=headers)

    assert response.status_code == 200
    assert response.content == b"hello world"
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["content-disposition"] == 'inline; filename="report.txt"'
    assert calls == [(workspace.slug, "docs/report.txt")]
    assert fake_stream.closed is True
    assert fake_stream.released is True


def test_embed_file_preview_returns_backend_preview_payload(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    db: Session,
    workspace: Workspace,
    owner: User,
) -> None:
    file = _file(db, workspace, owner, "/docs/report.html", mime_type="text/html")
    chunk = _chunk(db, workspace, file, "chunk-preview-ok", chunk_index=4)
    headers = _preview_headers(
        workspace_id=workspace.id,
        file_id=file.id,
        chunk_id=chunk.chunk_id,
        chunk_index=chunk.chunk_index,
    )
    preview_calls: list[tuple[bytes, str, str]] = []

    class FakeStorage:
        def read_object_bytes(self, bucket: str, object_key: str) -> bytes:
            assert (bucket, object_key) == (workspace.slug, "docs/report.html")
            return b"<p>source</p>"

    def fake_preview(data: bytes, mime_type: str, filename: str):
        preview_calls.append((data, mime_type, filename))
        return "html", "<div>preview</div>"

    monkeypatch.setattr("openrag.api.workspace_file_api.MinioStorage", FakeStorage)
    monkeypatch.setattr("openrag.api.workspace_file_api.build_file_preview", fake_preview)

    response = client.get("/embed/v1/files/preview", headers=headers)

    assert response.status_code == 200
    assert response.json() == {"format": "html", "content": "<div>preview</div>"}
    assert preview_calls == [(b"<p>source</p>", "text/html", "report.html")]


def test_embed_chunk_source_returns_canonical_source(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    db: Session,
    workspace: Workspace,
    owner: User,
) -> None:
    file = _file(db, workspace, owner, "/docs/report.txt")
    chunk = _chunk(db, workspace, file, "chunk-source-ok", chunk_index=8)
    artifact = DocumentParseArtifact(
        artifact_id="artifact-source-ok",
        file_id=file.id,
        workspace_id=workspace.id,
        source_doc_hash="hash-source-ok",
        parser_name="TxtParserAdapter",
        parser_version="1.0",
        canonical_json_bucket=workspace.slug,
        canonical_json_object_key="canonical/report.json",
        canonical_json_size_bytes=2,
        canonical_md_bucket=workspace.slug,
        canonical_md_object_key="canonical/report.md",
        canonical_md_size_bytes=10,
        status="completed",
    )
    db.add(artifact)
    db.commit()
    headers = _preview_headers(
        workspace_id=workspace.id,
        file_id=file.id,
        chunk_id=chunk.chunk_id,
        chunk_index=chunk.chunk_index,
    )
    calls: list[tuple[str, str]] = []

    class FakeStorage:
        def read_object_bytes(self, bucket: str, object_key: str) -> bytes:
            calls.append((bucket, object_key))
            return "Alpha\nBeta".encode("utf-8")

    monkeypatch.setattr("openrag.api.workspace_file_api.MinioStorage", FakeStorage)

    response = client.get("/embed/v1/files/chunk-source", headers=headers)

    assert response.status_code == 200
    assert response.json() == {"format": "text", "content": "Alpha\nBeta"}
    assert calls == [(workspace.slug, "canonical/report.md")]
