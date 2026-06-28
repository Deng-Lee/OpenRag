"""Model-level uniqueness for File.tag: unique per (workspace_id, tag); NULLs free."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from openrag.models import Base, File, User, Workspace


@pytest.fixture()
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    Base.metadata.drop_all(engine)


@pytest.fixture()
def ws(db: Session):
    u = User(username="u", email="u@e.com", password_hash="h", full_name="U", is_active=True)
    db.add(u); db.commit(); db.refresh(u)
    w1 = Workspace(name="W1", slug="w1", owner_id=u.id)
    w2 = Workspace(name="W2", slug="w2", owner_id=u.id)
    db.add_all([w1, w2]); db.commit(); db.refresh(w1); db.refresh(w2)
    return u, w1, w2


def _file(u, w, *, uri, tag):
    return File(uri=uri, name=uri.rsplit("/", 1)[-1], owner_id=u.id, workspace_id=w.id, tag=tag)


def test_same_workspace_same_tag_conflicts(db, ws):
    u, w1, _ = ws
    db.add(_file(u, w1, uri="/a.txt", tag="dup")); db.commit()
    db.add(_file(u, w1, uri="/b.txt", tag="dup"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_different_workspace_same_tag_ok(db, ws):
    u, w1, w2 = ws
    db.add(_file(u, w1, uri="/a.txt", tag="shared"))
    db.add(_file(u, w2, uri="/a.txt", tag="shared"))
    db.commit()  # no error
    assert db.query(File).filter(File.tag == "shared").count() == 2


def test_multiple_null_tags_ok(db, ws):
    u, w1, _ = ws
    db.add(_file(u, w1, uri="/a.txt", tag=None))
    db.add(_file(u, w1, uri="/b.txt", tag=None))
    db.commit()  # multiple NULLs allowed
    assert db.query(File).filter(File.workspace_id == w1.id).count() == 2
