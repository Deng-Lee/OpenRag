"""Tests for File Management API"""

import io
import os
import pytest
from datetime import datetime
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from openrag.api.main import app
from openrag.api.deps import get_db, get_current_user
from openrag.models.base import Base
from openrag.models.user import User
from openrag.models.file import File, ProcessingStatus
from openrag.models.task import Task
from openrag.models.workspace import Workspace, WorkspaceMember
from openrag.security import hash_password
from openrag.services.file_ingest import MAX_FILE_SIZE
from openrag.services.file_deletion import FileStorageCleanupError


class FakeMinioStorage:
    def put_file(self, *args, **kwargs):
        return None

    def read_object_bytes(self, *args, **kwargs):
        return b"Test file content"

    def remove_document_hierarchy(self, *args, **kwargs):
        return None


# Test database setup
TEST_DATABASE_URL = "sqlite:///:memory:"

engine = create_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    """Override database dependency for testing"""
    try:
        db = TestingSessionLocal()
        yield db
    finally:
        db.close()


# Override dependencies
app.dependency_overrides[get_db] = override_get_db


@pytest.fixture(scope="function")
def db():
    """Create test database and tables"""
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    yield db
    db.close()
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def test_user(db):
    """Create test user"""
    user = User(
        username="testuser",
        email="test@example.com",
        password_hash=hash_password("testpass123"),
        full_name="Test User",
        is_active=True
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def test_workspace(db, test_user):
    """Workspace with write access for test_user."""
    ws = Workspace(name="UploadWS", slug="upload-ws", owner_id=test_user.id)
    db.add(ws)
    db.commit()
    db.refresh(ws)
    db.add(
        WorkspaceMember(workspace_id=ws.id, user_id=test_user.id, role="write")
    )
    db.commit()
    return ws


@pytest.fixture
def test_user2(db):
    """Create second test user"""
    user = User(
        username="testuser2",
        email="test2@example.com",
        password_hash=hash_password("testpass123"),
        full_name="Test User 2",
        is_active=True
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def test_file(db, test_user, test_workspace):
    """Create test file"""
    file = File(
        uri="/test/file.txt",
        name="file.txt",
        owner_id=test_user.id,
        workspace_id=test_workspace.id,
        is_directory=False,
        size=1024,
        mime_type="text/plain"
    )
    db.add(file)
    db.commit()
    db.refresh(file)
    return file


@pytest.fixture
def test_directory(db, test_user, test_workspace):
    """Create test directory"""
    directory = File(
        uri="/test",
        name="test",
        owner_id=test_user.id,
        workspace_id=test_workspace.id,
        is_directory=True,
        size=0,
        mime_type=None
    )
    db.add(directory)
    db.commit()
    db.refresh(directory)
    return directory


def override_get_current_user_factory(user):
    """Factory to create override function for current user"""
    def override_get_current_user():
        return user
    return override_get_current_user


@pytest.fixture
def client():
    """Create test client"""
    return TestClient(app)


class TestFileUpload:
    """Test file upload endpoint"""

    def test_upload_file_success(self, client, db, test_user, test_workspace, monkeypatch):
        """Test successful file upload"""
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)
        monkeypatch.setattr("openrag.services.file_ingest.MinioStorage", FakeMinioStorage)

        file_content = b"Test file content"
        files = {"file": ("test.txt", io.BytesIO(file_content), "text/plain")}
        data = {"path": "/uploads", "workspace_id": str(test_workspace.id)}

        response = client.post("/files/upload", files=files, data=data)

        assert response.status_code == status.HTTP_201_CREATED
        result = response.json()
        assert result["name"] == "test.txt"
        assert result["size"] == len(file_content)
        assert result["mime_type"] == "text/plain"
        assert result["owner_id"] == test_user.id
        assert result["document_type"] == "general"
        assert "id" in result
        assert "task_id" in result

    def test_upload_file_manual_document_type(self, client, db, test_user, test_workspace, monkeypatch):
        """Upload should normalize, persist, and return manual document_type."""
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)
        monkeypatch.setattr("openrag.services.file_ingest.MinioStorage", FakeMinioStorage)

        file_content = b"Manual content"
        files = {"file": ("manual.txt", io.BytesIO(file_content), "text/plain")}
        data = {
            "path": "/uploads",
            "workspace_id": str(test_workspace.id),
            "document_type": " Manual ",
        }

        response = client.post("/files/upload", files=files, data=data)

        assert response.status_code == status.HTTP_201_CREATED
        result = response.json()
        assert result["document_type"] == "manual"
        saved = db.query(File).filter(File.id == result["id"]).one()
        assert saved.document_type == "manual"

    def test_upload_file_invalid_document_type(self, client, db, test_user, test_workspace):
        """Invalid upload document_type should return 400."""
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)

        file_content = b"Bad type"
        files = {"file": ("bad.txt", io.BytesIO(file_content), "text/plain")}
        data = {
            "workspace_id": str(test_workspace.id),
            "document_type": "contract",
        }

        response = client.post("/files/upload", files=files, data=data)

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "document_type" in response.json()["detail"]

    def test_upload_file_no_auth(self, client, db):
        """Test file upload without authentication"""
        app.dependency_overrides.pop(get_current_user, None)

        file_content = b"Test file content"
        files = {"file": ("test.txt", io.BytesIO(file_content), "text/plain")}

        response = client.post("/files/upload", files=files)

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_upload_file_invalid_path(self, client, db, test_user, test_workspace):
        """Test file upload with invalid path (path traversal)"""
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)

        file_content = b"Test file content"
        files = {"file": ("test.txt", io.BytesIO(file_content), "text/plain")}
        data = {"path": "../../../etc", "workspace_id": str(test_workspace.id)}

        response = client.post("/files/upload", files=files, data=data)

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "invalid path" in response.json()["detail"].lower()

    def test_upload_file_large_file(self, client, db, test_user, test_workspace):
        """Test file upload with large file (exceeds limit)"""
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)

        file_content = b"x" * (MAX_FILE_SIZE + 1)
        files = {"file": ("large.txt", io.BytesIO(file_content), "text/plain")}
        data = {"workspace_id": str(test_workspace.id)}

        response = client.post("/files/upload", files=files, data=data)

        assert response.status_code == status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
        assert db.query(File).count() == 0
        assert db.query(Task).count() == 0

    def test_upload_pdf_parser_rejects_spoofed_pdf_content_type(
        self, client, db, test_user, test_workspace, monkeypatch
    ):
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)
        monkeypatch.setattr("openrag.services.file_ingest.MinioStorage", FakeMinioStorage)

        files = {"file": ("notes.txt", io.BytesIO(b"not pdf"), "application/pdf")}
        data = {
            "path": "/uploads",
            "workspace_id": str(test_workspace.id),
            "parser_type": "pdf",
        }

        response = client.post("/files/upload", files=files, data=data)

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "PaddleOCR" in response.json()["detail"]
        assert db.query(File).filter(File.is_directory.is_(False)).count() == 0
        assert db.query(Task).count() == 0


