import pytest
from fastapi.testclient import TestClient
from openrag.api.main import app
from openrag.models.user import User
from openrag.security import hash_password, create_access_token


@pytest.fixture
def db():
    from openrag.models.base import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = TestingSessionLocal()

    from openrag.api.deps import get_db

    app.dependency_overrides[get_db] = lambda: db

    yield db
    db.close()
    Base.metadata.drop_all(bind=engine)
    app.dependency_overrides.clear()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def test_user(db):
    user = User(
        username="adminuser",
        email="admin@test.com",
        password_hash=hash_password("test"),
        full_name="Admin",
        is_admin=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def auth_headers(test_user):
    token = create_access_token(data={"sub": str(test_user.id)})
    return {"Authorization": f"Bearer {token}"}


def test_list_roles_non_admin(client, auth_headers):
    res = client.get("/roles/", headers=auth_headers)
    assert res.status_code == 403


def test_list_roles_admin(client, auth_headers, db, test_user):
    test_user.is_admin = True
    db.commit()
    res = client.get("/roles/", headers=auth_headers)
    assert res.status_code == 200


def test_create_role(client, auth_headers, db, test_user):
    test_user.is_admin = True
    db.commit()
    res = client.post(
        "/roles/",
        json={"name": "Test Role", "role_code": "test_role", "is_active": True},
        headers=auth_headers,
    )
    assert res.status_code == 200
    assert res.json()["role_code"] == "test_role"
