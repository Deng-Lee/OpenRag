"""Tests for JWT service token admin routes (multi-workspace binding model)."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import openrag.models  # noqa: F401
from openrag.api.deps import get_db
from openrag.api.main import app
from openrag.models import Base, ServiceTokenWorkspace, User, Workspace, WorkspaceMember
from openrag.security import create_access_token, hash_password

TEST_DATABASE_URL = "sqlite:///:memory:"
engine = create_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(scope="function")
def db() -> Session:
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()
    yield session
    session.close()
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client(db: Session):
    def override_get_db():
        try:
            yield db
        finally:
            pass

    prev = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    if prev is not None:
        app.dependency_overrides[get_db] = prev
    else:
        app.dependency_overrides.pop(get_db, None)


def _auth(user_id: int) -> dict[str, str]:
    token = create_access_token({"sub": str(user_id)})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def owner(db: Session) -> User:
    u = User(
        username="st_owner",
        email="st_owner@example.com",
        password_hash=hash_password("pw"),
        full_name="Owner",
        is_active=True,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture
def reader(db: Session) -> User:
    u = User(
        username="st_reader",
        email="st_reader@example.com",
        password_hash=hash_password("pw"),
        full_name="Reader",
        is_active=True,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture
def other(db: Session) -> User:
    u = User(
        username="st_other",
        email="st_other@example.com",
        password_hash=hash_password("pw"),
        full_name="Other",
        is_active=True,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture
def workspace_a(db: Session, owner: User) -> Workspace:
    ws = Workspace(name="WSA", slug="wsa", owner_id=owner.id)
    db.add(ws)
    db.commit()
    db.refresh(ws)
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=owner.id, role="write"))
    db.commit()
    return ws


@pytest.fixture
def workspace_b(db: Session, owner: User) -> Workspace:
    ws = Workspace(name="WSB", slug="wsb", owner_id=owner.id)
    db.add(ws)
    db.commit()
    db.refresh(ws)
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=owner.id, role="write"))
    db.commit()
    return ws


# --- CREATE ---


def test_create_token_with_single_workspace(
    client: TestClient, db: Session, workspace_a: Workspace, owner: User
) -> None:
    r = client.post(
        "/service-tokens",
        json={
            "name": "single-ws",
            "workspaces": [{"workspace_id": workspace_a.id, "permission": "read"}],
        },
        headers=_auth(owner.id),
    )
    assert r.status_code == 201
    data = r.json()
    assert data["secret"].startswith("sk-")
    assert len(data["workspaces"]) == 1
    assert data["workspaces"][0]["workspace_id"] == workspace_a.id
    assert data["workspaces"][0]["permission"] == "read"


def test_create_token_with_multiple_workspaces(
    client: TestClient, db: Session, workspace_a: Workspace, workspace_b: Workspace, owner: User
) -> None:
    r = client.post(
        "/service-tokens",
        json={
            "name": "multi-ws",
            "workspaces": [
                {"workspace_id": workspace_a.id, "permission": "write"},
                {"workspace_id": workspace_b.id, "permission": "read"},
            ],
        },
        headers=_auth(owner.id),
    )
    assert r.status_code == 201
    data = r.json()
    assert len(data["workspaces"]) == 2


def test_create_token_empty_workspaces_array_returns_422(
    client: TestClient, workspace_a: Workspace, owner: User
) -> None:
    r = client.post(
        "/service-tokens",
        json={"name": "empty", "workspaces": []},
        headers=_auth(owner.id),
    )
    assert r.status_code == 422


def test_create_token_duplicate_workspace_id_returns_400(
    client: TestClient, db: Session, workspace_a: Workspace, owner: User
) -> None:
    r = client.post(
        "/service-tokens",
        json={
            "workspaces": [
                {"workspace_id": workspace_a.id, "permission": "read"},
                {"workspace_id": workspace_a.id, "permission": "write"},
            ],
        },
        headers=_auth(owner.id),
    )
    assert r.status_code == 400


def test_create_token_user_lacks_workspace_permission_returns_403(
    client: TestClient, db: Session, workspace_a: Workspace, reader: User
) -> None:
    db.add(WorkspaceMember(workspace_id=workspace_a.id, user_id=reader.id, role="read"))
    db.commit()

    r = client.post(
        "/service-tokens",
        json={
            "workspaces": [{"workspace_id": workspace_a.id, "permission": "write"}],
        },
        headers=_auth(reader.id),
    )
    assert r.status_code == 403


def test_create_unauthorized(client: TestClient) -> None:
    r = client.post("/service-tokens", json={"workspaces": [{"workspace_id": 1, "permission": "read"}]})
    assert r.status_code == 401


# --- LIST ---


def test_list_all_tokens(
    client: TestClient, db: Session, workspace_a: Workspace, owner: User
) -> None:
    client.post(
        "/service-tokens",
        json={"workspaces": [{"workspace_id": workspace_a.id, "permission": "read"}]},
        headers=_auth(owner.id),
    )

    r = client.get("/service-tokens", headers=_auth(owner.id))
    assert r.status_code == 200
    assert len(r.json()) >= 1


def test_list_tokens_filtered_by_workspace(
    client: TestClient, db: Session, workspace_a: Workspace, workspace_b: Workspace, owner: User
) -> None:
    r1 = client.post(
        "/service-tokens",
        json={"workspaces": [{"workspace_id": workspace_a.id, "permission": "read"}]},
        headers=_auth(owner.id),
    )
    r2 = client.post(
        "/service-tokens",
        json={"workspaces": [{"workspace_id": workspace_b.id, "permission": "write"}]},
        headers=_auth(owner.id),
    )
    tid_a = r1.json()["id"]
    tid_b = r2.json()["id"]

    r_filt = client.get(
        "/service-tokens",
        params={"workspace_id": workspace_a.id},
        headers=_auth(owner.id),
    )
    assert r_filt.status_code == 200
    ids = [x["id"] for x in r_filt.json()]
    assert tid_a in ids
    assert tid_b not in ids


# --- PATCH BINDINGS ---


def test_patch_add_binding(
    client: TestClient, db: Session, workspace_a: Workspace, workspace_b: Workspace, owner: User
) -> None:
    r = client.post(
        "/service-tokens",
        json={"workspaces": [{"workspace_id": workspace_a.id, "permission": "read"}]},
        headers=_auth(owner.id),
    )
    tid = r.json()["id"]

    r_patch = client.patch(
        f"/service-tokens/{tid}/workspaces",
        json={"add": [{"workspace_id": workspace_b.id, "permission": "write"}]},
        headers=_auth(owner.id),
    )
    assert r_patch.status_code == 200
    assert len(r_patch.json()["workspaces"]) == 2


def test_patch_update_binding_permission(
    client: TestClient, db: Session, workspace_a: Workspace, owner: User
) -> None:
    r = client.post(
        "/service-tokens",
        json={"workspaces": [{"workspace_id": workspace_a.id, "permission": "read"}]},
        headers=_auth(owner.id),
    )
    tid = r.json()["id"]

    r_patch = client.patch(
        f"/service-tokens/{tid}/workspaces",
        json={"update": [{"workspace_id": workspace_a.id, "permission": "write"}]},
        headers=_auth(owner.id),
    )
    assert r_patch.status_code == 200
    ws_map = {w["workspace_id"]: w["permission"] for w in r_patch.json()["workspaces"]}
    assert ws_map[workspace_a.id] == "write"


def test_patch_remove_binding(
    client: TestClient, db: Session, workspace_a: Workspace, workspace_b: Workspace, owner: User
) -> None:
    r = client.post(
        "/service-tokens",
        json={
            "workspaces": [
                {"workspace_id": workspace_a.id, "permission": "read"},
                {"workspace_id": workspace_b.id, "permission": "write"},
            ],
        },
        headers=_auth(owner.id),
    )
    tid = r.json()["id"]

    r_patch = client.patch(
        f"/service-tokens/{tid}/workspaces",
        json={"remove": [{"workspace_id": workspace_a.id}]},
        headers=_auth(owner.id),
    )
    assert r_patch.status_code == 200
    assert len(r_patch.json()["workspaces"]) == 1


def test_patch_add_duplicate_binding_returns_409(
    client: TestClient, db: Session, workspace_a: Workspace, owner: User
) -> None:
    r = client.post(
        "/service-tokens",
        json={"workspaces": [{"workspace_id": workspace_a.id, "permission": "read"}]},
        headers=_auth(owner.id),
    )
    tid = r.json()["id"]

    r_patch = client.patch(
        f"/service-tokens/{tid}/workspaces",
        json={"add": [{"workspace_id": workspace_a.id, "permission": "write"}]},
        headers=_auth(owner.id),
    )
    assert r_patch.status_code == 409


def test_patch_revoked_token_returns_400(
    client: TestClient, db: Session, workspace_a: Workspace, owner: User
) -> None:
    r = client.post(
        "/service-tokens",
        json={"workspaces": [{"workspace_id": workspace_a.id, "permission": "read"}]},
        headers=_auth(owner.id),
    )
    tid = r.json()["id"]
    client.delete(f"/service-tokens/{tid}", headers=_auth(owner.id))

    r_patch = client.patch(
        f"/service-tokens/{tid}/workspaces",
        json={"add": [{"workspace_id": workspace_a.id, "permission": "write"}]},
        headers=_auth(owner.id),
    )
    assert r_patch.status_code == 400


def test_patch_forbidden_for_non_creator(
    client: TestClient, db: Session, workspace_a: Workspace, owner: User, other: User
) -> None:
    db.add(WorkspaceMember(workspace_id=workspace_a.id, user_id=other.id, role="write"))
    db.commit()

    r = client.post(
        "/service-tokens",
        json={"workspaces": [{"workspace_id": workspace_a.id, "permission": "read"}]},
        headers=_auth(owner.id),
    )
    tid = r.json()["id"]

    r_patch = client.patch(
        f"/service-tokens/{tid}/workspaces",
        json={"update": [{"workspace_id": workspace_a.id, "permission": "write"}]},
        headers=_auth(other.id),
    )
    assert r_patch.status_code == 403


# --- REVOKE & SECRET (unchanged) ---


def test_revoke_and_secret_flow(
    client: TestClient, db: Session, workspace_a: Workspace, owner: User
) -> None:
    r = client.post(
        "/service-tokens",
        json={"workspaces": [{"workspace_id": workspace_a.id, "permission": "read"}]},
        headers=_auth(owner.id),
    )
    tid = r.json()["id"]
    secret = r.json()["secret"]

    r_sec = client.get(f"/service-tokens/{tid}/secret", headers=_auth(owner.id))
    assert r_sec.status_code == 200
    assert r_sec.json()["secret"] == secret

    r_del = client.delete(f"/service-tokens/{tid}", headers=_auth(owner.id))
    assert r_del.status_code == 200

    r_list = client.get("/service-tokens", headers=_auth(owner.id))
    row = next(x for x in r_list.json() if x["id"] == tid)
    assert row["revoked_at"] is not None


def test_system_admin_can_reveal_secret(
    client: TestClient, db: Session, workspace_a: Workspace, owner: User, other: User
) -> None:
    other.is_admin = True
    db.add(other)
    db.commit()

    r = client.post(
        "/service-tokens",
        json={"workspaces": [{"workspace_id": workspace_a.id, "permission": "write"}]},
        headers=_auth(owner.id),
    )
    tid = r.json()["id"]

    r_sec = client.get(f"/service-tokens/{tid}/secret", headers=_auth(other.id))
    assert r_sec.status_code == 200


def test_other_user_cannot_reveal_secret(
    client: TestClient, db: Session, workspace_a: Workspace, owner: User, other: User
) -> None:
    db.add(WorkspaceMember(workspace_id=workspace_a.id, user_id=other.id, role="write"))
    db.commit()

    r = client.post(
        "/service-tokens",
        json={"workspaces": [{"workspace_id": workspace_a.id, "permission": "write"}]},
        headers=_auth(owner.id),
    )
    tid = r.json()["id"]

    r_sec = client.get(f"/service-tokens/{tid}/secret", headers=_auth(other.id))
    assert r_sec.status_code == 403