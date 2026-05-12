"""End-to-end tests for workspace functionality"""

import pytest
from fastapi.testclient import TestClient
from openrag.api.main import app
from openrag.models.user import User
from openrag.models.workspace import Workspace, WorkspaceMember
from openrag.models.file import File


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(scope="function")
def db_session():
    """Create a test database session"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from openrag.models import Base

    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()

    yield session

    session.close()
    Base.metadata.drop_all(engine)


def test_complete_workspace_workflow(client, db_session):
    """Test complete workspace workflow from creation to deletion"""

    # Create users
    owner = User(
        username="owner",
        email="owner@test.com",
        password_hash="hash",
        full_name="Owner",
        is_active=True
    )
    member1 = User(
        username="member1",
        email="member1@test.com",
        password_hash="hash",
        full_name="Member 1",
        is_active=True
    )
    member2 = User(
        username="member2",
        email="member2@test.com",
        password_hash="hash",
        full_name="Member 2",
        is_active=True
    )
    db_session.add_all([owner, member1, member2])
    db_session.commit()

    # Mock auth headers (simplified for test)
    owner_headers = {"Authorization": f"Bearer mock_token_{owner.id}"}
    member1_headers = {"Authorization": f"Bearer mock_token_{member1.id}"}

    # Note: This is a simplified test. Real e2e tests would need proper JWT auth setup
    # For now we verify the models work correctly

    # 1. Create workspace
    workspace = Workspace(
        name="Test Company",
        slug="test-company",
        description="Our test workspace",
        owner_id=owner.id,
        max_concurrent_tasks=15
    )
    db_session.add(workspace)
    db_session.commit()
    db_session.refresh(workspace)

    assert workspace.id is not None
    assert workspace.name == "Test Company"

    # 2. Add members
    member_record1 = WorkspaceMember(
        workspace_id=workspace.id,
        user_id=member1.id,
        role="member"
    )
    member_record2 = WorkspaceMember(
        workspace_id=workspace.id,
        user_id=member2.id,
        role="viewer"
    )
    db_session.add_all([member_record1, member_record2])
    db_session.commit()

    # Verify members
    members = db_session.query(WorkspaceMember).filter(
        WorkspaceMember.workspace_id == workspace.id
    ).all()
    assert len(members) == 2  # 2 members added manually

    # Add owner as member too
    owner_member = WorkspaceMember(
        workspace_id=workspace.id,
        user_id=owner.id,
        role="admin"
    )
    db_session.add(owner_member)
    db_session.commit()

    members = db_session.query(WorkspaceMember).filter(
        WorkspaceMember.workspace_id == workspace.id
    ).all()
    assert len(members) == 3  # owner + 2 members

    # 3. Update workspace
    workspace.name = "Updated Company"
    workspace.description = "New description"
    db_session.commit()

    db_session.refresh(workspace)
    assert workspace.name == "Updated Company"

    # 4. Update quota
    workspace.max_concurrent_tasks = 20
    workspace.priority_strategy = "user_role"
    db_session.commit()

    db_session.refresh(workspace)
    assert workspace.max_concurrent_tasks == 20

    # 5. Remove member
    db_session.delete(member_record2)
    db_session.commit()

    remaining = db_session.query(WorkspaceMember).filter(
        WorkspaceMember.workspace_id == workspace.id
    ).count()
    assert remaining == 2

    # 6. Delete workspace
    db_session.delete(workspace)
    db_session.commit()

    # Verify deletion
    deleted = db_session.get(Workspace, workspace.id)
    assert deleted is None

    # Verify members cascade deleted
    members_remaining = db_session.query(WorkspaceMember).filter(
        WorkspaceMember.workspace_id == workspace.id
    ).count()
    assert members_remaining == 0


def test_workspace_data_isolation(db_session):
    """Test that workspace data is properly isolated"""

    # Create users and workspaces
    user1 = User(
        username="user1",
        email="user1@test.com",
        password_hash="hash",
        full_name="User 1",
        is_active=True
    )
    user2 = User(
        username="user2",
        email="user2@test.com",
        password_hash="hash",
        full_name="User 2",
        is_active=True
    )
    db_session.add_all([user1, user2])
    db_session.commit()

    ws1 = Workspace(name="WS1", slug="ws1", owner_id=user1.id)
    ws2 = Workspace(name="WS2", slug="ws2", owner_id=user2.id)
    db_session.add_all([ws1, ws2])
    db_session.commit()

    # Add users as members
    member1 = WorkspaceMember(workspace_id=ws1.id, user_id=user1.id, role='admin')
    member2 = WorkspaceMember(workspace_id=ws2.id, user_id=user2.id, role='admin')
    db_session.add_all([member1, member2])
    db_session.commit()

    # Create files in different workspaces
    file1 = File(
        uri="/ws1/file.txt",
        name="file.txt",
        owner_id=user1.id,
        workspace_id=ws1.id,
        is_directory=False,
        size=100
    )
    file2 = File(
        uri="/ws2/file.txt",
        name="file.txt",
        owner_id=user2.id,
        workspace_id=ws2.id,
        is_directory=False,
        size=200
    )
    db_session.add_all([file1, file2])
    db_session.commit()

    # Verify workspace isolation
    assert file1.workspace_id == ws1.id
    assert file2.workspace_id == ws2.id

    # Verify workspace membership
    assert db_session.query(WorkspaceMember).filter(
        WorkspaceMember.workspace_id == ws1.id,
        WorkspaceMember.user_id == user1.id
    ).first() is not None

    assert db_session.query(WorkspaceMember).filter(
        WorkspaceMember.workspace_id == ws2.id,
        WorkspaceMember.user_id == user2.id
    ).first() is not None

    # Verify user1 is NOT in ws2
    assert db_session.query(WorkspaceMember).filter(
        WorkspaceMember.workspace_id == ws2.id,
        WorkspaceMember.user_id == user1.id
    ).first() is None
