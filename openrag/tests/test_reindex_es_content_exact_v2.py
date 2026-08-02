from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import openrag.models  # noqa: F401
from openrag.models import DocumentChunk, File, User, Workspace
from openrag.models.base import Base
from scripts.reindex_es_content_exact_v2 import (
    reindex_workspace,
    restore_workspace_aliases,
    scan_workspace,
    switch_workspace_aliases,
    validate_workspace_v2,
)


class FakeStorage:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})

    def get_object_text(self, bucket, object_key):
        return self.objects.get((bucket, object_key))


class FakeStore:
    def __init__(self, *, partial=False, smoke_succeeds=True):
        self.docs = {}
        self.partial = partial
        self.smoke_succeeds = smoke_succeeds
        self.ensure_calls = []
        self.aliases = {}
        self.alias_updates = 0

    def ensure_versioned_index(self, index_name):
        self.ensure_calls.append(index_name)

    def bulk_upsert_chunks(self, index_name, docs):
        for document in docs:
            self.docs[document["chunk_id"]] = dict(document)
        return max(0, len(docs) - 1) if self.partial and docs else len(docs)

    def iter_chunk_sources(self, index_name, batch_size=500):
        yield from self.docs.values()

    def validate_index_contract(self, index_name):
        return None

    def update_workspace_aliases(self, *, read_alias, write_alias, target_index):
        previous = {
            read_alias: dict(self.aliases.get(read_alias, {})),
            write_alias: dict(self.aliases.get(write_alias, {})),
        }
        self.aliases = {
            read_alias: {target_index: {}},
            write_alias: {target_index: {"is_write_index": True}},
        }
        self.alias_updates += 1
        return previous

    def get_alias_state(self, aliases):
        return {
            alias: dict(self.aliases.get(alias, {}))
            for alias in aliases
        }

    def restore_workspace_aliases(self, state):
        self.aliases = {alias: dict(targets) for alias, targets in state.items()}
        self.alias_updates += 1

    def search_chunks(self, **kwargs):
        if not self.smoke_succeeds:
            return []
        file_ids = set(kwargs["file_ids"])
        return [
            {
                "chunk_id": document["chunk_id"],
                "file_id": document["file_id"],
                "text": document["content"],
                "sparse_score": 1.0,
            }
            for document in self.docs.values()
            if document["file_id"] in file_ids and document["content"]
        ][: kwargs["top_k"]]


