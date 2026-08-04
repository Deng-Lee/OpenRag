"""Tests for Workspace service"""

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from openrag.models import Base, DocumentChunk, File, Task
from openrag.models.task import TaskStatus
from openrag.services.workspace_service import (
    WorkspaceNotEmptyError,
    WorkspaceStorageCleanupError,
    WorkspaceService,
)
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


class FakeWorkspaceStorage:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.delete_calls = []

    def remove_workspace_storage(self, workspace_slug):
        self.delete_calls.append(workspace_slug)
        if self.fail:
            raise OSError("storage cleanup failed")


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


def _workspace(db, slug):
    user = User(
        username=f"{slug}-owner",
        email=f"{slug}-owner@test.com",
        password_hash="hash",
        full_name=f"{slug} Owner",
    )
    db.add(user)
    db.commit()
    workspace = Workspace(name=slug, slug=slug, owner_id=user.id)
    db.add(workspace)
    db.commit()
    return user, workspace


def _file(db, user, workspace, uri="/", **overrides):
    values = {
        "uri": uri,
        "name": uri.rsplit("/", 1)[-1] or "root",
        "owner_id": user.id,
        "workspace_id": workspace.id,
        "is_directory": uri == "/",
        "size": 0 if uri == "/" else 3,
    }
    values.update(overrides)
    file = File(**values)
    db.add(file)
    db.commit()
    return file


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
    storage = FakeWorkspaceStorage()

    deleted = WorkspaceService(
        db,
        index_lifecycle=lifecycle,
        chunk_index_mode="v2_alias",
        minio_storage=storage,
    ).delete_workspace(workspace_id)

    assert deleted is True
    assert lifecycle.delete_calls == [(workspace_id, "delete-v2")]
    assert storage.delete_calls == ["delete-v2"]
    assert db.get(Workspace, workspace_id) is None


def test_delete_workspace_removes_legacy_indices_before_commit(db):
    _, workspace = _workspace(db, "delete-legacy")
    workspace_id = workspace.id
    lifecycle = FakeIndexLifecycle()
    storage = FakeWorkspaceStorage()

    deleted = WorkspaceService(
        db,
        index_lifecycle=lifecycle,
        chunk_index_mode="legacy",
        minio_storage=storage,
    ).delete_workspace(workspace_id)

    assert deleted is True
    assert lifecycle.delete_calls == [(workspace_id, "delete-legacy")]
    assert storage.delete_calls == ["delete-legacy"]
    assert db.get(Workspace, workspace_id) is None


@pytest.mark.parametrize(
    "task_status",
    [
        TaskStatus.PENDING.value,
        TaskStatus.ASSIGNED.value,
        TaskStatus.STARTED.value,
        TaskStatus.RETRY.value,
    ],
)
def test_delete_workspace_rejects_active_tasks(db, task_status):
    user, workspace = _workspace(db, f"active-{task_status}")
    workspace_id = workspace.id
    task = Task(
        task_id=f"active-{task_status}",
        workspace_id=workspace_id,
        user_id=user.id,
        task_type="process_document",
        status=task_status,
    )
    db.add(task)
    db.commit()
    lifecycle = FakeIndexLifecycle()
    storage = FakeWorkspaceStorage()

    with pytest.raises(WorkspaceNotEmptyError, match="active tasks"):
        WorkspaceService(
            db,
            index_lifecycle=lifecycle,
            chunk_index_mode="legacy",
            minio_storage=storage,
        ).delete_workspace(workspace_id)

    assert lifecycle.delete_calls == []
    assert storage.delete_calls == []
    assert db.get(Workspace, workspace_id) is not None


@pytest.mark.parametrize(
    "task_status",
    [
        TaskStatus.SUCCESS.value,
        TaskStatus.FAILURE.value,
        TaskStatus.CANCELLED.value,
    ],
)
def test_delete_workspace_allows_terminal_tasks(db, task_status):
    user, workspace = _workspace(db, f"terminal-{task_status}")
    workspace_id = workspace.id
    db.add(
        Task(
            task_id=f"terminal-{task_status}",
            workspace_id=workspace_id,
            user_id=user.id,
            task_type="process_document",
            status=task_status,
        )
    )
    db.commit()
    lifecycle = FakeIndexLifecycle()
    storage = FakeWorkspaceStorage()

    deleted = WorkspaceService(
        db,
        index_lifecycle=lifecycle,
        chunk_index_mode="legacy",
        minio_storage=storage,
    ).delete_workspace(workspace_id)

    assert deleted is True
    assert lifecycle.delete_calls == [(workspace_id, f"terminal-{task_status}")]
    assert storage.delete_calls == [f"terminal-{task_status}"]
    assert db.get(Workspace, workspace_id) is None


