"""TaskService.add_task is non-committing; create_task still commits."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from openrag.models import Base, Task, User, Workspace
from openrag.services.task_service import TaskService


@pytest.fixture()
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    Base.metadata.drop_all(engine)


@pytest.fixture()
def wsuser(db: Session):
    u = User(username="u", email="u@e.com", password_hash="h", full_name="U", is_active=True)
    db.add(u); db.commit(); db.refresh(u)
    w = Workspace(name="W", slug="w", owner_id=u.id)
    db.add(w); db.commit(); db.refresh(w)
    return w, u


def test_add_task_does_not_commit(db, wsuser):
    w, u = wsuser
    t = TaskService(db).add_task(workspace_id=w.id, user_id=u.id, task_type="delete_file", file_id=None)
    assert t.task_id  # uuid assigned
    # not committed yet -> rollback discards it
    db.rollback()
    assert db.query(Task).count() == 0


def test_create_task_commits(db, wsuser):
    w, u = wsuser
    TaskService(db).create_task(workspace_id=w.id, user_id=u.id, task_type="delete_file")
    db.rollback()  # already committed inside; rollback is a no-op for it
    assert db.query(Task).count() == 1