class TestFileList:
    """Test file listing endpoint"""

    def test_list_files_with_workspace_write(self, client, db, test_user, test_file):
        """Workspace write members can list files."""
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)

        response = client.get("/files/")

        assert response.status_code == status.HTTP_200_OK
        result = response.json()
        assert "items" in result
        assert "total" in result
        assert len(result["items"]) == 1
        assert result["items"][0]["id"] == test_file.id

    def test_list_files_with_workspace_read(
        self, client, db, test_user2, test_file, test_workspace
    ):
        """Workspace read members can list files."""
        db.add(
            WorkspaceMember(
                workspace_id=test_workspace.id,
                user_id=test_user2.id,
                role="read",
            )
        )
        db.commit()

        app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user2)

        response = client.get("/files/")

        assert response.status_code == status.HTTP_200_OK
        result = response.json()
        assert len(result["items"]) == 1
        assert result["items"][0]["id"] == test_file.id

    def test_workspace_read_only_lists_its_workspace_files(
        self, client, db, test_user, test_user2, test_file
    ):
        """Workspace access cannot expose the same path from another workspace."""
        readable_workspace = Workspace(
            name="ReadableWS",
            slug="readable-ws",
            owner_id=test_user.id,
        )
        db.add(readable_workspace)
        db.commit()
        db.refresh(readable_workspace)
        readable_file = File(
            uri=test_file.uri,
            name=test_file.name,
            owner_id=test_user.id,
            workspace_id=readable_workspace.id,
            is_directory=False,
            size=1024,
            mime_type="text/plain",
        )
        db.add_all(
            [
                WorkspaceMember(
                    workspace_id=readable_workspace.id,
                    user_id=test_user2.id,
                    role="read",
                ),
                readable_file,
            ]
        )
        db.commit()

        app.dependency_overrides[get_current_user] = override_get_current_user_factory(
            test_user2
        )

        response = client.get("/files/")

        assert response.status_code == status.HTTP_200_OK
        result = response.json()
        assert result["total"] == 1
        assert result["items"][0]["id"] == readable_file.id
        assert result["items"][0]["id"] != test_file.id
        assert result["items"][0]["uri"] == test_file.uri
        assert client.get(f"/files/{test_file.id}").status_code == status.HTTP_403_FORBIDDEN

    def test_list_files_pagination(self, client, db, test_user, test_workspace):
        """Test file listing with pagination"""
        # Create multiple files (URI globally unique; scoped by workspace in path)
        for i in range(15):
            file = File(
                uri=f"/pag-ws{test_workspace.id}/file{i}.txt",
                name=f"file{i}.txt",
                owner_id=test_user.id,
                workspace_id=test_workspace.id,
                is_directory=False,
                size=1024,
                mime_type="text/plain"
            )
            db.add(file)
        db.commit()

        app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)

        response = client.get(
            "/files/",
            params={"skip": 0, "limit": 10, "workspace_id": test_workspace.id},
        )

        assert response.status_code == status.HTTP_200_OK
        result = response.json()
        assert len(result["items"]) == 10
        assert result["total"] == 15

    def test_list_files_no_auth(self, client, db):
        """Test file listing without authentication"""
        app.dependency_overrides.pop(get_current_user, None)

        response = client.get("/files/")

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_list_parent_path_direct_children(
        self, client, db, test_user, test_workspace, test_directory, test_file
    ):
        """Lazy tree: parent_path=/ returns only /test; parent_path=/test returns file."""
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)

        r = client.get(
            "/files/",
            params={"workspace_id": test_workspace.id, "parent_path": "/"},
        )
        assert r.status_code == status.HTTP_200_OK
        items = r.json()["items"]
        assert len(items) == 1
        assert items[0]["uri"] == "/test"
        assert items[0]["is_directory"] is True

        r2 = client.get(
            "/files/",
            params={"workspace_id": test_workspace.id, "parent_path": "/test"},
        )
        assert r2.status_code == status.HTTP_200_OK
        items2 = r2.json()["items"]
        assert len(items2) == 1
        assert items2[0]["uri"] == "/test/file.txt"

    def test_list_under_path_subtree(
        self, client, db, test_user, test_workspace, test_directory, test_file
    ):
        """under_path=/test includes directory row and nested file."""
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)

        r = client.get(
            "/files/",
            params={"workspace_id": test_workspace.id, "under_path": "/test"},
        )
        assert r.status_code == status.HTTP_200_OK
        uris = {x["uri"] for x in r.json()["items"]}
        assert uris == {"/test", "/test/file.txt"}

    def test_list_parent_and_under_mutually_exclusive(self, client, db, test_user, test_workspace):
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)

        r = client.get(
            "/files/",
            params={"workspace_id": test_workspace.id, "parent_path": "/", "under_path": "/x"},
        )
        assert r.status_code == status.HTTP_400_BAD_REQUEST

    def test_list_files_includes_simple_status_and_null_for_directory(
        self, client, db, test_user, test_workspace, test_file, test_directory
    ):
        """processing_status / simple_status on files; directories omit both."""
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(
            test_user
        )
        test_file.processing_status = ProcessingStatus.embedding
        db.add(test_file)
        db.commit()

        response = client.get(
            "/files/",
            params={"workspace_id": test_workspace.id},
        )
        assert response.status_code == status.HTTP_200_OK
        items = {row["id"]: row for row in response.json()["items"]}
        assert items[test_file.id]["processing_status"] == "embedding"
        assert items[test_file.id]["simple_status"] == "processing"
        assert items[test_file.id].get("error_message") in (None, "")
        assert items[test_directory.id]["processing_status"] is None
        assert items[test_directory.id]["simple_status"] is None


