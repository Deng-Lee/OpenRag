"""_accessible_file_ids excludes soft-deleted; admin+workspace empty -> [] not None."""

import pytest
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from openrag.models import Base, File, User, Workspace
from openrag.retrieval.retrieval_service import RetrievalService


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    Base.metadata.drop_all(engine)


def _svc(db):
    svc = RetrievalService.__new__(RetrievalService)  # bypass heavy __init__
    svc.db = db
    return svc


def _seed(db, *, is_admin=False):
    u = User(username="u", email="u@e.com", password_hash="h", full_name="U", is_active=True, is_admin=is_admin)
    db.add(u); db.commit(); db.refresh(u)
    w = Workspace(name="W", slug="w", owner_id=u.id)
    db.add(w); db.commit(); db.refresh(w)
    return u, w


def test_accessible_excludes_soft_deleted_for_admin_workspace(db):
    u, w = _seed(db, is_admin=True)
    live = File(uri="/a.txt", name="a", owner_id=u.id, workspace_id=w.id, size=0)
    dead = File(uri="/b.txt", name="b", owner_id=u.id, workspace_id=w.id, size=0,
                deleted_at=datetime.now(timezone.utc))
    db.add_all([live, dead]); db.commit(); db.refresh(live)
    scope = _svc(db).resolve_file_scope(u.id, w.id)
    assert scope.allowed_file_ids == (live.id,)
    assert not scope.is_verified_global


def test_admin_workspace_all_deleted_returns_empty_not_all(db):
    u, w = _seed(db, is_admin=True)
    dead = File(uri="/b.txt", name="b", owner_id=u.id, workspace_id=w.id, size=0,
                deleted_at=datetime.now(timezone.utc))
    db.add(dead); db.commit()
    scope = _svc(db).resolve_file_scope(u.id, w.id)
    assert scope.is_empty
    assert not scope.is_verified_global


def test_filter_hits_drops_soft_deleted_files(db):
    """Codex round-3 #3: admin + workspace_id=None returns None (no id filter), so
    vector hits for not-yet-physically-cleaned soft-deleted files must be dropped
    by a post-filter on the enriched hits."""
    u, w = _seed(db, is_admin=True)
    live = File(uri="/a.txt", name="a", owner_id=u.id, workspace_id=w.id, size=0)
    dead = File(uri="/b.txt", name="b", owner_id=u.id, workspace_id=w.id, size=0,
                deleted_at=datetime.now(timezone.utc))
    db.add_all([live, dead]); db.commit(); db.refresh(live); db.refresh(dead)
    hits = [
        {"file_id": live.id, "score": 0.9},
        {"file_id": dead.id, "score": 0.8},
        {"file_id": None, "score": 0.1},  # malformed hit, no file_id -> dropped
    ]
    kept = _svc(db)._filter_hits_to_active_files(hits)
    assert [h["file_id"] for h in kept] == [live.id]


def test_filter_to_active_file_ids_drops_soft_deleted(db):
    """Codex round-4 #2: L0 candidate_files must be filtered to active ids BEFORE they
    reach L1 retrieval / LLM navigation / chunk search, so a soft-deleted file can never
    influence contextual ranking (not just be dropped from the final hits). Order kept."""
    u, w = _seed(db, is_admin=True)
    live = File(uri="/a.txt", name="a", owner_id=u.id, workspace_id=w.id, size=0)
    dead = File(uri="/b.txt", name="b", owner_id=u.id, workspace_id=w.id, size=0,
                deleted_at=datetime.now(timezone.utc))
    db.add_all([live, dead]); db.commit(); db.refresh(live); db.refresh(dead)
    assert _svc(db)._filter_to_active_file_ids([dead.id, live.id]) == [live.id]
    assert _svc(db)._filter_to_active_file_ids([]) == []