def test_delete_workspace_removes_structural_root(db):
    user, workspace = _workspace(db, "root-only")
    member = WorkspaceMember(
        workspace_id=workspace.id,
        user_id=user.id,
        role="admin",
    )
    db.add(member)
    db.commit()
    root = _file(db, user, workspace)
    workspace_id = workspace.id
    root_id = root.id
    lifecycle = FakeIndexLifecycle()
    storage = FakeWorkspaceStorage()

    deleted = WorkspaceService(
        db,
        index_lifecycle=lifecycle,
        chunk_index_mode="v2_alias",
        minio_storage=storage,
    ).delete_workspace(workspace_id)

    assert deleted is True
    assert lifecycle.delete_calls == [(workspace_id, "root-only")]
    assert storage.delete_calls == ["root-only"]
    assert db.get(File, root_id) is None
    assert db.get(Workspace, workspace_id) is None
    assert (
        db.query(WorkspaceMember)
        .filter(WorkspaceMember.workspace_id == workspace_id)
        .first()
        is None
    )


@pytest.mark.parametrize("deleted_at", [None, datetime(2026, 8, 2)])
def test_delete_workspace_rejects_non_root_file(db, deleted_at):
    user, workspace = _workspace(db, "non-empty")
    _file(db, user, workspace)
    _file(db, user, workspace, "/document.txt", deleted_at=deleted_at)
    workspace_id = workspace.id
    lifecycle = FakeIndexLifecycle()

    with pytest.raises(WorkspaceNotEmptyError, match="not empty"):
        WorkspaceService(
            db,
            index_lifecycle=lifecycle,
            chunk_index_mode="v2_alias",
        ).delete_workspace(workspace_id)

    assert lifecycle.delete_calls == []
    assert db.get(Workspace, workspace_id) is not None
    assert db.query(File).filter(File.workspace_id == workspace_id).count() == 2


def test_delete_workspace_rejects_root_with_hierarchy_content(db):
    user, workspace = _workspace(db, "root-content")
    root = _file(
        db,
        user,
        workspace,
        l0_path="http://minio/root.abstract.md",
    )
    workspace_id = workspace.id

    with pytest.raises(WorkspaceNotEmptyError, match="root contains"):
        WorkspaceService(db).delete_workspace(workspace_id)

    assert db.get(Workspace, workspace_id) is not None
    assert db.get(File, root.id) is not None


def test_delete_workspace_rejects_root_with_chunk(db):
    user, workspace = _workspace(db, "root-chunk")
    root = _file(db, user, workspace)
    db.add(
        DocumentChunk(
            id=1,
            file_id=root.id,
            workspace_id=workspace.id,
            chunk_id="root-chunk",
            chunk_index=0,
            object_key="hierarchy/root/chunks/0000.md",
        )
    )
    db.commit()
    workspace_id = workspace.id

    with pytest.raises(WorkspaceNotEmptyError, match="root contains"):
        WorkspaceService(db).delete_workspace(workspace_id)

    assert db.get(Workspace, workspace_id) is not None
    assert db.get(File, root.id) is not None


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
    root = File(
        uri="/",
        name="root",
        owner_id=user.id,
        workspace_id=workspace.id,
        is_directory=True,
        size=0,
    )
    db.add(root)
    db.commit()
    workspace_id = workspace.id
    root_id = root.id
    lifecycle = FakeIndexLifecycle(fail_delete=True)

    with pytest.raises(RuntimeError, match="cleanup failed"):
        WorkspaceService(
            db,
            index_lifecycle=lifecycle,
            chunk_index_mode="v2_alias",
        ).delete_workspace(workspace_id)

    assert db.get(Workspace, workspace_id) is not None
    assert db.get(File, root_id) is not None


def test_delete_workspace_keeps_db_when_storage_cleanup_fails(db):
    user, workspace = _workspace(db, "storage-failure")
    root = _file(db, user, workspace)
    workspace_id = workspace.id
    root_id = root.id
    lifecycle = FakeIndexLifecycle()
    storage = FakeWorkspaceStorage(fail=True)

    with pytest.raises(WorkspaceStorageCleanupError, match="storage cleanup failed"):
        WorkspaceService(
            db,
            index_lifecycle=lifecycle,
            chunk_index_mode="legacy",
            minio_storage=storage,
        ).delete_workspace(workspace_id)

    assert lifecycle.delete_calls == [(workspace_id, "storage-failure")]
    assert storage.delete_calls == ["storage-failure"]
    assert db.get(Workspace, workspace_id) is not None
    assert db.get(File, root_id) is not None


def test_delete_workspace_cleans_external_state_before_db_rows(db):
    user, workspace = _workspace(db, "external-first")
    root = _file(db, user, workspace)
    workspace_id = workspace.id
    root_id = root.id

    class DbObservingStorage:
        def remove_workspace_storage(self, workspace_slug):
            assert workspace_slug == "external-first"
            assert db.get(Workspace, workspace_id) is not None
            assert db.get(File, root_id) is not None

    deleted = WorkspaceService(
        db,
        index_lifecycle=FakeIndexLifecycle(),
        chunk_index_mode="legacy",
        minio_storage=DbObservingStorage(),
    ).delete_workspace(workspace_id)

    assert deleted is True


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