class TestFileGet:
    """Test get file details endpoint"""

    def test_get_file_with_workspace_write(self, client, db, test_user, test_file):
        """Workspace write members can read file details."""
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)

        response = client.get(f"/files/{test_file.id}")

        assert response.status_code == status.HTTP_200_OK
        result = response.json()
        assert result["id"] == test_file.id
        assert result["name"] == test_file.name
        assert result["uri"] == test_file.uri

    def test_get_file_with_workspace_read(
        self, client, db, test_user2, test_file, test_workspace, monkeypatch
    ):
        """Workspace read members can read details and previews."""
        db.add(
            WorkspaceMember(
                workspace_id=test_workspace.id,
                user_id=test_user2.id,
                role="read",
            )
        )
        db.commit()

        app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user2)
        monkeypatch.setattr("openrag.api.files_api.MinioStorage", FakeMinioStorage)
        monkeypatch.setattr(
            "openrag.api.files_api.build_file_preview",
            lambda data, mime_type, filename: ("text", data.decode()),
        )

        response = client.get(f"/files/{test_file.id}")
        preview_response = client.get(f"/files/{test_file.id}/preview")

        assert response.status_code == status.HTTP_200_OK
        assert preview_response.status_code == status.HTTP_200_OK
        assert preview_response.json() == {
            "format": "text",
            "content": "Test file content",
        }

    def test_get_file_no_permission(self, client, db, test_user, test_user2, test_file):
        """Test getting file details without permission"""
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user2)

        response = client.get(f"/files/{test_file.id}")

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_get_file_not_found(self, client, db, test_user):
        """Test getting non-existent file"""
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)

        response = client.get("/files/99999")

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_get_file_failed_includes_error_message(
        self, client, db, test_user, test_file
    ):
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(
            test_user
        )
        test_file.processing_status = ProcessingStatus.failed
        test_file.processing_error = "boom"
        db.add(test_file)
        db.commit()

        response = client.get(f"/files/{test_file.id}")
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["simple_status"] == "failed"
        assert body["error_message"] == "boom"

    def test_get_file_includes_parser_type(self, client, db, test_user, test_file):
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)
        test_file.parser_type = "pdf"
        db.add(test_file)
        db.commit()

        response = client.get(f"/files/{test_file.id}")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["parser_type"] == "pdf"