@pytest.fixture
def migration_db(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    user = User(
        username="a02",
        email="a02@example.com",
        password_hash="x",
        full_name="A02",
    )
    db.add(user)
    db.commit()
    workspace = Workspace(name="A02", slug="a02-migrate", owner_id=user.id)
    db.add(workspace)
    db.commit()
    file = File(
        uri="/docs/policy.md",
        name="policy.md",
        owner_id=user.id,
        workspace_id=workspace.id,
        size=10,
    )
    db.add(file)
    db.commit()
    local_path = tmp_path / "chunk-0.md"
    local_path.write_text("标准号 GB/T 35273-2020", encoding="utf-8")
    db.add_all(
        [
            DocumentChunk(
                id=1,
                file_id=file.id,
                workspace_id=workspace.id,
                chunk_id="local-chunk",
                chunk_index=0,
                object_key="chunks/local.md",
                local_chunk_path=str(local_path),
            ),
            DocumentChunk(
                id=2,
                file_id=file.id,
                workspace_id=workspace.id,
                chunk_id="minio-chunk",
                chunk_index=1,
                object_key="chunks/minio.md",
            ),
        ]
    )
    db.commit()
    try:
        yield db, workspace, FakeStorage(
            {(workspace.slug, "chunks/minio.md"): "型号 HT-2025-001"}
        )
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_dry_run_reads_local_and_minio_without_creating_index(migration_db):
    db, workspace, storage = migration_db
    store = FakeStore()

    report = scan_workspace(
        db=db,
        storage=storage,
        workspace_id=workspace.id,
    )
    dry_run = reindex_workspace(
        db=db,
        store=store,
        storage=storage,
        workspace_id=workspace.id,
        apply=False,
    )

    assert report["db_active_chunk_count"] == 2
    assert report["readable_l2_count"] == 2
    assert report["missing_l2_count"] == 0
    assert dry_run["dry_run"] is True
    assert dry_run["expected_v2_document_count"] == 2
    assert store.ensure_calls == []
    assert store.docs == {}


def test_scan_falls_back_to_minio_when_local_l2_is_unreadable(migration_db):
    db, workspace, storage = migration_db
    local_chunk = (
        db.query(DocumentChunk)
        .filter(DocumentChunk.chunk_id == "local-chunk")
        .one()
    )
    Path(local_chunk.local_chunk_path).unlink()
    storage.objects[(workspace.slug, local_chunk.object_key)] = "标准号 GB/T 35273-2020"

    report = scan_workspace(
        db=db,
        storage=storage,
        workspace_id=workspace.id,
    )

    assert report["readable_l2_count"] == 2
    assert report["missing_l2_count"] == 0


def test_apply_uses_contract_builder_and_is_idempotent(migration_db):
    db, workspace, storage = migration_db
    store = FakeStore()

    first = reindex_workspace(
        db=db,
        store=store,
        storage=storage,
        workspace_id=workspace.id,
        apply=True,
        batch_size=1,
    )
    second = reindex_workspace(
        db=db,
        store=store,
        storage=storage,
        workspace_id=workspace.id,
        apply=True,
        batch_size=1,
    )

    assert first["written_count"] == second["written_count"] == 2
    assert set(store.docs) == {"local-chunk", "minio-chunk"}
    assert store.docs["local-chunk"]["exact_terms"] == ["gb/t 35273-2020"]
    assert store.docs["minio-chunk"]["exact_terms"] == ["ht-2025-001"]
    assert store.docs["local-chunk"]["mom_with_weight"] == ""
    assert not any("ltks" in field for field in store.docs["local-chunk"])


def test_missing_l2_and_partial_bulk_stop_before_alias_switch(migration_db):
    db, workspace, _storage = migration_db
    missing_storage = FakeStorage()

    with pytest.raises(RuntimeError, match="L2"):
        reindex_workspace(
            db=db,
            store=FakeStore(),
            storage=missing_storage,
            workspace_id=workspace.id,
            apply=True,
        )

    partial = FakeStore(partial=True)
    with pytest.raises(RuntimeError, match="partial"):
        reindex_workspace(
            db=db,
            store=partial,
            storage=FakeStorage(
                {(workspace.slug, "chunks/minio.md"): "型号 HT-2025-001"}
            ),
            workspace_id=workspace.id,
            apply=True,
        )
    assert partial.alias_updates == 0


def test_validation_switch_and_real_restore_use_same_saved_alias_state(migration_db):
    db, workspace, storage = migration_db
    store = FakeStore()
    reindex_workspace(
        db=db,
        store=store,
        storage=storage,
        workspace_id=workspace.id,
        apply=True,
    )
    report = validate_workspace_v2(
        db=db,
        store=store,
        workspace_id=workspace.id,
    )
    assert report["anomaly_count"] == 0

    legacy_read = "legacy-read"
    legacy_write = "legacy-write"
    store.aliases = {
        legacy_read: {"legacy-index": {}},
        legacy_write: {"legacy-index": {"is_write_index": True}},
    }
    previous = switch_workspace_aliases(
        db=db,
        store=store,
        storage=storage,
        workspace_id=workspace.id,
        read_alias=legacy_read,
        write_alias=legacy_write,
    )
    assert store.alias_updates == 1
    restore_workspace_aliases(
        db=db,
        store=store,
        storage=storage,
        workspace_id=workspace.id,
        state=previous,
        read_alias=legacy_read,
        write_alias=legacy_write,
    )
    assert store.alias_updates == 2
    assert store.aliases == {
        legacy_read: {"legacy-index": {}},
        legacy_write: {"legacy-index": {"is_write_index": True}},
    }


def test_empty_workspace_can_initialize_and_restore_aliases(migration_db):
    db, workspace, storage = migration_db
    empty_workspace = Workspace(
        name="Empty",
        slug="empty-workspace",
        owner_id=workspace.owner_id,
    )
    db.add(empty_workspace)
    db.commit()
    store = FakeStore()

    report = reindex_workspace(
        db=db,
        store=store,
        storage=storage,
        workspace_id=empty_workspace.id,
        apply=True,
    )
    previous = switch_workspace_aliases(
        db=db,
        store=store,
        storage=storage,
        workspace_id=empty_workspace.id,
    )

    assert report["written_count"] == 0
    assert store.alias_updates == 1
    restore_workspace_aliases(
        db=db,
        store=store,
        storage=storage,
        workspace_id=empty_workspace.id,
        state=previous,
    )
    assert store.alias_updates == 2
    assert all(not targets for targets in store.aliases.values())


def test_failed_audit_or_smoke_query_never_leaves_aliases_on_v2(migration_db):
    db, workspace, storage = migration_db
    legacy_state = {
        "legacy-read": {"legacy-index": {}},
        "legacy-write": {"legacy-index": {"is_write_index": True}},
    }
    incomplete = FakeStore()
    incomplete.aliases = legacy_state
    with pytest.raises(RuntimeError, match="audit"):
        switch_workspace_aliases(
            db=db,
            store=incomplete,
            storage=storage,
            workspace_id=workspace.id,
            read_alias="legacy-read",
            write_alias="legacy-write",
        )
    assert incomplete.alias_updates == 0

    smoke_failure = FakeStore(smoke_succeeds=False)
    smoke_failure.aliases = legacy_state
    reindex_workspace(
        db=db,
        store=smoke_failure,
        storage=storage,
        workspace_id=workspace.id,
        apply=True,
    )
    with pytest.raises(RuntimeError, match="smoke"):
        switch_workspace_aliases(
            db=db,
            store=smoke_failure,
            storage=storage,
            workspace_id=workspace.id,
            read_alias="legacy-read",
            write_alias="legacy-write",
        )
    assert smoke_failure.alias_updates == 2
    assert smoke_failure.aliases == legacy_state


def test_db_workspace_ownership_mismatch_stops_before_l2_or_es(migration_db):
    db, workspace, storage = migration_db
    db.query(DocumentChunk).filter(
        DocumentChunk.chunk_id == "local-chunk"
    ).update({"workspace_id": workspace.id + 99})
    db.commit()
    store = FakeStore()

    with pytest.raises(RuntimeError, match="ownership"):
        reindex_workspace(
            db=db,
            store=store,
            storage=storage,
            workspace_id=workspace.id,
            apply=True,
        )

    assert store.ensure_calls == []
