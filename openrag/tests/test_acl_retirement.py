"""Contracts for retiring file-level ACL endpoints."""

from typing import Optional

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from openrag.api.deps import get_current_active_user, get_current_user, get_db
from openrag.api.main import app
from openrag.database import get_db as get_search_db
from openrag.models.base import Base
from openrag.models.document_chunk import DocumentChunk
from openrag.models.file import File
from openrag.models.team import Team, TeamMember, TeamRole
from openrag.models.user import User
from openrag.models.workspace import Workspace, WorkspaceMember
from openrag.retrieval.filters import PermissionFilter
from openrag.retrieval.retrieval_service import RetrievalService
from openrag.security import hash_password


engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(autouse=True)
def dependency_overrides():
    previous_overrides = dict(app.dependency_overrides)
    previous_openapi_schema = app.openapi_schema
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_search_db] = override_get_db
    app.openapi_schema = None
    yield
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous_overrides)
    app.openapi_schema = previous_openapi_schema


@pytest.fixture
def db():
    Base.metadata.create_all(bind=engine)
    db_session = TestingSessionLocal()
    yield db_session
    db_session.close()
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client():
    return TestClient(app)


def override_get_current_user_factory(user: User):
    def override():
        return user

    return override


def create_user(db, suffix: str, *, is_admin: bool = False) -> User:
    user = User(
        username=f"user-{suffix}",
        email=f"user-{suffix}@example.com",
        password_hash=hash_password("password123"),
        full_name=f"User {suffix}",
        is_active=True,
        is_admin=is_admin,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def create_workspace(db, owner: User, slug: str) -> Workspace:
    workspace = Workspace(name=f"Workspace {slug}", slug=slug, owner_id=owner.id)
    db.add(workspace)
    db.commit()
    db.refresh(workspace)
    return workspace


def create_file(
    db,
    owner: User,
    workspace: Workspace,
    *,
    uri: str = "/docs/file.txt",
) -> File:
    file = File(
        uri=uri,
        name=uri.rsplit("/", 1)[-1],
        owner_id=owner.id,
        workspace_id=workspace.id,
        is_directory=False,
        size=128,
        mime_type="text/plain",
    )
    db.add(file)
    db.commit()
    db.refresh(file)
    return file


def add_workspace_member(db, workspace: Workspace, user: User, role: str) -> None:
    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=role))
    db.commit()


def create_team_member(db, workspace: Workspace, owner: User, member: User) -> None:
    team = Team(
        name=f"Team {workspace.slug}",
        description="ACL retirement contract team",
        owner_id=owner.id,
        workspace_id=workspace.id,
    )
    db.add(team)
    db.commit()
    db.refresh(team)
    db.add(TeamMember(team_id=team.id, user_id=member.id, role=TeamRole.MEMBER))
    db.commit()


def create_chunk(db, file: File, chunk_id: str) -> DocumentChunk:
    chunk = DocumentChunk(
        id=db.query(DocumentChunk).count() + 1,
        file_id=file.id,
        workspace_id=file.workspace_id,
        chunk_id=chunk_id,
        chunk_index=0,
        object_key=f"chunks/{file.id}/0000.md",
        text_preview=f"preview-{file.id}",
    )
    db.add(chunk)
    db.commit()
    db.refresh(chunk)
    return chunk


class ContractEmbeddingEngine:
    def embed_text(self, text):
        return [0.1, 0.2, 0.3]


class ContractVectorStore:
    def __init__(self, file_ids: list[int]):
        self.file_ids = file_ids
        self.calls: list[list[int] | None] = []

    def search(self, *, query_embedding, top_k, file_ids=None):
        self.calls.append(file_ids)
        return [
            {
                "chunk_id": f"contract-chunk-{file_id}",
                "file_id": file_id,
                "text": f"chunk-{file_id}",
                "score": 1.0 - index / 10,
            }
            for index, file_id in enumerate(self.file_ids)
        ]