class TestFileReprocessDocumentType:
    """Test document_type behavior on reprocess."""

    def test_reprocess_without_document_type_preserves_existing_value(
        self, client, db, test_user, test_file, monkeypatch
    ):
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(
            test_user
        )
        test_file.document_type = "manual"
        db.add(test_file)
        db.commit()
        monkeypatch.setattr(
            "openrag.api.files_api.cleanup_file_processing_data",
            lambda file, workspace_slug, db: None,
        )

        response = client.post(f"/files/{test_file.id}/reprocess", json={})

        assert response.status_code == status.HTTP_200_OK
        result = response.json()
        assert result["document_type"] == "manual"
        db.refresh(test_file)
        assert test_file.document_type == "manual"

    def test_reprocess_with_document_type_updates_existing_value(
        self, client, db, test_user, test_file, monkeypatch
    ):
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(
            test_user
        )
        test_file.document_type = "manual"
        db.add(test_file)
        db.commit()
        monkeypatch.setattr(
            "openrag.api.files_api.cleanup_file_processing_data",
            lambda file, workspace_slug, db: None,
        )

        response = client.post(
            f"/files/{test_file.id}/reprocess",
            json={"document_type": "LAWS"},
        )

        assert response.status_code == status.HTTP_200_OK
        result = response.json()
        assert result["document_type"] == "laws"
        db.refresh(test_file)
        assert test_file.document_type == "laws"

    def test_reprocess_explicit_auto_pdf_persists_pdf_default(
        self, client, db, test_user, test_file, monkeypatch
    ):
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(
            test_user
        )
        test_file.name = "report.pdf"
        test_file.uri = "/test/report.pdf"
        test_file.mime_type = "application/pdf"
        test_file.parser_type = "deepdoc"
        db.add(test_file)
        db.commit()
        monkeypatch.setattr(
            "openrag.api.files_api.cleanup_file_processing_data",
            lambda file, workspace_slug, db: None,
        )

        response = client.post(
            f"/files/{test_file.id}/reprocess", json={"parser_type": "auto"}
        )

        assert response.status_code == status.HTTP_200_OK
        db.refresh(test_file)
        assert test_file.parser_type == "pdf"

    def test_reprocess_without_parser_type_preserves_explicit_deepdoc(
        self, client, db, test_user, test_file, monkeypatch
    ):
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(
            test_user
        )
        test_file.name = "report.pdf"
        test_file.uri = "/test/report.pdf"
        test_file.mime_type = "application/pdf"
        test_file.parser_type = "deepdoc"
        db.add(test_file)
        db.commit()
        monkeypatch.setattr(
            "openrag.api.files_api.cleanup_file_processing_data",
            lambda file, workspace_slug, db: None,
        )

        response = client.post(f"/files/{test_file.id}/reprocess", json={})

        assert response.status_code == status.HTTP_200_OK
        db.refresh(test_file)
        assert test_file.parser_type == "deepdoc"


