"""Workspace-scoped retrieval permission tests."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from openrag.models.base import Base
from openrag.models.file import File
from openrag.models.team import Team, TeamMember, TeamRole
from openrag.models.user import User
from openrag.models.workspace import Workspace, WorkspaceMember
from openrag.retrieval.filters import PermissionFilter
from openrag.retrieval.retrieval_service import RetrievalService


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    Base.metadata.drop_all(engine)


@pytest.fixture
def test_users(db_session):
    users = {
        name: User(
            username=name,
            email=f"{name}@example.com",
            password_hash="hash",
            full_name=name.title(),
        )
        for name in ("alice", "bob", "charlie")
    }
    db_session.add_all(users.values())
    db_session.commit()
    return users


@pytest.fixture
def retrieval_data(db_session, test_users):
    workspace_a = Workspace(
        name="Workspace A",
        slug="workspace-a",
        owner_id=test_users["alice"].id,
    )
    workspace_b = Workspace(
        name="Workspace B",
        slug="workspace-b",
        owner_id=test_users["alice"].id,
    )
    db_session.add_all([workspace_a, workspace_b])
    db_session.commit()
    db_session.add_all(
        [
            WorkspaceMember(
                workspace_id=workspace_a.id,
                user_id=test_users["alice"].id,
                role="write",
            ),
            WorkspaceMember(
                workspace_id=workspace_a.id,
                user_id=test_users["bob"].id,
                role="read",
            ),
        ]
    )
    files = {
        "a1": File(
            uri="/workspace-a/file1.pdf",
            name="file1.pdf",
            owner_id=test_users["alice"].id,
            workspace_id=workspace_a.id,
            size=1024,
            mime_type="application/pdf",
        ),
        "a2": File(
            uri="/workspace-a/file2.pdf",
            name="file2.pdf",
            owner_id=test_users["bob"].id,
            workspace_id=workspace_a.id,
            size=2048,
            mime_type="application/pdf",
        ),
        "b": File(
            uri="/workspace-b/private.pdf",
            name="private.pdf",
            owner_id=test_users["charlie"].id,
            workspace_id=workspace_b.id,
            size=3072,
            mime_type="application/pdf",
        ),
    }
    db_session.add_all(files.values())
    db_session.commit()
    return {"workspace_a": workspace_a, "workspace_b": workspace_b, **files}


@pytest.fixture
def team_membership(db_session, test_users, retrieval_data):
    team = Team(
        name="Engineering",
        description="Does not grant file access",
        owner_id=test_users["alice"].id,
        workspace_id=retrieval_data["workspace_b"].id,
    )
    db_session.add(team)
    db_session.commit()
    db_session.add(
        TeamMember(
            team_id=team.id,
            user_id=test_users["bob"].id,
            role=TeamRole.MEMBER,
        )
    )
    db_session.commit()
    return team


def test_get_accessible_uris_with_workspace_write(
    db_session, test_users, retrieval_data
):
    uris = PermissionFilter(db_session).get_accessible_uris(test_users["alice"].id)

    assert uris == {retrieval_data["a1"].uri, retrieval_data["a2"].uri}


def test_get_accessible_uris_with_workspace_read(
    db_session, test_users, retrieval_data
):
    uris = PermissionFilter(db_session).get_accessible_uris(test_users["bob"].id)

    assert uris == {retrieval_data["a1"].uri, retrieval_data["a2"].uri}


def test_get_accessible_uris_with_team_membership_only(
    db_session, test_users, retrieval_data, team_membership
):
    uris = PermissionFilter(db_session).get_accessible_uris(test_users["bob"].id)

    assert retrieval_data["b"].uri not in uris


def test_file_owner_without_workspace_membership_has_empty_scope(
    db_session, test_users, retrieval_data
):
    uris = PermissionFilter(db_session).get_accessible_uris(test_users["charlie"].id)

    assert uris == set()


def test_filter_results_uses_accessible_uris(db_session, retrieval_data):
    results = [
        {"uri": retrieval_data["a1"].uri, "score": 0.9},
        {"uri": retrieval_data["b"].uri, "score": 0.8},
    ]

    filtered = PermissionFilter(db_session).filter_results(
        results,
        {retrieval_data["a1"].uri},
    )

    assert filtered == [results[0]]


class FakeEmbeddingEngine:
    def embed_text(self, text):
        return [0.1, 0.2, 0.3]


class FakeVectorStore:
    def __init__(self, file_ids):
        self.file_ids = file_ids
        self.calls = []

    def search(self, *, query_embedding, top_k, file_ids=None):
        self.calls.append(file_ids)
        return [
            {
                "chunk_id": f"chunk-{file_id}",
                "file_id": file_id,
                "text": f"content-{file_id}",
                "score": 1.0 - index / 10,
            }
            for index, file_id in enumerate(self.file_ids)
        ]


class FakeLayerStore:
    def __init__(self, file_ids):
        self.file_ids = file_ids
        self.calls = []

    def search_layers(self, query_embedding, layer, top_k, file_ids=None):
        self.calls.append({"layer": layer, "file_ids": file_ids})
        return [
            {
                "file_id": file_id,
                "score": 1.0 - index / 10,
                "layer_row_id": f"{layer}-{file_id}",
                "text": f"{layer}-content-{file_id}",
            }
            for index, file_id in enumerate(self.file_ids)
        ]


def test_search_filters_by_workspace_permission(db_session, test_users, retrieval_data):
    all_file_ids = [
        retrieval_data["a1"].id,
        retrieval_data["a2"].id,
        retrieval_data["b"].id,
    ]
    vector_store = FakeVectorStore(all_file_ids)
    service = RetrievalService(
        db_session,
        FakeEmbeddingEngine(),
        vector_store,
    )

    results = service.search("query", test_users["bob"].id)

    assert {result["file_id"] for result in results} == {
        retrieval_data["a1"].id,
        retrieval_data["a2"].id,
    }
    assert vector_store.calls == [
        [retrieval_data["a1"].id, retrieval_data["a2"].id]
    ]


def test_contextual_search_limits_l0_l1_l2_to_workspace(
    db_session, test_users, retrieval_data
):
    all_file_ids = [
        retrieval_data["a1"].id,
        retrieval_data["a2"].id,
        retrieval_data["b"].id,
    ]
    vector_store = FakeVectorStore(all_file_ids)
    layer_store = FakeLayerStore(all_file_ids)
    service = RetrievalService(
        db_session,
        FakeEmbeddingEngine(),
        vector_store,
        layer_store=layer_store,
    )

    results = service.search(
        "query",
        test_users["bob"].id,
        use_contextual=True,
        retrieval_strategy="deep",
    )

    workspace_file_ids = {retrieval_data["a1"].id, retrieval_data["a2"].id}
    assert {result["file_id"] for result in results} == workspace_file_ids
    assert set(layer_store.calls[0]["file_ids"]) == workspace_file_ids
    assert set(layer_store.calls[1]["file_ids"]) == workspace_file_ids
    assert set(vector_store.calls[0]) == workspace_file_ids
