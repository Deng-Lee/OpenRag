"""File.deleted_at column + multiple soft-deleted rows can share NULL tag."""

import pytest
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from openrag.models import Base, File, User, Workspace


@pytest.fixture()
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    Base.metadata.drop_all(engine)


@pytest.fixture()
def wsowner(db: Session):
    u = User(username="u", email="u@e.com", password_hash="h", full_name="U", is_active=True)
    db.add(u); db.commit(); db.refresh(u)
    w = Workspace(name="W", slug="w", owner_id=u.id)
    db.add(w); db.commit(); db.refresh(w)
    return w, u


def test_deleted_at_defaults_null_and_settable(db, wsowner):
    w, u = wsowner
    f = File(uri="/a.txt", name="a.txt", owner_id=u.id, workspace_id=w.id, tag="t")
    db.add(f); db.commit(); db.refresh(f)
    assert f.deleted_at is None
    f.deleted_at = datetime.now(timezone.utc)
    f.tag = None
    db.commit(); db.refresh(f)
    assert f.deleted_at is not None
    assert f.tag is None


def test_soft_deleted_row_frees_tag_for_reuse(db, wsowner):
    """A soft-deleted row has tag=NULL, so the same tag can be inserted again."""
    w, u = wsowner
    old = File(uri="/a.txt", name="a.txt", owner_id=u.id, workspace_id=w.id, tag="dup")
    db.add(old); db.commit()
    old.deleted_at = datetime.now(timezone.utc); old.tag = None
    db.commit()
    new = File(uri="/b.txt", name="b.txt", owner_id=u.id, workspace_id=w.id, tag="dup")
    db.add(new); db.commit()  # no IntegrityError: old.tag is NULL
    assert db.query(File).filter(File.tag == "dup").count() == 1