class TestFileDelete:
    """Test file deletion endpoint"""

    def test_delete_file_with_workspace_write(
        self, client, db, test_user2, test_file, test_workspace, monkeypatch
    ):
        """Workspace write members can delete files."""
        db.add(
            WorkspaceMember(
                workspace_id=test_workspace.id,
                user_id=test_user2.id,
                role="write",
            )
        )
        db.commit()
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(
            test_user2
        )

        def delete_from_db(db, file, _workspace):
            db.delete(file)
            db.commit()

        monkeypatch.setattr(
            "openrag.api.files_api.delete_file_with_storage",
            delete_from_db,
        )

        response = client.delete(f"/files/{test_file.id}", params={"background": "false"})

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["message"] == "File deleted successfully"

        # Verify file is deleted
        deleted_file = db.query(File).filter(File.id == test_file.id).first()
        assert deleted_file is None

    def test_delete_file_no_permission(self, client, db, test_user, test_user2, test_file):
        """Test deleting file without permission"""
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user2)

        response = client.delete(f"/files/{test_file.id}")

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_delete_file_not_found(self, client, db, test_user):
        """Test deleting non-existent file"""
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)

        response = client.delete("/files/99999")

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_sync_delete_returns_503_when_storage_cleanup_fails(
        self, client, db, test_user, test_file, monkeypatch
    ):
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(
            test_user
        )

        def fail_cleanup(*_args, **_kwargs):
            raise FileStorageCleanupError(test_file.id)

        monkeypatch.setattr(
            "openrag.api.files_api.delete_file_with_storage",
            fail_cleanup,
        )

        response = client.delete(
            f"/files/{test_file.id}", params={"background": "false"}
        )

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert response.json()["detail"] == "File storage cleanup is incomplete"
        db.expire_all()
        assert db.get(File, test_file.id) is not None


