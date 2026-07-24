"""DELETE_PATH_PREFIX worker physically deletes only pre-watermark soft-deleted rows."""

import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from openrag.models import Base, File, User, Workspace
from openrag.services import file_deletion
from openrag.services.file_deletion import utcnow


@pytest.fixture()
def session_factory(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Local = sessionmaker(bind=engine)
    # worker uses SessionLocal()
    monkeypatch.setattr("openrag.worker.task_worker.SessionLocal", Local)
    # stub external storage/vector deletes
    monkeypatch.setattr(
        file_deletion,
        "delete_vectors_for_file_across_generations",
        lambda *a, **k: {},
    )
    monkeypatch.setattr(file_deletion, "MinioStorage", lambda *a, **k: type("M", (), {
        "remove_document_hierarchy": lambda *a, **k: None,
        "remove_file": lambda *a, **k: None,
        "remove_directory": lambda *a, **k: None,
    })())
    monkeypatch.setattr(file_deletion, "HierarchyStorage", lambda *a, **k: type("H", (), {
        "delete_document_hierarchy": lambda *a, **k: None,
    })())
    return Local


def test_prefix_cleanup_skips_post_watermark_active_rows(session_factory):
    from openrag.worker.task_worker import TaskWorker

    db = session_factory()
    u = User(username="u", email="u@e.com", password_hash="h", full_name="U", is_active=True)
    db.add(u); db.commit(); db.refresh(u)
    w = Workspace(name="W", slug="w", owner_id=u.id)
    db.add(w); db.commit(); db.refresh(w)
    old = File(uri="/docs/old.txt", name="old.txt", owner_id=u.id, workspace_id=w.id,
               size=0, deleted_at=utcnow())
    db.add(old); db.commit(); db.refresh(old)
    watermark = utcnow()
    new_active = File(uri="/docs/new.txt", name="new.txt", owner_id=u.id, workspace_id=w.id, size=0)
    db.add(new_active); db.commit(); db.refresh(new_active)
    old_id, new_id, ws_id = old.id, new_active.id, w.id
    db.close()

    worker = TaskWorker.__new__(TaskWorker)  # bypass __init__ (no broker needed)
    result = worker._delete_path_prefix_task({
        "workspace_id": ws_id,
        "payload": {"path": "/docs", "deleted_before": watermark.isoformat()},
    })
    assert result["status"] == "deleted"

    db2 = session_factory()
    assert db2.query(File).filter(File.id == old_id).first() is None  # physically gone
    assert db2.query(File).filter(File.id == new_id).first() is not None  # untouched
    db2.close()


def test_legacy_task_without_watermark_is_fail_closed(session_factory):
    """Codex #1: a DELETE_PATH_PREFIX task missing deleted_before must NOT prefix-wide
    delete (which would drop active rows). It fails closed: deletes nothing, reports skip."""
    from openrag.worker.task_worker import TaskWorker

    db = session_factory()
    u = User(username="u", email="u@e.com", password_hash="h", full_name="U", is_active=True)
    db.add(u); db.commit(); db.refresh(u)
    w = Workspace(name="W", slug="w", owner_id=u.id)
    db.add(w); db.commit(); db.refresh(w)
    active = File(uri="/docs/keep.txt", name="keep.txt", owner_id=u.id, workspace_id=w.id, size=0)
    db.add(active); db.commit(); db.refresh(active)
    keep_id, ws_id = active.id, w.id
    db.close()

    worker = TaskWorker.__new__(TaskWorker)
    result = worker._delete_path_prefix_task({
        "workspace_id": ws_id,
        "payload": {"path": "/docs"},  # NO deleted_before
    })
    assert result["status"] == "skipped"

    db2 = session_factory()
    assert db2.query(File).filter(File.id == keep_id).first() is not None  # NOT deleted
    db2.close()


def test_root_prefix_task_is_fail_closed(session_factory):
    """Codex round-4 #1: a DELETE_PATH_PREFIX task with path='/' (even carrying a valid
    deleted_before) must NOT physically wipe the whole workspace's soft-deleted rows.
    The worker skips it before opening a session."""
    from openrag.worker.task_worker import TaskWorker

    db = session_factory()
    u = User(username="u", email="u@e.com", password_hash="h", full_name="U", is_active=True)
    db.add(u); db.commit(); db.refresh(u)
    w = Workspace(name="W", slug="w", owner_id=u.id)
    db.add(w); db.commit(); db.refresh(w)
    dead = File(uri="/docs/dead.txt", name="dead.txt", owner_id=u.id, workspace_id=w.id,
                size=0, deleted_at=utcnow())
    db.add(dead); db.commit(); db.refresh(dead)
    watermark = utcnow()
    dead_id, ws_id = dead.id, w.id
    db.close()

    worker = TaskWorker.__new__(TaskWorker)
    result = worker._delete_path_prefix_task({
        "workspace_id": ws_id,
        "payload": {"path": "/", "deleted_before": watermark.isoformat()},
    })
    assert result["status"] == "skipped"
    assert result.get("reason") == "root_prefix"

    db2 = session_factory()
    assert db2.query(File).filter(File.id == dead_id).first() is not None  # NOT wiped
    db2.close()
