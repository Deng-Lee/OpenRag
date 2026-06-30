"""Soft-delete helpers: single-row, subtree, and physical-cleanup-by-watermark."""

import pytest
from datetime import datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from openrag.models import Base, File, Task, User, Workspace
from openrag.services import file_deletion
from openrag.services.file_deletion import (
    _release_tag_and_soft_delete,
    soft_delete_subtree,
    utcnow,
)


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


def _mk(db, w, u, uri, *, is_dir=False, tag=None):
    f = File(uri=uri, name=uri.rsplit("/", 1)[-1] or "root", owner_id=u.id,
             workspace_id=w.id, is_directory=is_dir, size=0, tag=tag)
    db.add(f); db.commit(); db.refresh(f)
    return f


def test_release_tag_and_soft_delete_single_commit(db, wsowner):
    w, u = wsowner
    f = _mk(db, w, u, "/a.txt", tag="t1")
    task = _release_tag_and_soft_delete(db, f, user_id=u.id)
    db.refresh(f)
    assert f.deleted_at is not None
    assert f.tag is None
    assert task.task_type == "delete_file"
    assert db.query(Task).filter(Task.file_id == f.id).count() == 1


def test_soft_delete_subtree_marks_all_active_rows(db, wsowner):
    w, u = wsowner
    d = _mk(db, w, u, "/dir", is_dir=True)
    a = _mk(db, w, u, "/dir/a.txt", tag="x")
    b = _mk(db, w, u, "/dir/sub/b.txt", tag="y")
    outside = _mk(db, w, u, "/other.txt", tag="z")
    before = soft_delete_subtree(db, w.id, "/dir")
    db.commit()
    for f in (d, a, b):
        db.refresh(f)
        assert f.deleted_at is not None and f.tag is None
    db.refresh(outside)
    assert outside.deleted_at is None and outside.tag == "z"
    assert isinstance(before, datetime)


def test_soft_delete_subtree_escapes_like_wildcards(db, wsowner):
    """Codex LIKE review: '_' in a logical path is literal, not a SQL wildcard."""
    w, u = wsowner
    target = _mk(db, w, u, "/a_b/one.txt", tag="target")
    neighbor = _mk(db, w, u, "/axb/two.txt", tag="neighbor")

    soft_delete_subtree(db, w.id, "/a_b")
    db.commit()

    db.refresh(target); db.refresh(neighbor)
    assert target.deleted_at is not None and target.tag is None
    assert neighbor.deleted_at is None and neighbor.tag == "neighbor"


def _stub_storage(monkeypatch):
    """Stub external storage/vector deletes so cleanup runs DB-only.

    Returns a list recording every remove_directory(bucket, prefix) call, so a test
    can assert the prefix-recursive object delete was NOT used on a directory row.
    """
    rmdir_calls: list = []
    monkeypatch.setattr(file_deletion, "delete_milvus_vectors_for_file", lambda *a, **k: [])

    class _M:
        def remove_document_hierarchy(self, *a, **k):
            return None
        def remove_file(self, *a, **k):
            return None
        def remove_directory(self, *a, **k):
            rmdir_calls.append(a)

    monkeypatch.setattr(file_deletion, "MinioStorage", lambda *a, **k: _M())
    monkeypatch.setattr(file_deletion, "HierarchyStorage", lambda *a, **k: type("H", (), {
        "delete_document_hierarchy": lambda *a, **k: None,
    })())
    return rmdir_calls


def test_physical_cleanup_only_touches_pre_watermark_soft_deleted(db, wsowner, monkeypatch):
    w, u = wsowner
    # one soft-deleted-before-watermark, one active added "after enqueue"
    old = _mk(db, w, u, "/dir/old.txt")
    soft_delete_subtree(db, w.id, "/dir"); db.commit()
    watermark = utcnow()  # naive UTC, same type the helper stamps deleted_at with
    new_active = _mk(db, w, u, "/dir/new.txt")  # active, deleted_at IS NULL
    _stub_storage(monkeypatch)

    ids = file_deletion.physically_delete_soft_deleted_under_prefix(
        db, w.id, "/dir", watermark, w
    )
    assert old.id in ids
    db.refresh(new_active)
    assert new_active.deleted_at is None  # untouched
    assert db.query(File).filter(File.id == old.id).first() is None  # physically gone


def test_cleanup_does_not_cascade_active_children_of_soft_deleted_dir(db, wsowner, monkeypatch):
    """Codex #1: a soft-deleted directory row must NOT drag an active child with it.

    soft-delete /dir (and its old child), then an active /dir/new.txt appears; the
    watermark cleanup deletes only the pre-watermark soft-deleted rows (the dir row
    + old child), and the no-cascade single-row delete leaves /dir/new.txt intact.
    """
    w, u = wsowner
    d = _mk(db, w, u, "/dir", is_dir=True)
    old_child = _mk(db, w, u, "/dir/old.txt")
    soft_delete_subtree(db, w.id, "/dir"); db.commit()
    watermark = utcnow()
    new_active = _mk(db, w, u, "/dir/new.txt")  # appears AFTER watermark, active
    rmdir_calls = _stub_storage(monkeypatch)

    ids = file_deletion.physically_delete_soft_deleted_under_prefix(
        db, w.id, "/dir", watermark, w
    )
    assert d.id in ids and old_child.id in ids
    assert db.query(File).filter(File.id == new_active.id).first() is not None  # survived
    assert new_active.deleted_at is None
    # Codex round-3 #1: directory row cleanup must NOT prefix-recursive delete objects
    assert rmdir_calls == []


def test_physical_cleanup_rejects_root_prefix(db, wsowner, monkeypatch):
    """Codex round-4 #1: even with a valid watermark, a root/empty/"/" prefix must NOT
    physically wipe the whole workspace's soft-deleted rows — the helper fails closed."""
    w, u = wsowner
    dead = _mk(db, w, u, "/docs/dead.txt")
    soft_delete_subtree(db, w.id, "/docs"); db.commit()
    dead_id = dead.id
    _stub_storage(monkeypatch)
    watermark = utcnow()
    for bad in ("/", "", "//"):
        with pytest.raises(ValueError):
            file_deletion.physically_delete_soft_deleted_under_prefix(
                db, w.id, bad, watermark, w
            )
    assert db.query(File).filter(File.id == dead_id).first() is not None  # NOT wiped


def test_physical_cleanup_escapes_like_percent_wildcard(db, wsowner, monkeypatch):
    """Codex LIKE review: '%' in a logical path is literal during physical cleanup."""
    w, u = wsowner
    target = _mk(db, w, u, "/100%/a.txt")
    neighbor = _mk(db, w, u, "/100x/b.txt")
    stamp = utcnow()
    target.deleted_at = stamp; target.tag = None
    neighbor.deleted_at = stamp; neighbor.tag = None
    db.commit()
    _stub_storage(monkeypatch)

    ids = file_deletion.physically_delete_soft_deleted_under_prefix(
        db, w.id, "/100%", utcnow(), w
    )

    assert target.id in ids
    assert neighbor.id not in ids
    assert db.query(File).filter(File.id == target.id).first() is None
    assert db.query(File).filter(File.id == neighbor.id).first() is not None