class TestFileMove:
    """Test file move/rename endpoint"""

    def test_move_file_with_workspace_write_member(
        self, client, db, test_user, test_file
    ):
        """Existing workspace write members can move files."""
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)

        new_path = "/test/renamed.txt"
        response = client.put(f"/files/{test_file.id}/move", json={"new_path": new_path})

        assert response.status_code == status.HTTP_200_OK
        result = response.json()
        assert result["uri"] == new_path
        assert result["name"] == "renamed.txt"

    def test_move_file_with_workspace_write(
        self, client, db, test_user2, test_file, test_workspace
    ):
        """Workspace write members can move files."""
        db.add(
            WorkspaceMember(
                workspace_id=test_workspace.id,
                user_id=test_user2.id,
                role="write",
            )
        )
        db.commit()

        app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user2)

        new_path = "/test/renamed.txt"
        response = client.put(f"/files/{test_file.id}/move", json={"new_path": new_path})

        assert response.status_code == status.HTTP_200_OK

    def test_move_file_no_permission(self, client, db, test_user, test_user2, test_file):
        """Test moving file without permission"""
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user2)

        new_path = "/test/renamed.txt"
        response = client.put(f"/files/{test_file.id}/move", json={"new_path": new_path})

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_move_file_invalid_path(self, client, db, test_user, test_file):
        """Test moving file with invalid path"""
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)

        new_path = "../../../etc/passwd"
        response = client.put(f"/files/{test_file.id}/move", json={"new_path": new_path})

        assert response.status_code == status.HTTP_400_BAD_REQUEST


class TestFileWorkspaceAuthorization:
    """Test workspace RBAC boundaries across file endpoints."""

    def test_file_owner_without_workspace_membership_cannot_read_or_write(
        self, client, db, test_user2, test_file
    ):
        test_file.owner_id = test_user2.id
        db.commit()
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(
            test_user2
        )

        responses = [
            client.get(f"/files/{test_file.id}"),
            client.delete(f"/files/{test_file.id}", params={"background": "false"}),
            client.put(
                f"/files/{test_file.id}/move",
                json={"new_path": "/test/owner-renamed.txt"},
            ),
            client.post(f"/files/{test_file.id}/reprocess", json={}),
        ]

        assert all(
            response.status_code == status.HTTP_403_FORBIDDEN
            for response in responses
        )

    def test_workspace_read_cannot_delete_move_or_reprocess(
        self, client, db, test_user2, test_file, test_workspace
    ):
        db.add(
            WorkspaceMember(
                workspace_id=test_workspace.id,
                user_id=test_user2.id,
                role="read",
            )
        )
        db.commit()
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(
            test_user2
        )

        responses = [
            client.delete(f"/files/{test_file.id}", params={"background": "false"}),
            client.put(
                f"/files/{test_file.id}/move",
                json={"new_path": "/test/read-renamed.txt"},
            ),
            client.post(f"/files/{test_file.id}/reprocess", json={}),
        ]

        assert all(
            response.status_code == status.HTTP_403_FORBIDDEN
            for response in responses
        )


class TestDirectoryCreate:
    """Test directory creation endpoint"""

    def test_create_directory_success(self, client, db, test_user, test_workspace):
        """Test creating directory successfully"""
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)

        response = client.post(
            "/files/directories",
            data={"path": "/mydir", "workspace_id": str(test_workspace.id)},
        )

        assert response.status_code == status.HTTP_201_CREATED
        result = response.json()
        assert result["name"] == "mydir"
        assert result["is_directory"] is True
        assert result["owner_id"] == test_user.id

    def test_create_directory_duplicate(self, client, db, test_user, test_workspace, test_directory):
        """Test creating directory that already exists"""
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)

        response = client.post(
            "/files/directories",
            data={"path": test_directory.uri, "workspace_id": str(test_workspace.id)},
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "already exists" in response.json()["detail"].lower()

    def test_create_directory_invalid_path(self, client, db, test_user, test_workspace):
        """Test creating directory with invalid path"""
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(test_user)

        response = client.post(
            "/files/directories",
            data={"path": "../../../etc", "workspace_id": str(test_workspace.id)},
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_create_directory_no_auth(self, client, db, test_workspace):
        """Test creating directory without authentication"""
        app.dependency_overrides.pop(get_current_user, None)

        response = client.post(
            "/files/directories",
            data={"path": "/mydir", "workspace_id": str(test_workspace.id)},
        )

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
