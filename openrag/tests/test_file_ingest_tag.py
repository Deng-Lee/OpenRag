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


def test_duplicate_tag_same_workspace_409(db, wsowner):
    w, u = wsowner
    _ingest(db, w, u, filename="a.txt", tag="dup")
    with pytest.raises(HTTPException) as ei:
        _ingest(db, w, u, filename="b.txt", tag="dup")
    assert ei.value.status_code == status.HTTP_409_CONFLICT


def test_duplicate_tag_check_before_mkdir(db, wsowner):
    """Tag conflict must abort before ancestor dirs are created (no empty dirs left)."""
    w, u = wsowner
    _ingest(db, w, u, path="/", filename="a.txt", tag="dup")
    with pytest.raises(HTTPException):
        _ingest(db, w, u, path="/new/deep", filename="b.txt", tag="dup")
    # /new and /new/deep must NOT have been created by the aborted upload
    leftover = db.query(File).filter(
        File.workspace_id == w.id, File.is_directory.is_(True), File.uri.like("/new%")
    ).count()
    assert leftover == 0


def test_same_tag_other_workspace_ok(db, wsowner):
    w, u = wsowner
    other = Workspace(name="W2", slug="w2", owner_id=u.id)
    db.add(other); db.commit(); db.refresh(other)
    _ingest(db, w, u, filename="a.txt", tag="shared")
    row2, _ = _ingest(db, other, u, filename="a.txt", tag="shared")
    assert row2.tag == "shared"


def test_violated_unique_constraint_classifier():
    from openrag.services.file_ingest import _violated_unique_constraint

    class _E:
        def __init__(self, m): self.orig = m

    assert _violated_unique_constraint(_E('violates unique constraint "uq_files_workspace_tag"')) == "tag"
    assert _violated_unique_constraint(_E("UNIQUE constraint failed: files.workspace_id, files.tag")) == "tag"
    assert _violated_unique_constraint(_E('violates unique constraint "uq_files_workspace_uri"')) == "uri"
    assert _violated_unique_constraint(_E("UNIQUE constraint failed: files.workspace_id, files.uri")) == "uri"
    assert _violated_unique_constraint(_E("some unrelated error")) is None
