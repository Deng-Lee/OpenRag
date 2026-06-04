import pytest
from fastapi import FastAPI
from fastapi import HTTPException, status
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from openrag.api.deps import get_current_active_user, get_db
from openrag.models import (
    Base,
    DocumentChunk,
    DocumentParseArtifact,
    File,
    User,
    Workspace,
    WorkspaceMember,
)
from openrag.models.file import ProcessingStatus
from openrag.api.workspace_file_api import (
    _content_disposition_inline,
    _open_file_stream_from_workspace,
    _read_file_bytes_from_workspace,
    _simple_status,
    get_readable_workspace_file_or_404,
    router,
)


@pytest.fixture(scope="function")
def db_session() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        echo=False,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.close()
    Base.metadata.drop_all(engine)


@pytest.fixture
def owner(db_session: Session) -> User:
    user = User(
        username="wf_owner",
        email="wf-owner@example.com",
        password_hash="h",
        full_name="Owner",
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def other_user(db_session: Session) -> User:
    user = User(
        username="wf_other",
        email="wf-other@example.com",
        password_hash="h",
        full_name="Other",
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def workspace(db_session: Session, owner: User) -> Workspace:
    ws = Workspace(name="Workspace File", slug="workspace-file", owner_id=owner.id)
    db_session.add(ws)
    db_session.commit()
    db_session.refresh(ws)
    db_session.add(WorkspaceMember(workspace_id=ws.id, user_id=owner.id, role="read"))
    db_session.commit()
    return ws


def _workspace(db: Session, owner: User, slug: str) -> Workspace:
    ws = Workspace(name=slug, slug=slug, owner_id=owner.id)
    db.add(ws)
    db.commit()
    db.refresh(ws)
    return ws


def _file(
    db: Session,
    ws: Workspace,
    owner: User,
    uri: str = "/docs/a.txt",
    *,
    is_directory: bool = False,
    total_chunks: int = 0,
    mime_type: str = "text/plain",
    processing_status: ProcessingStatus = ProcessingStatus.pending,
) -> File:
    file = File(
        uri=uri,
        name=uri.rsplit("/", 1)[-1],
        owner_id=owner.id,
        workspace_id=ws.id,
        is_directory=is_directory,
        size=3,
        mime_type=mime_type,
        processing_status=processing_status,
        total_chunks=total_chunks,
    )
    db.add(file)
    db.commit()
    db.refresh(file)
    return file


def _chunk(
    db: Session,
    file: File,
    *,
    chunk_index: int,
    text: str,
    chunk_id: str | None = None,
    workspace_id: int | None = None,
    position_int=None,
    page: int = 0,
    bbox_x0=None,
    bbox_y0=None,
    bbox_x1=None,
    bbox_y1=None,
    source_char_start=None,
    source_char_end=None,
) -> DocumentChunk:
    row = DocumentChunk(
        id=db.query(DocumentChunk).count() + 1,
        file_id=file.id,
        workspace_id=workspace_id or file.workspace_id,
        chunk_id=chunk_id or f"chunk-{file.id}-{chunk_index}",
        chunk_index=chunk_index,
        object_key=f"chunks/{file.id}/{chunk_index}.md",
        text_preview=text,
        page=page,
        position_int=position_int,
        bbox_x0=bbox_x0,
        bbox_y0=bbox_y0,
        bbox_x1=bbox_x1,
        bbox_y1=bbox_y1,
        source_char_start=source_char_start,
        source_char_end=source_char_end,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def workspace_file_client(db_session: Session, owner: User):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_active_user] = lambda: owner
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def test_get_readable_workspace_file_returns_file_for_matching_workspace(
    db_session: Session, workspace: Workspace, owner: User
) -> None:
    file = _file(db_session, workspace, owner)

    got = get_readable_workspace_file_or_404(
        db_session, workspace.id, file.id, owner
    )

    assert got.id == file.id


def test_get_readable_workspace_file_404_for_workspace_mismatch(
    db_session: Session, workspace: Workspace, owner: User
) -> None:
    other_ws = _workspace(db_session, owner, "other-workspace")
    file = _file(db_session, other_ws, owner, "/docs/b.txt")

    with pytest.raises(HTTPException) as exc:
        get_readable_workspace_file_or_404(
            db_session, workspace.id, file.id, owner
        )

    assert exc.value.status_code == status.HTTP_404_NOT_FOUND


def test_get_readable_workspace_file_403_without_workspace_read_permission(
    db_session: Session, workspace: Workspace, owner: User, other_user: User
) -> None:
    file = _file(db_session, workspace, owner)

    with pytest.raises(HTTPException) as exc:
        get_readable_workspace_file_or_404(
            db_session, workspace.id, file.id, other_user
        )

    assert exc.value.status_code == status.HTTP_403_FORBIDDEN


def test_list_workspace_file_chunks_scopes_by_workspace_and_file_and_orders_by_chunk_index(
    workspace_file_client: TestClient,
    db_session: Session,
    workspace: Workspace,
    owner: User,
) -> None:
    file = _file(
        db_session,
        workspace,
        owner,
        "/docs/report.pdf",
        total_chunks=3,
        mime_type="application/pdf",
        processing_status=ProcessingStatus.completed,
    )
    same_workspace_file = _file(db_session, workspace, owner, "/docs/other.pdf")
    other_ws = _workspace(db_session, owner, "workspace-file-other")
    same_name_other_workspace = _file(db_session, other_ws, owner, "/docs/report.pdf")

    _chunk(db_session, file, chunk_index=2, text="third", chunk_id="target-2")
    _chunk(db_session, file, chunk_index=0, text="first", chunk_id="target-0")
    _chunk(db_session, file, chunk_index=1, text="second", chunk_id="target-1")
    _chunk(
        db_session,
        same_workspace_file,
        chunk_index=0,
        text="same workspace different file",
        chunk_id="same-ws-other-file",
    )
    _chunk(
        db_session,
        same_name_other_workspace,
        chunk_index=0,
        text="same filename different workspace",
        chunk_id="same-name-other-ws",
    )
    _chunk(
        db_session,
        file,
        chunk_index=99,
        text="wrong workspace for target file",
        chunk_id="wrong-workspace",
        workspace_id=other_ws.id,
    )

    response = workspace_file_client.get(
        f"/workspaces/{workspace.id}/files/{file.id}/chunks",
        params={"skip": 0, "limit": 50},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["file"] == {
        "id": file.id,
        "workspace_id": workspace.id,
        "name": "report.pdf",
        "uri": "/docs/report.pdf",
        "mime_type": "application/pdf",
        "processing_status": "completed",
        "simple_status": "done",
        "total_chunks": 3,
    }
    assert data["total"] == 3
    assert [item["chunk_id"] for item in data["items"]] == [
        "target-0",
        "target-1",
        "target-2",
    ]
    assert [item["chunk_index"] for item in data["items"]] == [0, 1, 2]


def test_list_workspace_file_chunks_q_filters_text_and_escapes_like_special_chars(
    workspace_file_client: TestClient,
    db_session: Session,
    workspace: Workspace,
    owner: User,
) -> None:
    file = _file(db_session, workspace, owner, "/docs/report.txt", total_chunks=3)
    _chunk(
        db_session,
        file,
        chunk_index=0,
        text=r"literal 100%_\ marker",
        chunk_id="literal",
    )
    _chunk(db_session, file, chunk_index=1, text="literal 100abc marker", chunk_id="wild")
    _chunk(db_session, file, chunk_index=2, text="unrelated", chunk_id="none")

    response = workspace_file_client.get(
        f"/workspaces/{workspace.id}/files/{file.id}/chunks",
        params={"q": "100%_\\", "skip": 0, "limit": 50},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert [item["chunk_id"] for item in data["items"]] == ["literal"]
    assert data["items"][0]["text"] == r"literal 100%_\ marker"


def test_list_workspace_file_chunks_returns_multiple_position_int_entries(
    workspace_file_client: TestClient,
    db_session: Session,
    workspace: Workspace,
    owner: User,
) -> None:
    file = _file(db_session, workspace, owner, "/docs/report.pdf", total_chunks=1)
    position_int = [
        [1, 10, 120, 30, 58],
        [1, 10, 121, 60, 88],
        [2, 12, 130, 32, 70],
    ]
    _chunk(
        db_session,
        file,
        chunk_index=0,
        text="multi-line position chunk",
        chunk_id="multi-line-position",
        position_int=position_int,
    )

    response = workspace_file_client.get(
        f"/workspaces/{workspace.id}/files/{file.id}/chunks"
    )

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    item = data["items"][0]
    assert item["workspace_id"] == workspace.id
    assert item["file_id"] == file.id
    assert item["position_int"] == position_int
    assert item["positions"] == position_int


def test_list_workspace_file_chunks_filters_invalid_positions_and_uses_bbox_fallback(
    workspace_file_client: TestClient,
    db_session: Session,
    workspace: Workspace,
    owner: User,
) -> None:
    file = _file(db_session, workspace, owner, "/docs/report.pdf", total_chunks=2)
    _chunk(
        db_session,
        file,
        chunk_index=0,
        text="has positions",
        chunk_id="positions",
        position_int=[
            [1, 10, 120, 30, 58],
            ["bad", 10, 120, 30, 58],
            [2, "11", 121, 31.0, 59.0],
            [3, 1, 2, 3, 4, 5],
        ],
    )
    _chunk(
        db_session,
        file,
        chunk_index=1,
        text="fallback",
        chunk_id="fallback",
        position_int=[["bad", 10, 120, 30, 58]],
        page=5,
        bbox_x0=10.2,
        bbox_y0=30.8,
        bbox_x1=120.0,
        bbox_y1=58.9,
        source_char_start=7,
        source_char_end=19,
    )

    response = workspace_file_client.get(
        f"/workspaces/{workspace.id}/files/{file.id}/chunks"
    )

    assert response.status_code == 200
    items = response.json()["items"]
    assert items[0]["position_int"] == [[1, 10, 120, 30, 58], [2, 11, 121, 31, 59]]
    assert items[0]["positions"] == items[0]["position_int"]
    assert items[1]["position_int"] == [[5, 10, 120, 30, 58]]
    assert items[1]["positions"] == items[1]["position_int"]
    assert items[1]["page"] == 5
    assert items[1]["source_char_start"] == 7
    assert items[1]["source_char_end"] == 19


def test_list_workspace_file_chunks_returns_400_for_directory(
    workspace_file_client: TestClient,
    db_session: Session,
    workspace: Workspace,
    owner: User,
) -> None:
    directory = _file(
        db_session,
        workspace,
        owner,
        "/docs",
        is_directory=True,
    )

    response = workspace_file_client.get(
        f"/workspaces/{workspace.id}/files/{directory.id}/chunks"
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST


def test_list_workspace_file_chunks_returns_403_without_read_permission(
    db_session: Session,
    workspace: Workspace,
    owner: User,
    other_user: User,
) -> None:
    file = _file(db_session, workspace, owner, "/docs/report.txt")
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_active_user] = lambda: other_user

    with TestClient(app) as client:
        response = client.get(f"/workspaces/{workspace.id}/files/{file.id}/chunks")

    app.dependency_overrides.clear()
    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_list_workspace_file_chunks_returns_404_for_workspace_file_mismatch(
    workspace_file_client: TestClient,
    db_session: Session,
    workspace: Workspace,
    owner: User,
) -> None:
    other_ws = _workspace(db_session, owner, "mismatch-workspace")
    file = _file(db_session, other_ws, owner, "/docs/report.txt")

    response = workspace_file_client.get(
        f"/workspaces/{workspace.id}/files/{file.id}/chunks"
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND


def test_get_workspace_file_content_streams_bytes_and_closes_storage_stream(
    monkeypatch: pytest.MonkeyPatch,
    workspace_file_client: TestClient,
    db_session: Session,
    workspace: Workspace,
    owner: User,
) -> None:
    file = _file(
        db_session,
        workspace,
        owner,
        "/docs/report.txt",
        mime_type="text/plain",
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

    response = workspace_file_client.get(
        f"/workspaces/{workspace.id}/files/{file.id}/content"
    )

    assert response.status_code == 200
    assert response.content == b"hello world"
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["content-disposition"] == 'inline; filename="report.txt"'
    assert calls == [("workspace-file", "docs/report.txt")]
    assert fake_stream.closed is True
    assert fake_stream.released is True


def test_get_workspace_file_content_returns_400_for_directory(
    workspace_file_client: TestClient,
    db_session: Session,
    workspace: Workspace,
    owner: User,
) -> None:
    directory = _file(db_session, workspace, owner, "/docs", is_directory=True)

    response = workspace_file_client.get(
        f"/workspaces/{workspace.id}/files/{directory.id}/content"
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST


def test_get_workspace_file_preview_returns_preview_payload(
    monkeypatch: pytest.MonkeyPatch,
    workspace_file_client: TestClient,
    db_session: Session,
    workspace: Workspace,
    owner: User,
) -> None:
    file = _file(
        db_session,
        workspace,
        owner,
        "/docs/report.html",
        mime_type="text/html",
    )
    preview_calls: list[tuple[bytes, str, str]] = []

    class FakeStorage:
        def read_object_bytes(self, bucket: str, object_key: str) -> bytes:
            assert (bucket, object_key) == ("workspace-file", "docs/report.html")
            return b"<p>source</p>"

    def fake_preview(data: bytes, mime_type: str, filename: str):
        preview_calls.append((data, mime_type, filename))
        return "html", "<div>preview</div>"

    monkeypatch.setattr("openrag.api.workspace_file_api.MinioStorage", FakeStorage)
    monkeypatch.setattr("openrag.api.workspace_file_api.build_file_preview", fake_preview)

    response = workspace_file_client.get(
        f"/workspaces/{workspace.id}/files/{file.id}/preview"
    )

    assert response.status_code == 200
    assert response.json() == {"format": "html", "content": "<div>preview</div>"}
    assert preview_calls == [(b"<p>source</p>", "text/html", "report.html")]


def test_get_workspace_file_preview_returns_400_for_directory(
    workspace_file_client: TestClient,
    db_session: Session,
    workspace: Workspace,
    owner: User,
) -> None:
    directory = _file(db_session, workspace, owner, "/docs", is_directory=True)

    response = workspace_file_client.get(
        f"/workspaces/{workspace.id}/files/{directory.id}/preview"
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST


def test_get_workspace_file_preview_maps_value_error_to_415(
    monkeypatch: pytest.MonkeyPatch,
    workspace_file_client: TestClient,
    db_session: Session,
    workspace: Workspace,
    owner: User,
) -> None:
    file = _file(db_session, workspace, owner, "/docs/report.bin")

    class FakeStorage:
        def read_object_bytes(self, bucket: str, object_key: str) -> bytes:
            return b"data"

    def fake_preview(data: bytes, mime_type: str, filename: str):
        raise ValueError("unsupported")

    monkeypatch.setattr("openrag.api.workspace_file_api.MinioStorage", FakeStorage)
    monkeypatch.setattr("openrag.api.workspace_file_api.build_file_preview", fake_preview)

    response = workspace_file_client.get(
        f"/workspaces/{workspace.id}/files/{file.id}/preview"
    )

    assert response.status_code == status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
    assert response.json()["detail"] == "unsupported"


def test_get_workspace_file_preview_maps_unexpected_error_to_500(
    monkeypatch: pytest.MonkeyPatch,
    workspace_file_client: TestClient,
    db_session: Session,
    workspace: Workspace,
    owner: User,
) -> None:
    file = _file(db_session, workspace, owner, "/docs/report.txt")

    class FakeStorage:
        def read_object_bytes(self, bucket: str, object_key: str) -> bytes:
            return b"data"

    def fake_preview(data: bytes, mime_type: str, filename: str):
        raise RuntimeError("boom")

    monkeypatch.setattr("openrag.api.workspace_file_api.MinioStorage", FakeStorage)
    monkeypatch.setattr("openrag.api.workspace_file_api.build_file_preview", fake_preview)

    response = workspace_file_client.get(
        f"/workspaces/{workspace.id}/files/{file.id}/preview"
    )

    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert response.json()["detail"] == "Failed to generate preview"


def test_get_workspace_file_chunk_source_returns_latest_completed_artifact(
    monkeypatch: pytest.MonkeyPatch,
    workspace_file_client: TestClient,
    db_session: Session,
    workspace: Workspace,
    owner: User,
) -> None:
    file = _file(db_session, workspace, owner, "/docs/report.txt")
    old_artifact = DocumentParseArtifact(
        artifact_id="old-artifact",
        file_id=file.id,
        workspace_id=workspace.id,
        source_doc_hash="hash-old",
        parser_name="TxtParserAdapter",
        parser_version="1.0",
        canonical_json_bucket=workspace.slug,
        canonical_json_object_key="old/canonical.json",
        canonical_json_size_bytes=2,
        canonical_md_bucket=workspace.slug,
        canonical_md_object_key="old/canonical.md",
        canonical_md_size_bytes=3,
        status="completed",
    )
    latest_artifact = DocumentParseArtifact(
        artifact_id="latest-artifact",
        file_id=file.id,
        workspace_id=workspace.id,
        source_doc_hash="hash-new",
        parser_name="TxtParserAdapter",
        parser_version="1.0",
        canonical_json_bucket=workspace.slug,
        canonical_json_object_key="latest/canonical.json",
        canonical_json_size_bytes=2,
        canonical_md_bucket=workspace.slug,
        canonical_md_object_key="latest/canonical.md",
        canonical_md_size_bytes=16,
        status="completed",
    )
    db_session.add_all([old_artifact, latest_artifact])
    db_session.commit()

    calls: list[tuple[str, str]] = []

    class FakeStorage:
        def read_object_bytes(self, bucket: str, object_key: str) -> bytes:
            calls.append((bucket, object_key))
            return "Alpha\nBeta".encode("utf-8")

    monkeypatch.setattr("openrag.api.workspace_file_api.MinioStorage", FakeStorage)

    response = workspace_file_client.get(
        f"/workspaces/{workspace.id}/files/{file.id}/chunk-source"
    )

    assert response.status_code == 200
    assert response.json() == {"format": "text", "content": "Alpha\nBeta"}
    assert calls == [(workspace.slug, "latest/canonical.md")]


def test_get_workspace_file_chunk_source_returns_404_without_artifact(
    workspace_file_client: TestClient,
    db_session: Session,
    workspace: Workspace,
    owner: User,
) -> None:
    file = _file(db_session, workspace, owner, "/docs/report.txt")

    response = workspace_file_client.get(
        f"/workspaces/{workspace.id}/files/{file.id}/chunk-source"
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "Canonical chunk source not found"


def test_get_workspace_file_chunk_source_returns_400_for_directory(
    workspace_file_client: TestClient,
    db_session: Session,
    workspace: Workspace,
    owner: User,
) -> None:
    directory = _file(db_session, workspace, owner, "/docs", is_directory=True)

    response = workspace_file_client.get(
        f"/workspaces/{workspace.id}/files/{directory.id}/chunk-source"
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.parametrize(
    ("processing_status", "expected"),
    [
        (ProcessingStatus.pending, "processing"),
        (ProcessingStatus.parsing, "processing"),
        (ProcessingStatus.building_hierarchy, "processing"),
        (ProcessingStatus.embedding, "processing"),
        (ProcessingStatus.completed, "done"),
        (ProcessingStatus.failed, "failed"),
    ],
)
def test_simple_status_maps_processing_statuses(
    processing_status: ProcessingStatus, expected: str
) -> None:
    file = File(
        uri="/x.txt",
        name="x.txt",
        owner_id=1,
        workspace_id=1,
        processing_status=processing_status,
    )

    assert _simple_status(file) == expected


def test_content_disposition_inline_for_basic_filename() -> None:
    assert _content_disposition_inline("report.txt") == 'inline; filename="report.txt"'


def test_content_disposition_inline_sanitizes_quotes_and_encodes_non_ascii() -> None:
    value = _content_disposition_inline('报告"一".pdf')

    assert value.startswith('inline; filename="__\'_\'.pdf"')
    assert "filename*=UTF-8''%E6%8A%A5%E5%91%8A%27%E4%B8%80%27.pdf" in value


def test_read_file_bytes_from_workspace_uses_workspace_slug_and_lstripped_uri(
    monkeypatch: pytest.MonkeyPatch, db_session: Session, workspace: Workspace, owner: User
) -> None:
    file = _file(db_session, workspace, owner, "/nested/a.txt")
    calls: list[tuple[str, str]] = []

    class FakeStorage:
        def read_object_bytes(self, bucket: str, object_key: str) -> bytes:
            calls.append((bucket, object_key))
            return b"abc"

    monkeypatch.setattr("openrag.api.workspace_file_api.MinioStorage", FakeStorage)

    assert _read_file_bytes_from_workspace(file, workspace) == b"abc"
    assert calls == [("workspace-file", "nested/a.txt")]


def test_open_file_stream_from_workspace_uses_workspace_slug_and_lstripped_uri(
    monkeypatch: pytest.MonkeyPatch, db_session: Session, workspace: Workspace, owner: User
) -> None:
    file = _file(db_session, workspace, owner, "/nested/a.txt")
    stream = object()
    calls: list[tuple[str, str]] = []

    class FakeStorage:
        def open_object_stream(self, bucket: str, object_key: str):
            calls.append((bucket, object_key))
            return stream

    monkeypatch.setattr("openrag.api.workspace_file_api.MinioStorage", FakeStorage)

    assert _open_file_stream_from_workspace(file, workspace) is stream
    assert calls == [("workspace-file", "nested/a.txt")]


def test_storage_helper_raises_404_when_minio_read_fails(
    monkeypatch: pytest.MonkeyPatch, db_session: Session, workspace: Workspace, owner: User
) -> None:
    file = _file(db_session, workspace, owner, "/missing.txt")

    class FakeStorage:
        def read_object_bytes(self, bucket: str, object_key: str) -> bytes:
            raise RuntimeError("missing")

    monkeypatch.setattr("openrag.api.workspace_file_api.MinioStorage", FakeStorage)

    with pytest.raises(HTTPException) as exc:
        _read_file_bytes_from_workspace(file, workspace)

    assert exc.value.status_code == status.HTTP_404_NOT_FOUND
