"""ingest_new_file tag: normalize + charset validate + persist."""

import pytest
from fastapi import HTTPException, status
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from openrag.models import Base, File, User, Workspace
from openrag.services import file_ingest
from openrag.services.file_ingest import ingest_new_file


class _FakeMinio:
    def put_file(self, *a, **k):
        return None


@pytest.fixture()
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    Base.metadata.drop_all(engine)


@pytest.fixture(autouse=True)
def _minio(monkeypatch):
    monkeypatch.setattr(file_ingest, "MinioStorage", _FakeMinio)


@pytest.fixture()
def wsowner(db: Session):
    u = User(username="o", email="o@e.com", password_hash="h", full_name="O", is_active=True)
    db.add(u); db.commit(); db.refresh(u)
    w = Workspace(name="W", slug="w", owner_id=u.id)
    db.add(w); db.commit(); db.refresh(w)
    return w, u


def _ingest(db, w, u, *, filename="f.txt", tag=None, path="/"):
    return ingest_new_file(
        db, w, u.id,
        parent_logical_path=path, upload_filename=filename,
        file_content=b"x", content_type="text/plain",
        require_parent_dir=False, tag=tag,
    )


def test_valid_tag_persisted(db, wsowner):
    w, u = wsowner
    row, _ = _ingest(db, w, u, tag="report-2024.v1")
    assert row.tag == "report-2024.v1"


def test_blank_tag_becomes_null(db, wsowner):
    w, u = wsowner
    row, _ = _ingest(db, w, u, tag="   ")
    assert row.tag is None


def test_none_tag_is_null(db, wsowner):
    w, u = wsowner
    row, _ = _ingest(db, w, u, tag=None)
    assert row.tag is None


@pytest.mark.parametrize("bad", ["has space", "slash/x", "q?x", "hash#x", "汉字", "a" * 129])
def test_bad_tag_rejected_400(db, wsowner, bad):
    w, u = wsowner
    with pytest.raises(HTTPException) as ei:
        _ingest(db, w, u, tag=bad)
    assert ei.value.status_code == status.HTTP_400_BAD_REQUEST
