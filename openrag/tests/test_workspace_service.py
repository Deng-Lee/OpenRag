"""Tests for Workspace service"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from openrag.models import Base
from openrag.services.workspace_service import WorkspaceService
from openrag.models.workspace import Workspace, WorkspaceMember
from openrag.models.user import User


class FakeIndexLifecycle:
    def __init__(self, *, fail=False, fail_delete=False):
        self.fail = fail
        self.fail_delete = fail_delete
        self.ensure_calls = []
        self.rollback_calls = []
        self.delete_calls = []

    def ensure_ready(self, *, workspace_id, workspace_slug):
        self.ensure_calls.append((workspace_id, workspace_slug))
        if self.fail:
            raise RuntimeError("provision failed")
        return object()

    def rollback_provision(self, receipt):
        self.rollback_calls.append(receipt)

    def delete_workspace_indices(self, *, workspace_id, workspace_slug):
        self.delete_calls.append((workspace_id, workspace_slug))
        if self.fail_delete:
            raise RuntimeError("cleanup failed")


@pytest.fixture(scope="function")
def db():
    """Create a test database session"""
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()

    yield session

    session.close()
    Base.metadata.drop_all(engine)


def test_create_workspace(db):
    """Test creating a workspace"""
    user = User(
        username="owner",
        email="owner@test.com",
        password_hash="hash",
        full_name="Owner"
    )
    db.add(user)
    db.commit()

    service = WorkspaceService(db)
    workspace = service.create_workspace(
        name="Test Workspace",
        slug="test-workspace",
        description="Test",
        owner_id=user.id
    )

    assert workspace.id is not None
    assert workspace.name == "Test Workspace"
    assert workspace.slug == "test-workspace"
    assert workspace.owner_id == user.id

    # Verify owner is added as admin member
    member = db.query(WorkspaceMember).filter(
        WorkspaceMember.workspace_id == workspace.id,
        WorkspaceMember.user_id == user.id
    ).first()
    assert member is not None
    assert member.role == 'admin'


def test_create_workspace_provisions_v2_before_success(db):
    user = User(
        username="v2-owner",
        email="v2-owner@test.com",
        password_hash="hash",
        full_name="V2 Owner",
    )
    db.add(user)
    db.commit()
    lifecycle = FakeIndexLifecycle()

    workspace = WorkspaceService(
        db,
        index_lifecycle=lifecycle,
        chunk_index_mode="v2_alias",
    ).create_workspace(
        name="V2 Workspace",
        slug="v2-workspace",
        description=None,
        owner_id=user.id,
    )

    assert lifecycle.ensure_calls == [(workspace.id, workspace.slug)]
    assert lifecycle.rollback_calls == []


def test_create_workspace_rolls_back_db_when_v2_provisioning_fails(db):
    user = User(
        username="failed-owner",
        email="failed-owner@test.com",
        password_hash="hash",
        full_name="Failed Owner",
    )
    db.add(user)
    db.commit()
    lifecycle = FakeIndexLifecycle(fail=True)
    service = WorkspaceService(
        db,
        index_lifecycle=lifecycle,
        chunk_index_mode="v2_alias",
    )

    with pytest.raises(RuntimeError, match="provision failed"):
        service.create_workspace(
            name="Failed Workspace",
            slug="failed-workspace",
            description=None,
            owner_id=user.id,
        )

    assert (
        db.query(Workspace)
        .filter(Workspace.slug == "failed-workspace")
        .first()
        is None
    )


def test_create_workspace_compensates_es_when_db_commit_fails(db, monkeypatch):
    user = User(
        username="commit-owner",
        email="commit-owner@test.com",
        password_hash="hash",
        full_name="Commit Owner",
    )
    db.add(user)
    db.commit()
    lifecycle = FakeIndexLifecycle()
    service = WorkspaceService(
        db,
        index_lifecycle=lifecycle,
        chunk_index_mode="v2_alias",
    )

    def fail_commit():
        raise RuntimeError("commit failed")

    monkeypatch.setattr(db, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="commit failed"):
        service.create_workspace(
            name="Commit Failure",
            slug="commit-failure",
            description=None,
            owner_id=user.id,
        )

    assert len(lifecycle.rollback_calls) == 1
    assert (
        db.query(Workspace)
        .filter(Workspace.slug == "commit-failure")
        .first()
        is None
    )


def test_delete_workspace_removes_v2_indices_before_commit(db):
    user = User(
        username="delete-owner",
        email="delete-owner@test.com",
        password_hash="hash",
        full_name="Delete Owner",
    )
    db.add(user)
    db.commit()
    workspace = Workspace(name="Delete", slug="delete-v2", owner_id=user.id)
    db.add(workspace)
    db.commit()
    workspace_id = workspace.id
    lifecycle = FakeIndexLifecycle()

    deleted = WorkspaceService(
        db,
        index_lifecycle=lifecycle,
        chunk_index_mode="v2_alias",
    ).delete_workspace(workspace_id)

    assert deleted is True
    assert lifecycle.delete_calls == [(workspace_id, "delete-v2")]
    assert db.get(Workspace, workspace_id) is None


def test_delete_workspace_rolls_back_db_when_v2_cleanup_fails(db):
    user = User(
        username="cleanup-owner",
        email="cleanup-owner@test.com",
        password_hash="hash",
        full_name="Cleanup Owner",
    )
    db.add(user)
    db.commit()
    workspace = Workspace(name="Cleanup", slug="cleanup-v2", owner_id=user.id)
    db.add(workspace)
    db.commit()
    workspace_id = workspace.id
    lifecycle = FakeIndexLifecycle(fail_delete=True)

    with pytest.raises(RuntimeError, match="cleanup failed"):
        WorkspaceService(
            db,
            index_lifecycle=lifecycle,
            chunk_index_mode="v2_alias",
        ).delete_workspace(workspace_id)

    assert db.get(Workspace, workspace_id) is not None


def test_get_user_workspaces(db):
    """Test getting user's workspaces"""
    user = User(
        username="user",
        email="user@test.com",
        password_hash="hash",
        full_name="User"
    )
    db.add(user)
    db.commit()

    # Create workspaces
    ws1 = Workspace(name="WS1", slug="ws1", owner_id=user.id)
    ws2 = Workspace(name="WS2", slug="ws2", owner_id=user.id)
    db.add_all([ws1, ws2])
    db.commit()

    # Add user as member
    member1 = WorkspaceMember(workspace_id=ws1.id, user_id=user.id, role='admin')
    member2 = WorkspaceMember(workspace_id=ws2.id, user_id=user.id, role='member')
    db.add_all([member1, member2])
    db.commit()

    service = WorkspaceService(db)
    workspaces = service.get_user_workspaces(user.id)

    assert len(workspaces) == 2
    assert {ws.slug for ws in workspaces} == {'ws1', 'ws2'}


