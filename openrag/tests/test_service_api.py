"""Integration tests for /service/v1 routes (service token auth)."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import openrag.models  # noqa: F401 — register all mappers on Base.metadata
import openrag.config as config_module
from openrag.api.deps import get_db
from openrag.api.main import app
from openrag.api.search_api import SearchResponse, SearchResult
from openrag.api.service_api import _resolve_scope_paths
from openrag.models import Base, DocumentChunk, File, ServiceToken, ServiceTokenWorkspace, User, Workspace
from openrag.models.file import ProcessingStatus
from openrag.models.task import Task, TaskStatus
from openrag.security import hash_password
from openrag.services.file_ingest import ensure_directory_path
from openrag.services.file_deletion import FileStorageCleanupError
from openrag.services.preview_token_service import decode_preview_token
from openrag.storage.minio_storage import MinioStorage

TEST_DATABASE_URL = "sqlite:///:memory:"

engine = create_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _assert_initial_task_quota(body: dict) -> None:
    assert body["processing_status"] == "pending"
    assert body["processing_error"] is None
    assert body["task_id"] is not None
    assert body["task_uuid"] is not None
    assert body["task_status"] == "pending"
    assert body["task_progress"] == 0
    assert body["retry_count"] == 0
    assert body["max_retries"] == 3
    assert body["remaining_retries"] == 3
    assert body["can_retry"] is False


@pytest.fixture(autouse=True)
def _stub_app_startup():
    """Make app startup hit the test's in-memory SQLite instead of a real Postgres so
    ``TestClient`` tests run without external infra. Startup calls ``get_db()`` directly
    (bypassing the dependency override) and runs ``SELECT 1``; pointing ``get_engine`` at
    the test engine keeps that from blocking on an unreachable DB. The background
    scheduler is stubbed so no 60s job thread touches the shared in-memory DB.
    """
    import openrag.database

    # Poison the lazy engine singleton so EVERY get_engine() caller (init_db and
    # deps.get_db, whatever the import path) resolves to the in-memory SQLite engine.
    with patch.object(openrag.database, "_engine", engine), patch(
        "openrag.scheduler.start_scheduler", lambda: None
    ), patch("openrag.scheduler.stop_scheduler", lambda: None):
        yield


@pytest.fixture(scope="function")
def db() -> Session:
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()
    yield session
    session.close()
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def owner(db: Session) -> User:
    u = User(
        username="svc_owner",
        email="svc_owner@example.com",
        password_hash=hash_password("pw"),
        full_name="S",
        is_active=True,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture
def workspace(db: Session, owner: User) -> Workspace:
    ws = Workspace(name="SvcApiWS", slug="svc-api-ws", owner_id=owner.id)
    db.add(ws)
    db.commit()
    db.refresh(ws)
    return ws


@pytest.fixture
def service_token_headers(db: Session, owner: User, workspace: Workspace) -> dict[str, str]:
    tok = ServiceToken(
        secret="sk-integration-test",
        name="t",
        created_by_user_id=owner.id,
    )
    db.add(tok)
    db.commit()
    db.refresh(tok)
    db.add(ServiceTokenWorkspace(token_id=tok.id, workspace_id=workspace.id, permission="read"))
    db.commit()
    return {"X-OpenRag-Token": "sk-integration-test"}


@pytest.fixture
def service_token_write_headers(db: Session, owner: User, workspace: Workspace) -> dict[str, str]:
    tok = ServiceToken(
        secret="sk-write-test",
        name="w",
        created_by_user_id=owner.id,
    )
    db.add(tok)
    db.commit()
    db.refresh(tok)
    db.add(ServiceTokenWorkspace(token_id=tok.id, workspace_id=workspace.id, permission="write"))
    db.commit()
    return {"X-OpenRag-Token": "sk-write-test"}


@pytest.fixture
def client(db: Session):
    def override_get_db():
        try:
            yield db
        finally:
            db.rollback()

    prev = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    if prev is not None:
        app.dependency_overrides[get_db] = prev
    else:
        app.dependency_overrides.pop(get_db, None)


def _root(db: Session, ws: Workspace, owner: User) -> None:
    db.add(
        File(
            uri="/",
            name="root",
            owner_id=owner.id,
            workspace_id=ws.id,
            is_directory=True,
            size=0,
        )
    )
    db.commit()


def _file(db: Session, ws: Workspace, owner: User, uri: str, *, is_directory: bool = False) -> File:
    f = File(
        uri=uri,
        name=uri.rsplit("/", 1)[-1] or "root",
        owner_id=owner.id,
        workspace_id=ws.id,
        is_directory=is_directory,
        size=0 if is_directory else 3,
        mime_type=None if is_directory else "text/plain",
    )
    db.add(f)
    db.commit()
    db.refresh(f)
    return f


def _chunk(
    db: Session,
    ws: Workspace,
    file: File,
    chunk_id: str,
    *,
    chunk_index: int = 0,
) -> DocumentChunk:
    c = DocumentChunk(
        id=db.query(DocumentChunk).count() + 1,
        file_id=file.id,
        workspace_id=ws.id,
        chunk_id=chunk_id,
        chunk_index=chunk_index,
        object_key=f"chunks/{chunk_id}.md",
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _failed_process_task(
    db: Session,
    ws: Workspace,
    owner: User,
    file: File,
    *,
    task_id: str,
    retry_count: int,
    max_retries: int,
) -> Task:
    task = Task(
        task_id=task_id,
        workspace_id=ws.id,
        user_id=owner.id,
        file_id=file.id,
        task_type="process_document",
        status=TaskStatus.FAILURE,
        progress=65,
        retry_count=retry_count,
        max_retries=max_retries,
        error="milvus timeout",
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def _assert_retry_requeued(body: dict, file: File, task: Task) -> None:
    assert body["id"] == file.id
    assert body["path"] == file.uri
    assert body["processing_status"] == "pending"
    assert body["processing_error"] is None
    assert body["task_id"] == task.id
    assert body["task_uuid"] == task.task_id
    assert body["task_status"] == "pending"
    assert body["task_progress"] == 0
    assert body["retry_count"] == 1
    assert body["max_retries"] == 3
    assert body["remaining_retries"] == 2
    assert body["can_retry"] is False


def test_service_list_workspaces_requires_token(client: TestClient, workspace: Workspace) -> None:
    r = client.get("/service/v1/workspaces")
    assert r.status_code == 401


def test_service_list_workspaces_ok(
    client: TestClient, workspace: Workspace, service_token_headers
) -> None:
    r = client.get("/service/v1/workspaces", headers=service_token_headers)
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["id"] == workspace.id
    assert item["name"] == workspace.name
    assert item["slug"] == workspace.slug
    assert item["description"] == workspace.description
    assert item["permission"] == "read"


def test_service_list_workspaces_write_token(
    client: TestClient, workspace: Workspace, service_token_write_headers
) -> None:
    r = client.get("/service/v1/workspaces", headers=service_token_write_headers)
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["permission"] == "write"


def test_service_tree_requires_token(client: TestClient, workspace: Workspace) -> None:
    r = client.get(f"/service/v1/workspaces/{workspace.name}/tree")
    assert r.status_code == 401


def test_service_tree_ok(
    client: TestClient, db: Session, workspace: Workspace, owner: User, service_token_headers
) -> None:
    _root(db, workspace, owner)
    db.add(
        File(
            uri="/docs",
            name="docs",
            owner_id=owner.id,
            workspace_id=workspace.id,
            is_directory=True,
            size=0,
        )
    )
    db.commit()

    r = client.get(
        f"/service/v1/workspaces/{workspace.name}/tree",
        params={"path_prefix": "/"},
        headers=service_token_headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "/"
    assert body["kind"] == "dir"


def test_service_children_ok(
    client: TestClient, db: Session, workspace: Workspace, owner: User, service_token_headers
) -> None:
    _root(db, workspace, owner)
    db.add(
        File(
            uri="/docs",
            name="docs",
            owner_id=owner.id,
            workspace_id=workspace.id,
            is_directory=True,
            size=0,
        )
    )
    db.add(
        File(
            uri="/docs/a.txt",
            name="a.txt",
            owner_id=owner.id,
            workspace_id=workspace.id,
            is_directory=False,
            size=3,
            mime_type="text/plain",
        )
    )
    db.commit()

    r = client.get(
        f"/service/v1/workspaces/{workspace.name}/children",
        params={"path": "/docs"},
        headers=service_token_headers,
    )
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 1
    assert items[0]["path"] == "/docs/a.txt"


def test_service_entries_by_prefix_ok(
    client: TestClient, db: Session, workspace: Workspace, owner: User, service_token_headers
) -> None:
    _root(db, workspace, owner)
    db.add(
        File(
            uri="/docs",
            name="docs",
            owner_id=owner.id,
            workspace_id=workspace.id,
            is_directory=True,
            size=0,
        )
    )
    db.add(
        File(
            uri="/docs/a.txt",
            name="a.txt",
            owner_id=owner.id,
            workspace_id=workspace.id,
            is_directory=False,
            size=3,
            mime_type="text/plain",
        )
    )
    db.add(
        File(
            uri="/images",
            name="images",
            owner_id=owner.id,
            workspace_id=workspace.id,
            is_directory=True,
            size=0,
        )
    )
    db.commit()

    r = client.get(
        f"/service/v1/workspaces/{workspace.name}/entries/by-prefix",
        params={"url_prefix": "/docs"},
        headers=service_token_headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["url_prefix"] == "/docs"
    assert body["total"] == 2
    assert body["items"][0]["path"] == "/docs"
    assert body["items"][1]["path"] == "/docs/a.txt"


def test_service_document_by_path_ok(
    client: TestClient, db: Session, workspace: Workspace, owner: User, service_token_headers
) -> None:
    _root(db, workspace, owner)
    db.add(
        File(
            uri="/readme.md",
            name="readme.md",
            owner_id=owner.id,
            workspace_id=workspace.id,
            is_directory=False,
            size=5,
            mime_type="text/markdown",
        )
    )
    db.commit()

    r = client.get(
        f"/service/v1/workspaces/{workspace.name}/documents/by-path",
        params={"path": "/readme.md"},
        headers=service_token_headers,
    )
    assert r.status_code == 200
    assert r.json()["path"] == "/readme.md"
    assert r.json()["name"] == "readme.md"


def test_service_sync_delete_returns_503_when_storage_cleanup_fails(
    client: TestClient,
    db: Session,
    workspace: Workspace,
    owner: User,
    service_token_write_headers,
) -> None:
    file = File(
        uri="/delete-me.md",
        name="delete-me.md",
        owner_id=owner.id,
        workspace_id=workspace.id,
        is_directory=False,
        size=5,
        mime_type="text/markdown",
    )
    db.add(file)
    db.commit()
    db.refresh(file)

    with patch(
        "openrag.api.service_api.delete_file_with_storage",
        side_effect=FileStorageCleanupError(file.id),
    ):
        response = client.delete(
            f"/service/v1/workspaces/{workspace.name}/documents/by-path",
            params={"path": file.uri, "background": "false"},
            headers=service_token_write_headers,
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "File storage cleanup is incomplete"
    db.expire_all()
    assert db.get(File, file.id) is not None


def test_service_document_by_path_includes_retry_budget(
    client: TestClient, db: Session, workspace: Workspace, owner: User, service_token_headers
) -> None:
    file = _file(db, workspace, owner, "/docs/fail.txt")
    file.processing_status = ProcessingStatus.failed
    file.processing_error = "milvus timeout"
    task = _failed_process_task(
        db,
        workspace,
        owner,
        file,
        task_id="by-path-failed-task",
        retry_count=1,
        max_retries=3,
    )

    r = client.get(
        f"/service/v1/workspaces/{workspace.name}/documents/by-path",
        params={"path": "/docs/fail.txt"},
        headers=service_token_headers,
    )

    assert r.status_code == 200
    body = r.json()
    assert body["id"] == file.id
    assert body["path"] == "/docs/fail.txt"
    assert body["name"] == "fail.txt"
    assert body["size"] == 3
    assert body["mime_type"] == "text/plain"
    assert body["owner_id"] == owner.id
    assert body["parser_type"] == file.parser_type
    assert body["created_at"] == file.created_at.isoformat()
    assert body["processing_status"] == "failed"
    assert body["processing_error"] == "milvus timeout"
    assert body["task_id"] == task.id
    assert body["task_uuid"] == "by-path-failed-task"
    assert body["task_status"] == "failure"
    assert body["task_progress"] == 65
    assert body["retry_count"] == 1
    assert body["max_retries"] == 3
    assert body["remaining_retries"] == 2
    assert body["can_retry"] is True


def test_service_document_by_tag_includes_retry_budget(
    client: TestClient, db: Session, workspace: Workspace, owner: User, service_token_headers
) -> None:
    file = _file(db, workspace, owner, "/docs/fail.txt")
    file.tag = "retry-tag"
    file.processing_status = ProcessingStatus.failed
    file.processing_error = "milvus timeout"
    task = _failed_process_task(
        db,
        workspace,
        owner,
        file,
        task_id="by-tag-failed-task",
        retry_count=2,
        max_retries=3,
    )

    r = client.get(
        f"/service/v1/workspaces/{workspace.name}/documents/by-tag",
        params={"tag": "retry-tag"},
        headers=service_token_headers,
    )

    assert r.status_code == 200
    body = r.json()
    assert body["tag"] == "retry-tag"
    assert body["processing_error"] == "milvus timeout"
    assert body["task_id"] == task.id
    assert body["task_uuid"] == "by-tag-failed-task"
    assert body["task_status"] == "failure"
    assert body["task_progress"] == 65
    assert body["retry_count"] == 2
    assert body["max_retries"] == 3
    assert body["remaining_retries"] == 1
    assert body["can_retry"] is True


def test_service_search_by_name_includes_retry_budget_for_failed_document(
    client: TestClient, db: Session, workspace: Workspace, owner: User, service_token_headers
) -> None:
    file = _file(db, workspace, owner, "/docs/fail.txt")
    file.tag = "failed-doc"
    file.processing_status = ProcessingStatus.failed
    file.processing_error = "milvus timeout"
    task = Task(
        task_id="failed-task",
        workspace_id=workspace.id,
        user_id=owner.id,
        file_id=file.id,
        task_type="process_document",
        status=TaskStatus.FAILURE,
        progress=65,
        retry_count=1,
        max_retries=3,
    )
    db.add(task)
    db.commit()

    r = client.get(
        f"/service/v1/workspaces/{workspace.name}/documents/search-by-name",
        params={"path_prefix": "/docs"},
        headers=service_token_headers,
    )

    assert r.status_code == 200
    item = r.json()["items"][0]
    assert item["id"] == file.id
    assert item["path"] == "/docs/fail.txt"
    assert item["name"] == "fail.txt"
    assert item["size"] == 3
    assert item["mime_type"] == "text/plain"
    assert item["tag"] == "failed-doc"
    assert item["processing_status"] == "failed"
    assert item["updated_at"] == file.updated_at.isoformat()
    assert item["processing_error"] == "milvus timeout"
    assert item["task_id"] == task.id
    assert item["task_uuid"] == "failed-task"
    assert item["task_status"] == "failure"
    assert item["task_progress"] == 65
    assert item["retry_count"] == 1
    assert item["max_retries"] == 3
    assert item["remaining_retries"] == 2
    assert item["can_retry"] is True


def test_service_search_by_name_exhausted_retry_budget_cannot_retry(
    client: TestClient, db: Session, workspace: Workspace, owner: User, service_token_headers
) -> None:
    file = _file(db, workspace, owner, "/docs/fail.txt")
    file.processing_status = ProcessingStatus.failed
    file.processing_error = "milvus timeout"
    task = Task(
        task_id="exhausted-task",
        workspace_id=workspace.id,
        user_id=owner.id,
        file_id=file.id,
        task_type="process_document",
        status=TaskStatus.FAILURE,
        progress=65,
        retry_count=3,
        max_retries=3,
    )
    db.add(task)
    db.commit()

    r = client.get(
        f"/service/v1/workspaces/{workspace.name}/documents/search-by-name",
        params={"path_prefix": "/docs"},
        headers=service_token_headers,
    )

    assert r.status_code == 200
    item = r.json()["items"][0]
    assert item["processing_status"] == "failed"
    assert item["processing_error"] == "milvus timeout"
    assert item["task_id"] == task.id
    assert item["task_uuid"] == "exhausted-task"
    assert item["task_status"] == "failure"
    assert item["task_progress"] == 65
    assert item["retry_count"] == 3
    assert item["max_retries"] == 3
    assert item["remaining_retries"] == 0
    assert item["can_retry"] is False


def test_service_retry_document_by_id_resets_file_and_requeues_task(
    client: TestClient,
    db: Session,
    workspace: Workspace,
    owner: User,
    service_token_write_headers,
) -> None:
    file = _file(db, workspace, owner, "/docs/fail.txt")
    file.processing_status = ProcessingStatus.failed
    file.processing_error = "milvus timeout"
    task = _failed_process_task(
        db,
        workspace,
        owner,
        file,
        task_id="retry-by-id-task",
        retry_count=0,
        max_retries=3,
    )
    db.commit()

    r = client.post(
        f"/service/v1/workspaces/{workspace.name}/documents/{file.id}/retry",
        headers=service_token_write_headers,
    )

    assert r.status_code == 200
    _assert_retry_requeued(r.json(), file, task)
    db.refresh(file)
    db.refresh(task)
    assert file.processing_status == ProcessingStatus.pending
    assert file.processing_error is None
    assert task.status == TaskStatus.PENDING
    assert task.retry_count == 1
    assert task.progress == 0
    assert task.error is None


def test_service_retry_document_by_id_exhausted_returns_409(
    client: TestClient,
    db: Session,
    workspace: Workspace,
    owner: User,
    service_token_write_headers,
) -> None:
    file = _file(db, workspace, owner, "/docs/fail.txt")
    file.processing_status = ProcessingStatus.failed
    file.processing_error = "milvus timeout"
    task = _failed_process_task(
        db,
        workspace,
        owner,
        file,
        task_id="retry-exhausted-task",
        retry_count=3,
        max_retries=3,
    )
    db.commit()

    r = client.post(
        f"/service/v1/workspaces/{workspace.name}/documents/{file.id}/retry",
        headers=service_token_write_headers,
    )

    assert r.status_code == 409
    db.refresh(file)
    db.refresh(task)
    assert file.processing_status == ProcessingStatus.failed
    assert file.processing_error == "milvus timeout"
    assert task.status == TaskStatus.FAILURE
    assert task.retry_count == 3


def test_service_retry_document_by_id_requires_write_permission(
    client: TestClient,
    db: Session,
    workspace: Workspace,
    owner: User,
    service_token_headers,
) -> None:
    file = _file(db, workspace, owner, "/docs/fail.txt")
    file.processing_status = ProcessingStatus.failed
    file.processing_error = "milvus timeout"
    _failed_process_task(
        db,
        workspace,
        owner,
        file,
        task_id="retry-readonly-task",
        retry_count=0,
        max_retries=3,
    )
    db.commit()

    r = client.post(
        f"/service/v1/workspaces/{workspace.name}/documents/{file.id}/retry",
        headers=service_token_headers,
    )

    assert r.status_code == 403


def test_service_retry_document_by_path_resets_file_and_requeues_task(
    client: TestClient,
    db: Session,
    workspace: Workspace,
    owner: User,
    service_token_write_headers,
) -> None:
    file = _file(db, workspace, owner, "/docs/fail.txt")
    file.processing_status = ProcessingStatus.failed
    file.processing_error = "milvus timeout"
    task = _failed_process_task(
        db,
        workspace,
        owner,
        file,
        task_id="retry-by-path-task",
        retry_count=0,
        max_retries=3,
    )
    db.commit()

    r = client.post(
        f"/service/v1/workspaces/{workspace.name}/documents/by-path/retry",
        params={"path": "/docs/fail.txt"},
        headers=service_token_write_headers,
    )

    assert r.status_code == 200
    _assert_retry_requeued(r.json(), file, task)
    db.refresh(file)
    db.refresh(task)
    assert file.processing_status == ProcessingStatus.pending
    assert file.processing_error is None
    assert task.status == TaskStatus.PENDING
    assert task.retry_count == 1
    assert task.progress == 0
    assert task.error is None


def test_service_wrong_workspace_name_404(
    client: TestClient, service_token_headers
) -> None:
    r = client.get(
        "/service/v1/workspaces/DoesNotExistWS/tree",
        params={"path_prefix": "/"},
        headers=service_token_headers,
    )
    assert r.status_code == 404


def test_service_search_requires_token(client: TestClient, workspace: Workspace) -> None:
    r = client.post(
        f"/service/v1/workspaces/{workspace.name}/search",
        json={"query": "hello"},
    )
    assert r.status_code == 401


@patch("openrag.api.service_api._execute_search")
def test_service_search_path_prefix_filter(
    mock_search: MagicMock,
    client: TestClient,
    workspace: Workspace,
    service_token_headers,
) -> None:
    mock_search.return_value = SearchResponse(
        results=[
            SearchResult(text="a", score=1.0, file_id=1, uri="/docs/a.txt"),
            SearchResult(text="b", score=0.9, file_id=2, uri="/other/b.txt"),
        ],
        total=2,
        query_time_ms=1.0,
    )
    r = client.post(
        f"/service/v1/workspaces/{workspace.name}/search",
        json={"query": "hello", "path_prefix": "/docs"},
        headers=service_token_headers,
    )
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 2                        # no longer post-filter trimmed
    assert [h["uri"] for h in data["results"]] == ["/docs/a.txt", "/other/b.txt"]
    mock_search.assert_called_once()
    assert mock_search.call_args[0][2].workspace_id == workspace.id
    assert mock_search.call_args[0][2].paths == ["/docs"]   # path_prefix -> paths
    assert mock_search.call_args.kwargs["workspace_access_prevalidated"] is True


@patch("openrag.api.service_api._execute_search")
def test_service_search_preserves_authorization_503(
    mock_search: MagicMock,
    client: TestClient,
    workspace: Workspace,
    service_token_headers,
) -> None:
    mock_search.side_effect = HTTPException(
        status_code=503,
        detail="Search authorization temporarily unavailable",
    )

    response = client.post(
        f"/service/v1/workspaces/{workspace.name}/search",
        json={"query": "hello"},
        headers=service_token_headers,
    )

    assert response.status_code == 503
    assert response.json() == {
        "detail": "Search authorization temporarily unavailable"
    }


def test_service_multi_workspace_search_requires_token(
    client: TestClient,
    workspace: Workspace,
) -> None:
    r = client.post(
        "/service/v1/workspaces/multi_space/search",
        json={"workspace_names": [workspace.name], "query": "hello"},
    )
    assert r.status_code == 401


def test_service_multi_workspace_search_empty_workspace_names(
    client: TestClient,
    service_token_headers,
) -> None:
    r = client.post(
        "/service/v1/workspaces/multi_space/search",
        json={"workspace_names": [" ", ""], "query": "hello"},
        headers=service_token_headers,
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "workspace_names is required for multi_space search"


@patch("openrag.api.service_api._execute_search")
def test_service_multi_workspace_search_unknown_workspace_skipped(
    mock_search: MagicMock,
    client: TestClient,
    service_token_headers,
) -> None:
    r = client.post(
        "/service/v1/workspaces/multi_space/search",
        json={"workspace_names": ["MissingWS"], "query": "hello"},
        headers=service_token_headers,
    )
    assert r.status_code == 200
    data = r.json()
    assert data["results"] == []
    assert data["workspace_count"] == 0
    assert data["skipped_workspaces"] == [
        {
            "workspace_name": "MissingWS",
            "reason": "not_found",
            "message": "Workspace not found",
        }
    ]
    mock_search.assert_not_called()


@patch("openrag.api.service_api._execute_search")
def test_service_multi_workspace_search_skips_workspace_without_permission(
    mock_search: MagicMock,
    client: TestClient,
    db: Session,
    owner: User,
    workspace: Workspace,
    service_token_headers,
) -> None:
    other = Workspace(name="SvcApiWSB", slug="svc-api-ws-b", owner_id=owner.id)
    db.add(other)
    db.commit()
    db.refresh(other)
    mock_search.return_value = SearchResponse(results=[], total=0, query_time_ms=1.0)

    r = client.post(
        "/service/v1/workspaces/multi_space/search",
        json={"workspace_names": [workspace.name, other.name], "query": "hello"},
        headers=service_token_headers,
    )

    assert r.status_code == 200
    data = r.json()
    assert data["workspace_count"] == 1
    assert data["skipped_workspaces"][0]["workspace_name"] == other.name
    assert data["skipped_workspaces"][0]["reason"] == "permission_denied"
    mock_search.assert_called_once()
    assert mock_search.call_args[0][2].workspace_id == workspace.id


@patch("openrag.api.service_api._execute_search")
def test_service_multi_workspace_search_read_and_write_bindings_can_search(
    mock_search: MagicMock,
    client: TestClient,
    db: Session,
    owner: User,
    workspace: Workspace,
) -> None:
    other = Workspace(name="SvcApiWSB", slug="svc-api-ws-b", owner_id=owner.id)
    db.add(other)
    db.commit()
    db.refresh(other)
    tok = ServiceToken(secret="sk-read-write", name="rw", created_by_user_id=owner.id)
    db.add(tok)
    db.commit()
    db.refresh(tok)
    db.add(ServiceTokenWorkspace(token_id=tok.id, workspace_id=workspace.id, permission="read"))
    db.add(ServiceTokenWorkspace(token_id=tok.id, workspace_id=other.id, permission="write"))
    db.commit()
    mock_search.return_value = SearchResponse(results=[], total=0, query_time_ms=1.0)

    r = client.post(
        "/service/v1/workspaces/multi_space/search",
        json={"workspace_names": [workspace.name, other.name], "query": "hello"},
        headers={"X-OpenRag-Token": "sk-read-write"},
    )

    assert r.status_code == 200
    assert r.json()["workspace_count"] == 2
    assert r.json()["skipped_workspaces"] == []
    assert [call[0][2].workspace_id for call in mock_search.call_args_list] == [
        workspace.id,
        other.id,
    ]


@patch("openrag.api.service_api._execute_search")
def test_service_multi_workspace_search_merges_workspace_fields_sorts_and_limits(
    mock_search: MagicMock,
    client: TestClient,
    db: Session,
    owner: User,
    workspace: Workspace,
) -> None:
    other = Workspace(name="SvcApiWSB", slug="svc-api-ws-b", owner_id=owner.id)
    db.add(other)
    db.commit()
    db.refresh(other)
    tok = ServiceToken(secret="sk-merge", name="merge", created_by_user_id=owner.id)
    db.add(tok)
    db.commit()
    db.refresh(tok)
    db.add(ServiceTokenWorkspace(token_id=tok.id, workspace_id=workspace.id, permission="read"))
    db.add(ServiceTokenWorkspace(token_id=tok.id, workspace_id=other.id, permission="read"))
    db.commit()
    mock_search.side_effect = [
        SearchResponse(
            results=[
                SearchResult(text="a1", score=0.6, file_id=1, uri="/a1.txt"),
                SearchResult(text="a2", score=0.9, file_id=2, uri="/a2.txt"),
            ],
            total=2,
            query_time_ms=5.0,
            l1_llm_applied=False,
            l1_llm_skip_reason="no_l1_hits",
        ),
        SearchResponse(
            results=[SearchResult(text="b1", score=0.8, file_id=3, uri="/b1.txt")],
            total=1,
            query_time_ms=7.0,
            l1_llm_applied=True,
            l1_llm_skip_reason="no_api_key",
        ),
    ]

    r = client.post(
        "/service/v1/workspaces/multi_space/search",
        json={
            "workspace_names": [workspace.name, other.name],
            "query": "hello",
            "top_k": 2,
        },
        headers={"X-OpenRag-Token": "sk-merge"},
    )

    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 2
    assert data["workspace_count"] == 2
    assert data["l1_llm_applied"] is True
    assert data["l1_llm_skip_reason"] == "mixed"
    assert [(hit["text"], hit["score"]) for hit in data["results"]] == [
        ("a2", 0.9),
        ("b1", 0.8),
    ]
    assert data["results"][0]["workspace_id"] == workspace.id
    assert data["results"][0]["workspace_name"] == workspace.name
    assert data["results"][1]["workspace_id"] == other.id
    assert data["results"][1]["workspace_name"] == other.name


@patch("openrag.api.service_api._execute_search")
def test_service_multi_workspace_search_applies_path_prefix_per_workspace(
    mock_search: MagicMock,
    client: TestClient,
    db: Session,
    owner: User,
    workspace: Workspace,
) -> None:
    other = Workspace(name="SvcApiWSB", slug="svc-api-ws-b", owner_id=owner.id)
    db.add(other)
    db.commit()
    db.refresh(other)
    tok = ServiceToken(secret="sk-prefix", name="prefix", created_by_user_id=owner.id)
    db.add(tok)
    db.commit()
    db.refresh(tok)
    db.add(ServiceTokenWorkspace(token_id=tok.id, workspace_id=workspace.id, permission="read"))
    db.add(ServiceTokenWorkspace(token_id=tok.id, workspace_id=other.id, permission="read"))
    db.commit()
    mock_search.side_effect = [
        SearchResponse(
            results=[
                SearchResult(text="a", score=1.0, file_id=1, uri="/docs/a.txt"),
                SearchResult(text="x", score=0.9, file_id=2, uri="/other/x.txt"),
            ],
            total=2,
            query_time_ms=1.0,
            l1_llm_applied=False,
            l1_llm_skip_reason="no_l1_hits",
        ),
        SearchResponse(
            results=[
                SearchResult(text="b", score=0.8, file_id=3, uri="/docs/b.txt"),
                SearchResult(text="y", score=0.7, file_id=4, uri="/other/y.txt"),
            ],
            total=2,
            query_time_ms=1.0,
            l1_llm_applied=True,
            l1_llm_skip_reason="no_api_key",
        ),
    ]

    r = client.post(
        "/service/v1/workspaces/multi_space/search",
        json={
            "workspace_names": [workspace.name, other.name],
            "query": "hello",
            "path_prefix": "/docs",
        },
        headers={"X-OpenRag-Token": "sk-prefix"},
    )

    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 4                         # no longer post-filter trimmed
    assert data["l1_llm_applied"] is True
    assert data["l1_llm_skip_reason"] == "mixed"
    assert [h["uri"] for h in data["results"]] == [
        "/docs/a.txt", "/other/x.txt", "/docs/b.txt", "/other/y.txt",
    ]
    for call in mock_search.call_args_list:
        assert call[0][2].paths == ["/docs"]          # paths forwarded per workspace


@patch("openrag.api.service_api._execute_search")
def test_service_multi_workspace_search_route_not_captured_by_single_workspace_route(
    mock_search: MagicMock,
    client: TestClient,
    workspace: Workspace,
    service_token_headers,
) -> None:
    mock_search.return_value = SearchResponse(results=[], total=0, query_time_ms=1.0)

    r = client.post(
        "/service/v1/workspaces/multi_space/search",
        json={"workspace_names": [workspace.name], "query": "hello"},
        headers=service_token_headers,
    )

    assert r.status_code == 200
    mock_search.assert_called_once()
    assert mock_search.call_args[0][2].workspace_id == workspace.id


def test_service_create_preview_link_ok_returns_url_and_actual_ttl(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    db: Session,
    workspace: Workspace,
    owner: User,
    service_token_headers,
) -> None:
    monkeypatch.setenv("PREVIEW_PUBLIC_WEB_BASE_URL", "https://preview.example.com///")
    monkeypatch.setattr(config_module, "_config", None)
    f = _file(db, workspace, owner, "/docs/a.txt")
    c = _chunk(db, workspace, f, "chunk-preview-ok", chunk_index=42)

    r = client.post(
        f"/service/v1/workspaces/{workspace.name}/preview-links",
        json={
            "file_id": f.id,
            "chunk_id": c.chunk_id,
            "chunk_index": 42,
            "ttl_seconds": 9999,
        },
        headers=service_token_headers,
    )

    assert r.status_code == 200
    body = r.json()
    assert body["preview_url"].startswith(
        "https://preview.example.com/embed/document-preview#token="
    )
    assert body["ttl_seconds"] == 1800
    assert body["expires_at"]
    token = body["preview_url"].split("#token=", 1)[1]
    claims = decode_preview_token(token)
    assert claims.workspace_id == workspace.id
    assert claims.file_id == f.id
    assert claims.chunk_id == c.chunk_id
    assert claims.chunk_index == 42
    assert claims.exp - claims.iat == body["ttl_seconds"]


def test_service_create_preview_link_default_ttl(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    db: Session,
    workspace: Workspace,
    owner: User,
    service_token_headers,
) -> None:
    monkeypatch.setenv("PREVIEW_PUBLIC_WEB_BASE_URL", "https://preview.example.com")
    monkeypatch.setattr(config_module, "_config", None)
    f = _file(db, workspace, owner, "/docs/default.txt")
    c = _chunk(db, workspace, f, "chunk-preview-default", chunk_index=7)

    r = client.post(
        f"/service/v1/workspaces/{workspace.name}/preview-links",
        json={"file_id": f.id, "chunk_id": c.chunk_id},
        headers=service_token_headers,
    )

    assert r.status_code == 200
    assert r.json()["ttl_seconds"] == 900


def test_service_create_preview_link_requires_workspace_read_permission(
    client: TestClient,
    db: Session,
    workspace: Workspace,
    owner: User,
) -> None:
    other = Workspace(name="SvcPreviewOther", slug="svc-preview-other", owner_id=owner.id)
    db.add(other)
    db.commit()
    db.refresh(other)
    tok = ServiceToken(secret="sk-preview-other", name="other", created_by_user_id=owner.id)
    db.add(tok)
    db.commit()
    db.refresh(tok)
    db.add(ServiceTokenWorkspace(token_id=tok.id, workspace_id=other.id, permission="read"))
    db.commit()

    r = client.post(
        f"/service/v1/workspaces/{workspace.name}/preview-links",
        json={"file_id": 1, "chunk_id": "chunk-no-permission"},
        headers={"X-OpenRag-Token": "sk-preview-other"},
    )

    assert r.status_code == 403
    assert r.json()["detail"] == "Token not authorized for this workspace"


def test_service_create_preview_link_rejects_missing_file(
    client: TestClient,
    workspace: Workspace,
    service_token_headers,
) -> None:
    r = client.post(
        f"/service/v1/workspaces/{workspace.name}/preview-links",
        json={"file_id": 999, "chunk_id": "chunk-missing-file"},
        headers=service_token_headers,
    )

    assert r.status_code == 404
    assert r.json()["detail"] == "File not found"


def test_service_create_preview_link_rejects_file_from_other_workspace(
    client: TestClient,
    db: Session,
    workspace: Workspace,
    owner: User,
    service_token_headers,
) -> None:
    other = Workspace(name="SvcPreviewFileOther", slug="svc-preview-file-other", owner_id=owner.id)
    db.add(other)
    db.commit()
    db.refresh(other)
    f = _file(db, other, owner, "/docs/other.txt")

    r = client.post(
        f"/service/v1/workspaces/{workspace.name}/preview-links",
        json={"file_id": f.id, "chunk_id": "chunk-other-file"},
        headers=service_token_headers,
    )

    assert r.status_code == 404
    assert r.json()["detail"] == "File not found"


def test_service_create_preview_link_rejects_directory(
    client: TestClient,
    db: Session,
    workspace: Workspace,
    owner: User,
    service_token_headers,
) -> None:
    directory = _file(db, workspace, owner, "/docs", is_directory=True)

    r = client.post(
        f"/service/v1/workspaces/{workspace.name}/preview-links",
        json={"file_id": directory.id, "chunk_id": "chunk-dir"},
        headers=service_token_headers,
    )

    assert r.status_code == 400
    assert r.json()["detail"] == "File is a directory"


def test_service_create_preview_link_rejects_chunk_from_other_file(
    client: TestClient,
    db: Session,
    workspace: Workspace,
    owner: User,
    service_token_headers,
) -> None:
    requested_file = _file(db, workspace, owner, "/docs/requested.txt")
    other_file = _file(db, workspace, owner, "/docs/other.txt")
    c = _chunk(db, workspace, other_file, "chunk-other-file", chunk_index=2)

    r = client.post(
        f"/service/v1/workspaces/{workspace.name}/preview-links",
        json={"file_id": requested_file.id, "chunk_id": c.chunk_id},
        headers=service_token_headers,
    )

    assert r.status_code == 404
    assert r.json()["detail"] == "Chunk not found"


def test_service_create_preview_link_rejects_chunk_from_other_workspace(
    client: TestClient,
    db: Session,
    workspace: Workspace,
    owner: User,
    service_token_headers,
) -> None:
    other = Workspace(name="SvcPreviewChunkOther", slug="svc-preview-chunk-other", owner_id=owner.id)
    db.add(other)
    db.commit()
    db.refresh(other)
    f = _file(db, workspace, owner, "/docs/a.txt")
    c = DocumentChunk(
        id=db.query(DocumentChunk).count() + 1,
        file_id=f.id,
        workspace_id=other.id,
        chunk_id="chunk-other-workspace",
        chunk_index=3,
        object_key="chunks/chunk-other-workspace.md",
    )
    db.add(c)
    db.commit()
    db.refresh(c)

    r = client.post(
        f"/service/v1/workspaces/{workspace.name}/preview-links",
        json={"file_id": f.id, "chunk_id": c.chunk_id},
        headers=service_token_headers,
    )

    assert r.status_code == 404
    assert r.json()["detail"] == "Chunk not found"


def test_service_create_preview_link_rejects_chunk_index_mismatch(
    client: TestClient,
    db: Session,
    workspace: Workspace,
    owner: User,
    service_token_headers,
) -> None:
    f = _file(db, workspace, owner, "/docs/a.txt")
    c = _chunk(db, workspace, f, "chunk-index-mismatch", chunk_index=5)

    r = client.post(
        f"/service/v1/workspaces/{workspace.name}/preview-links",
        json={"file_id": f.id, "chunk_id": c.chunk_id, "chunk_index": 6},
        headers=service_token_headers,
    )

    assert r.status_code == 400
    assert r.json()["detail"] == "chunk_index does not match chunk"


def test_service_upload_document_201(
    client: TestClient,
    db: Session,
    workspace: Workspace,
    owner: User,
    service_token_write_headers,
) -> None:
    _root(db, workspace, owner)
    db.add(
        File(
            uri="/in",
            name="in",
            owner_id=owner.id,
            workspace_id=workspace.id,
            is_directory=True,
            size=0,
        )
    )
    db.commit()

    with patch.object(MinioStorage, "put_file", return_value=None):
        files = {"file": ("n.txt", b"hello", "text/plain")}
        data = {"path": "/in", "parser_type": "auto"}
        r = client.post(
            f"/service/v1/workspaces/{workspace.name}/documents",
            files=files,
            data=data,
            headers=service_token_write_headers,
        )
    assert r.status_code == 201
    body = r.json()
    assert body["path"] == "/in/n.txt"
    assert body["mime_type"] == "text/plain"
    _assert_initial_task_quota(body)


def test_service_upload_duplicate_409(
    client: TestClient,
    db: Session,
    workspace: Workspace,
    owner: User,
    service_token_write_headers,
) -> None:
    _root(db, workspace, owner)
    db.add(
        File(
            uri="/in",
            name="in",
            owner_id=owner.id,
            workspace_id=workspace.id,
            is_directory=True,
            size=0,
        )
    )
    db.add(
        File(
            uri="/in/dup.txt",
            name="dup.txt",
            owner_id=owner.id,
            workspace_id=workspace.id,
            is_directory=False,
            size=1,
            mime_type="text/plain",
        )
    )
    db.commit()

    with patch.object(MinioStorage, "put_file", return_value=None):
        files = {"file": ("dup.txt", b"x", "text/plain")}
        data = {"path": "/in"}
        r = client.post(
            f"/service/v1/workspaces/{workspace.name}/documents",
            files=files,
            data=data,
            headers=service_token_write_headers,
        )
    assert r.status_code == 409


def test_service_upload_duplicate_failed_file_409_returns_retry_document(
    client: TestClient,
    db: Session,
    workspace: Workspace,
    owner: User,
    service_token_write_headers,
) -> None:
    _root(db, workspace, owner)
    db.add(
        File(
            uri="/in",
            name="in",
            owner_id=owner.id,
            workspace_id=workspace.id,
            is_directory=True,
            size=0,
        )
    )
    file = _file(db, workspace, owner, "/in/dup.txt")
    file.processing_status = ProcessingStatus.failed
    file.processing_error = "milvus timeout"
    task = Task(
        task_id="failed-duplicate-task",
        workspace_id=workspace.id,
        user_id=owner.id,
        file_id=file.id,
        task_type="process_document",
        status=TaskStatus.FAILURE,
        retry_count=0,
        max_retries=3,
    )
    db.add(task)
    db.commit()

    with patch.object(MinioStorage, "put_file", return_value=None):
        files = {"file": ("dup.txt", b"x", "text/plain")}
        data = {"path": "/in"}
        r = client.post(
            f"/service/v1/workspaces/{workspace.name}/documents",
            files=files,
            data=data,
            headers=service_token_write_headers,
        )

    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "file_already_exists_processing_failed"
    assert detail["document"]["path"] == "/in/dup.txt"
    assert detail["document"]["processing_status"] == "failed"
    assert detail["document"]["processing_error"] == "milvus timeout"
    assert detail["document"]["remaining_retries"] == 3
    assert detail["document"]["can_retry"] is True


def test_service_upload_duplicate_completed_file_409_returns_non_retryable_document(
    client: TestClient,
    db: Session,
    workspace: Workspace,
    owner: User,
    service_token_write_headers,
) -> None:
    _root(db, workspace, owner)
    db.add(
        File(
            uri="/in",
            name="in",
            owner_id=owner.id,
            workspace_id=workspace.id,
            is_directory=True,
            size=0,
        )
    )
    file = _file(db, workspace, owner, "/in/dup.txt")
    file.processing_status = ProcessingStatus.completed
    task = Task(
        task_id="stale-failed-duplicate-task",
        workspace_id=workspace.id,
        user_id=owner.id,
        file_id=file.id,
        task_type="process_document",
        status=TaskStatus.FAILURE,
        retry_count=0,
        max_retries=3,
    )
    db.add(task)
    db.commit()

    with patch.object(MinioStorage, "put_file", return_value=None):
        files = {"file": ("dup.txt", b"x", "text/plain")}
        data = {"path": "/in"}
        r = client.post(
            f"/service/v1/workspaces/{workspace.name}/documents",
            files=files,
            data=data,
            headers=service_token_write_headers,
        )

    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "file_already_exists"
    assert detail["document"]["processing_status"] == "completed"
    assert detail["document"]["remaining_retries"] == 3
    assert detail["document"]["can_retry"] is False


def test_service_replace_document_200(
    client: TestClient,
    db: Session,
    workspace: Workspace,
    owner: User,
    service_token_write_headers,
) -> None:
    _root(db, workspace, owner)
    db.add(
        File(
            uri="/rep.txt",
            name="rep.txt",
            owner_id=owner.id,
            workspace_id=workspace.id,
            is_directory=False,
            size=1,
            mime_type="text/plain",
        )
    )
    db.commit()

    with patch.object(MinioStorage, "put_file", return_value=None):
        with patch(
            "openrag.api.files_api.cleanup_file_processing_data",
            lambda *args, **kwargs: None,
        ):
            files = {"file": ("rep.txt", b"new-content-here", "text/plain")}
            data = {"parser_type": "auto"}
            r = client.put(
                f"/service/v1/workspaces/{workspace.name}/documents/by-path",
                params={"path": "/rep.txt"},
                files=files,
                data=data,
                headers=service_token_write_headers,
            )
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "/rep.txt"
    _assert_initial_task_quota(body)


def test_ensure_directory_path_creates_nested_dirs(db: Session, workspace: Workspace, owner: User) -> None:
    leaf = ensure_directory_path(db, workspace, "/personal/u1/kb1")
    assert leaf.uri == "/personal/u1/kb1"
    assert leaf.is_directory is True
    for uri in ("/", "/personal", "/personal/u1", "/personal/u1/kb1"):
        row = (
            db.query(File)
            .filter(File.workspace_id == workspace.id, File.uri == uri, File.is_directory.is_(True))
            .first()
        )
        assert row is not None, f"missing directory row {uri}"


def test_ensure_directory_path_is_idempotent(db: Session, workspace: Workspace, owner: User) -> None:
    ensure_directory_path(db, workspace, "/a/b")
    ensure_directory_path(db, workspace, "/a/b")
    for uri in ("/a", "/a/b"):
        count = (
            db.query(File)
            .filter(File.workspace_id == workspace.id, File.uri == uri)
            .count()
        )
        assert count == 1, f"{uri} duplicated: {count} rows"


def test_ensure_directory_path_seeds_root(db: Session, workspace: Workspace, owner: User) -> None:
    assert (
        db.query(File).filter(File.workspace_id == workspace.id, File.uri == "/").first()
        is None
    )
    ensure_directory_path(db, workspace, "/x")
    root = db.query(File).filter(File.workspace_id == workspace.id, File.uri == "/").first()
    assert root is not None and root.is_directory is True


def test_ensure_directory_path_rejects_file_in_path(db: Session, workspace: Workspace, owner: User) -> None:
    db.add(File(uri="/a", name="a", owner_id=owner.id, workspace_id=workspace.id, is_directory=False, size=3))
    db.commit()
    with pytest.raises(HTTPException) as exc:
        ensure_directory_path(db, workspace, "/a/b")
    assert exc.value.status_code == 409


def test_service_upload_create_dirs_materialises_parents(
    client: TestClient, db: Session, workspace: Workspace, owner: User, service_token_write_headers
) -> None:
    # No directories seeded at all — not even root.
    with patch.object(MinioStorage, "put_file", return_value=None):
        files = {"file": ("n.txt", b"hello", "text/plain")}
        data = {"path": "/personal/u1/kb1", "parser_type": "auto", "create_dirs": "true"}
        r = client.post(
            f"/service/v1/workspaces/{workspace.name}/documents",
            files=files,
            data=data,
            headers=service_token_write_headers,
        )
    assert r.status_code == 201, r.text
    assert r.json()["path"] == "/personal/u1/kb1/n.txt"
    for uri in ("/personal", "/personal/u1", "/personal/u1/kb1"):
        row = (
            db.query(File)
            .filter(File.workspace_id == workspace.id, File.uri == uri, File.is_directory.is_(True))
            .first()
        )
        assert row is not None, f"expected directory {uri} to be created"


def test_service_upload_without_create_dirs_still_400(
    client: TestClient, db: Session, workspace: Workspace, owner: User, service_token_write_headers
) -> None:
    with patch.object(MinioStorage, "put_file", return_value=None):
        files = {"file": ("n.txt", b"hello", "text/plain")}
        data = {"path": "/personal/u1/kb1", "parser_type": "auto"}  # no create_dirs -> strict
        r = client.post(
            f"/service/v1/workspaces/{workspace.name}/documents",
            files=files,
            data=data,
            headers=service_token_write_headers,
        )
    assert r.status_code == 400
    assert r.json()["detail"] == "Parent directory does not exist"


def test_service_upload_create_dirs_file_is_navigable(
    client: TestClient, db: Session, workspace: Workspace, owner: User, service_token_write_headers
) -> None:
    with patch.object(MinioStorage, "put_file", return_value=None):
        files = {"file": ("n.txt", b"hello", "text/plain")}
        data = {"path": "/personal/u1/kb1", "create_dirs": "true"}
        up = client.post(
            f"/service/v1/workspaces/{workspace.name}/documents",
            files=files,
            data=data,
            headers=service_token_write_headers,
        )
        assert up.status_code == 201, up.text
        children = client.get(
            f"/service/v1/workspaces/{workspace.name}/children",
            params={"path": "/personal/u1/kb1"},
            headers=service_token_write_headers,
        )
    assert children.status_code == 200, children.text
    names = [item.get("name") for item in children.json()]
    assert "n.txt" in names


def test_resolve_scope_paths_prefers_paths_over_prefix():
    assert _resolve_scope_paths(["/a"], "/b") == ["/a"]
    assert _resolve_scope_paths([], "/b") == []          # explicit empty scope overrides
    assert _resolve_scope_paths(None, "/b") == ["/b"]    # fall back only when paths is None
    assert _resolve_scope_paths(None, "/") is None
    assert _resolve_scope_paths(None, "  ") is None
    assert _resolve_scope_paths(None, None) is None


@patch("openrag.api.service_api._execute_search")
def test_service_search_forwards_paths_over_prefix(
    mock_search, client, workspace, service_token_headers
):
    mock_search.return_value = SearchResponse(results=[], total=0, query_time_ms=1.0)
    r = client.post(
        f"/service/v1/workspaces/{workspace.name}/search",
        json={"query": "q", "paths": ["/docs"], "path_prefix": "/ignored"},
        headers=service_token_headers,
    )
    assert r.status_code == 200
    assert mock_search.call_args[0][2].paths == ["/docs"]   # paths overrides path_prefix