class ContractLayerStore:
    def __init__(self, file_ids: list[int]):
        self.file_ids = file_ids
        self.calls: list[dict] = []

    def search_layers(self, query_embedding, layer, top_k, file_ids=None):
        self.calls.append({"layer": layer, "file_ids": file_ids})
        return [
            {
                "file_id": file_id,
                "score": 1.0 - index / 10,
                "layer_row_id": f"{layer}-{file_id}",
                "text": f"{layer}-{file_id}",
            }
            for index, file_id in enumerate(self.file_ids)
        ]


class ContractObjectStream:
    def stream(self, chunk_size):
        yield b"contract content"

    def close(self):
        return None

    def release_conn(self):
        return None


@pytest.mark.parametrize(
    ("method", "path_template", "json_body"),
    [
        ("GET", "/files/{file_id}/permissions", None),
        (
            "POST",
            "/files/{file_id}/permissions",
            {"entity_type": "user", "entity_id": 0, "permission": "read"},
        ),
        ("DELETE", "/files/{file_id}/permissions/1", None),
    ],
)
@pytest.mark.parametrize("authenticated", [False, True])
def test_file_acl_routes_are_not_registered(
    client,
    db,
    method: str,
    path_template: str,
    json_body: Optional[dict],
    authenticated: bool,
):
    owner = create_user(db, "old-route-owner")
    recipient = create_user(db, "old-route-recipient")
    workspace = create_workspace(db, owner, "old-route")
    add_workspace_member(db, workspace, owner, "write")
    file = create_file(db, owner, workspace)
    if authenticated:
        app.dependency_overrides[get_current_user] = override_get_current_user_factory(
            owner
        )

    body = dict(json_body or {})
    if "entity_id" in body:
        body["entity_id"] = recipient.id

    response = client.request(
        method,
        path_template.format(file_id=file.id),
        json=body if body else None,
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "Not Found"


def test_openapi_excludes_file_acl_paths(client):
    response = client.get("/openapi.json")

    assert response.status_code == status.HTTP_200_OK
    paths = response.json()["paths"]
    assert "/files/{file_id}/permissions" not in paths
    assert "/files/{file_id}/permissions/{permission_id}" not in paths
    schemas = response.json()["components"]["schemas"]
    assert "PermissionResponse" not in schemas
    assert "GrantPermissionRequest" not in schemas


def test_metadata_excludes_file_permissions(db):
    assert "file_permissions" not in Base.metadata.tables
    assert "file_permissions" not in inspect(engine).get_table_names()

    relationship_names = set(File.__mapper__.relationships.keys())
    assert "permissions" not in relationship_names
    assert {
        "owner",
        "workspace",
        "children",
        "share_links",
        "document_chunks",
    } <= relationship_names

    column_names = set(File.__table__.columns.keys())
    assert {"owner_id", "l0_path", "l1_path", "l2_path", "l0_vector_id"} <= column_names


def test_user_permission_details_route_is_preserved(client, db):
    user = create_user(db, "details")
    workspace = create_workspace(db, user, "details")
    add_workspace_member(db, workspace, user, "read")
    app.dependency_overrides[get_current_user] = override_get_current_user_factory(user)

    response = client.get(f"/users/{user.id}/permissions/details")

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["user_id"] == user.id
    assert "roles" in body
    assert "workspace_permissions" in body
    assert body["workspace_permissions"][0]["workspace_id"] == workspace.id


def test_owner_id_without_workspace_permission_is_denied(client, db):
    workspace_owner = create_user(db, "workspace-owner")
    file_owner = create_user(db, "file-owner")
    workspace = create_workspace(db, workspace_owner, "owner-denied")
    file = create_file(db, file_owner, workspace)
    app.dependency_overrides[get_current_user] = override_get_current_user_factory(
        file_owner
    )

    read_response = client.get(f"/files/{file.id}")
    write_response = client.post(
        "/files/directories",
        data={"path": "/owner-created", "workspace_id": str(workspace.id)},
    )

    assert read_response.status_code == status.HTTP_403_FORBIDDEN
    assert write_response.status_code == status.HTTP_403_FORBIDDEN
    assert PermissionFilter(db).get_accessible_uris(file_owner.id) == set()


def test_team_membership_without_workspace_permission_is_denied(client, db):
    workspace_owner = create_user(db, "team-workspace-owner")
    file_owner = create_user(db, "team-file-owner")
    member = create_user(db, "team-member")
    workspace = create_workspace(db, workspace_owner, "team-denied")
    file = create_file(db, file_owner, workspace)
    create_team_member(db, workspace, workspace_owner, member)
    app.dependency_overrides[get_current_user] = override_get_current_user_factory(member)

    response = client.get(f"/files/{file.id}")

    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_workspace_read_can_read_but_cannot_write(client, db):
    owner = create_user(db, "read-owner")
    reader = create_user(db, "reader")
    workspace = create_workspace(db, owner, "read-only")
    file = create_file(db, owner, workspace)
    add_workspace_member(db, workspace, reader, "read")
    app.dependency_overrides[get_current_user] = override_get_current_user_factory(reader)

    read_response = client.get(f"/files/{file.id}")
    write_response = client.post(
        "/files/directories",
        data={"path": "/readonly-created", "workspace_id": str(workspace.id)},
    )

    assert read_response.status_code == status.HTTP_200_OK
    assert write_response.status_code == status.HTTP_403_FORBIDDEN


def test_workspace_write_can_manage_files(client, db, monkeypatch):
    owner = create_user(db, "write-owner")
    writer = create_user(db, "writer")
    workspace = create_workspace(db, owner, "write-manage")
    add_workspace_member(db, workspace, writer, "write")
    app.dependency_overrides[get_current_user] = override_get_current_user_factory(writer)
    monkeypatch.setattr(
        "openrag.api.files_api.MinioStorage.ensure_bucket",
        lambda self, bucket_name: None,
    )

    response = client.post(
        "/files/directories",
        data={"path": "/managed", "workspace_id": str(workspace.id)},
    )

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["uri"] == "/managed"
    assert body["name"] == "managed"


def test_same_workspace_readers_share_hierarchy_scope(db):
    owner = create_user(db, "hierarchy-owner")
    reader_a = create_user(db, "hierarchy-reader-a")
    reader_b = create_user(db, "hierarchy-reader-b")
    workspace = create_workspace(db, owner, "hierarchy-shared")
    other_workspace = create_workspace(db, owner, "hierarchy-other")
    add_workspace_member(db, workspace, reader_a, "read")
    add_workspace_member(db, workspace, reader_b, "read")
    file_a = create_file(db, owner, workspace, uri="/shared/a.txt")
    file_b = create_file(db, owner, workspace, uri="/shared/b.txt")
    other_file = create_file(db, owner, other_workspace, uri="/private/unique.txt")
    create_chunk(db, file_a, f"contract-chunk-{file_a.id}")
    create_chunk(db, file_b, f"contract-chunk-{file_b.id}")
    create_chunk(db, other_file, f"contract-chunk-{other_file.id}")
    file_a.l0_path = "l0/shared-a.md"
    file_a.l1_path = "l1/shared-a.md"
    file_a.l2_path = "l2/shared-a"
    file_b.l0_path = "l0/shared-b.md"
    file_b.l1_path = "l1/shared-b.md"
    file_b.l2_path = "l2/shared-b"
    other_file.l0_path = "l0/private-unique.md"
    other_file.l1_path = "l1/private-unique.md"
    other_file.l2_path = "l2/private-unique"
    db.commit()
    all_file_ids = [file_a.id, file_b.id, other_file.id]

    def run_for(user: User):
        vector_store = ContractVectorStore(all_file_ids)
        layer_store = ContractLayerStore(all_file_ids)
        service = RetrievalService(
            db,
            ContractEmbeddingEngine(),
            vector_store,
            layer_store=layer_store,
        )
        scope = service._accessible_file_ids(user.id)
        results = service.search(
            "shared hierarchy",
            user.id,
            use_contextual=True,
            retrieval_strategy="deep",
        )
        return scope, layer_store.calls, vector_store.calls, results

    result_a = run_for(reader_a)
    result_b = run_for(reader_b)
    expected_file_ids = {file_a.id, file_b.id}

    assert set(result_a[0]) == expected_file_ids
    assert set(result_b[0]) == expected_file_ids
    assert result_a[1] == result_b[1]
    assert result_a[2] == result_b[2]
    assert {row["file_id"] for row in result_a[3]} == expected_file_ids
    assert result_a[3] == result_b[3]
    assert all(other_file.id not in (call["file_ids"] or []) for call in result_a[1])
    assert all(other_file.id not in (call or []) for call in result_a[2])
    assert file_a.l0_path == "l0/shared-a.md"
    assert file_a.l1_path == "l1/shared-a.md"
    assert file_a.l2_path == "l2/shared-a"


def test_workspace_revocation_hides_search_preview_and_context(
    client, db, monkeypatch
):
    owner = create_user(db, "revocation-owner")
    reader = create_user(db, "revocation-reader")
    workspace = create_workspace(db, owner, "revocation")
    add_workspace_member(db, workspace, reader, "read")
    file = create_file(db, owner, workspace, uri="/revocation/unique.txt")
    chunk = create_chunk(db, file, f"contract-chunk-{file.id}")
    app.dependency_overrides[get_current_user] = override_get_current_user_factory(reader)
    app.dependency_overrides[get_current_active_user] = (
        override_get_current_user_factory(reader)
    )
    monkeypatch.setattr(
        "openrag.api.files_api.MinioStorage.read_object_bytes",
        lambda self, bucket, object_key: b"contract content",
    )
    monkeypatch.setattr(
        "openrag.api.files_api.MinioStorage.open_object_stream",
        lambda self, bucket, object_key: ContractObjectStream(),
    )
    monkeypatch.setattr(
        "openrag.api.files_api.build_file_preview",
        lambda data, mime_type, filename: ("text", data.decode()),
    )
    vector_store = ContractVectorStore([file.id])
    service = RetrievalService(db, ContractEmbeddingEngine(), vector_store)

    before_search = service.search("unique phrase", reader.id)
    before_preview = client.get(f"/files/{file.id}/preview")
    before_content = client.get(f"/files/{file.id}/content")
    before_context = client.get(f"/search/chunks/{chunk.chunk_id}")

    assert [row["file_id"] for row in before_search] == [file.id]
    assert before_preview.status_code == status.HTTP_200_OK
    assert before_content.status_code == status.HTTP_200_OK
    assert before_context.status_code == status.HTTP_200_OK

    db.query(WorkspaceMember).filter(
        WorkspaceMember.workspace_id == workspace.id,
        WorkspaceMember.user_id == reader.id,
    ).delete()
    db.commit()

    after_search = service.search("unique phrase", reader.id)
    after_preview = client.get(f"/files/{file.id}/preview")
    after_content = client.get(f"/files/{file.id}/content")
    after_context = client.get(f"/search/chunks/{chunk.chunk_id}")

    assert after_search == []
    assert vector_store.calls == [[file.id]]
    assert after_preview.status_code == status.HTTP_403_FORBIDDEN
    assert after_content.status_code == status.HTTP_403_FORBIDDEN
    assert after_context.status_code == status.HTTP_403_FORBIDDEN


def test_team_membership_does_not_change_retrieval_scope(db):
    owner = create_user(db, "retrieval-team-owner")
    member = create_user(db, "retrieval-team-member")
    workspace = create_workspace(db, owner, "retrieval-team")
    file = create_file(db, owner, workspace, uri="/team-only/unique.txt")
    create_team_member(db, workspace, owner, member)
    vector_store = ContractVectorStore([file.id])
    service = RetrievalService(db, ContractEmbeddingEngine(), vector_store)

    assert PermissionFilter(db).get_accessible_uris(member.id) == set()
    assert service._accessible_file_ids(member.id) == []
    assert service.search("team-only phrase", member.id) == []
    assert vector_store.calls == []