def test_add_member(db):
    """Test adding member to workspace"""
    owner = User(
        username="owner",
        email="owner@test.com",
        password_hash="hash",
        full_name="Owner"
    )
    member_user = User(
        username="member",
        email="member@test.com",
        password_hash="hash",
        full_name="Member"
    )
    db.add_all([owner, member_user])
    db.commit()

    workspace = Workspace(name="Test", slug="test", owner_id=owner.id)
    db.add(workspace)
    db.commit()

    service = WorkspaceService(db)
    member = service.add_member(workspace.id, member_user.id, role='member')

    assert member.workspace_id == workspace.id
    assert member.user_id == member_user.id
    assert member.role == 'member'


def test_remove_member(db):
    """Test removing member from workspace"""
    owner = User(
        username="owner",
        email="owner@test.com",
        password_hash="hash",
        full_name="Owner"
    )
    member_user = User(
        username="member",
        email="member@test.com",
        password_hash="hash",
        full_name="Member"
    )
    db.add_all([owner, member_user])
    db.commit()

    workspace = Workspace(name="Test", slug="test", owner_id=owner.id)
    db.add(workspace)
    db.commit()

    member = WorkspaceMember(workspace_id=workspace.id, user_id=member_user.id, role='member')
    db.add(member)
    db.commit()

    service = WorkspaceService(db)
    result = service.remove_member(workspace.id, member_user.id)

    assert result is True

    # Verify member was removed
    remaining = db.query(WorkspaceMember).filter(
        WorkspaceMember.workspace_id == workspace.id,
        WorkspaceMember.user_id == member_user.id
    ).first()
    assert remaining is None


def test_check_user_access(db):
    """Test checking user access to workspace"""
    owner = User(
        username="owner",
        email="owner@test.com",
        password_hash="hash",
        full_name="Owner"
    )
    member_user = User(
        username="member",
        email="member@test.com",
        password_hash="hash",
        full_name="Member"
    )
    non_member = User(
        username="other",
        email="other@test.com",
        password_hash="hash",
        full_name="Other"
    )
    db.add_all([owner, member_user, non_member])
    db.commit()

    workspace = Workspace(name="Test", slug="test", owner_id=owner.id)
    db.add(workspace)
    db.commit()

    member = WorkspaceMember(workspace_id=workspace.id, user_id=member_user.id, role='member')
    db.add(member)
    db.commit()

    service = WorkspaceService(db)

    assert service.check_user_access(workspace.id, member_user.id) is True
    assert service.check_user_access(workspace.id, non_member.id) is False


def test_update_workspace_quota(db):
    """Test updating workspace quota"""
    user = User(
        username="owner",
        email="owner@test.com",
        password_hash="hash",
        full_name="Owner"
    )
    db.add(user)
    db.commit()

    workspace = Workspace(name="Test", slug="test", owner_id=user.id)
    db.add(workspace)
    db.commit()

    service = WorkspaceService(db)
    updated = service.update_workspace_quota(
        workspace.id,
        max_concurrent_tasks=20,
        priority_strategy='user_role'
    )

    assert updated.max_concurrent_tasks == 20
    assert updated.priority_strategy == 'user_role'
