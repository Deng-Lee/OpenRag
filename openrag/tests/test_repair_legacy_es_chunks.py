import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import openrag.models  # noqa: F401
import scripts.repair_legacy_es_chunks as repair_module
from openrag.models import DocumentChunk, File, User, Workspace
from openrag.models.base import Base
from scripts.repair_legacy_es_chunks import (
    build_missing_repair_plan,
    build_orphan_delete_manifest,
    delete_approved_orphans,
    preflight_workspace,
    repair_approved_missing_chunks,
    repair_missing_chunks,
    save_action_report,
)


LEGACY_MAPPING = {
    "properties": {
        "chunk_id": {"type": "keyword"},
        "file_id": {"type": "long"},
        "workspace_id": {"type": "long"},
        "workspace_slug": {"type": "keyword"},
        "content": {"type": "text"},
    }
}


def test_script_help_works_when_invoked_by_file_path():
    app_root = Path(__file__).resolve().parents[1]

    completed = subprocess.run(
        [
            sys.executable,
            str(app_root / "scripts" / "repair_legacy_es_chunks.py"),
            "--help",
        ],
        cwd=app_root,
        env={
            **os.environ,
            "PYTHONPATH": str(app_root / "src"),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--apply-missing" in completed.stdout


class FakeStorage:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.read_calls = []

    def read_object_bytes(self, bucket, object_key):
        self.read_calls.append((bucket, object_key))
        try:
            return self.objects[(bucket, object_key)].encode("utf-8")
        except KeyError as exc:
            raise FileNotFoundError(object_key) from exc


class FakeIndices:
    def __init__(self, client, *, mapping=None, settings=None):
        self.client = client
        self.mapping = mapping or LEGACY_MAPPING
        self.settings = settings or {
            "index.number_of_shards": "1",
            "index.number_of_replicas": "0",
        }

    @staticmethod
    def exists_alias(*, name):
        return False

    def exists(self, *, index):
        return self.client.index_exists

    def get_mapping(self, *, index):
        return {index: {"mappings": self.mapping}}

    def get_settings(self, *, index, flat_settings):
        assert flat_settings is True
        return {index: {"settings": self.settings}}

    @staticmethod
    def analyze(*, index, field, text):
        assert field == "content"
        return {"tokens": [{"token": "openrag"}, {"token": "中"}]}


class FakeEsClient:
    def __init__(self, docs=None, *, index_exists=True, mapping=None, settings=None):
        self.docs = {str(doc["_id"]): dict(doc) for doc in docs or []}
        self.index_exists = index_exists
        self.fail_search = False
        self.indices = FakeIndices(self, mapping=mapping, settings=settings)
        self._remaining = []

    def mget(self, *, index, ids, source):
        return {
            "docs": [
                {"_id": chunk_id, "found": chunk_id in self.docs}
                for chunk_id in ids
            ]
        }

    def search(self, *, index, size, scroll, query, source, sort):
        if self.fail_search:
            raise RuntimeError("audit unavailable")
        docs = list(self.docs.values())
        self._remaining = docs[size:]
        return {"_scroll_id": "repair-audit", "hits": {"hits": docs[:size]}}

    def scroll(self, *, scroll_id, scroll):
        hits = self._remaining
        self._remaining = []
        return {"_scroll_id": scroll_id, "hits": {"hits": hits}}

    @staticmethod
    def clear_scroll(*, scroll_id):
        return {"succeeded": True}


class FakeStore:
    def __init__(
        self,
        client,
        *,
        partial_upsert=False,
        fail_audit_after_upsert=False,
    ):
        self.client = client
        self.partial_upsert = partial_upsert
        self.fail_audit_after_upsert = fail_audit_after_upsert
        self.ensure_calls = []
        self.upsert_calls = []
        self.delete_calls = []

    def ensure_index(self, index_name):
        self.ensure_calls.append(index_name)
        self.client.index_exists = True

    def bulk_upsert_chunks(self, index_name, documents):
        documents = [dict(document) for document in documents]
        self.upsert_calls.append((index_name, documents))
        accepted = len(documents) - 1 if self.partial_upsert and documents else len(documents)
        for document in documents[:accepted]:
            chunk_id = document["chunk_id"]
            self.client.docs[chunk_id] = {
                "_id": chunk_id,
                "_source": document,
            }
        if self.fail_audit_after_upsert:
            self.client.fail_search = True
        return accepted

    def bulk_delete_chunks(self, index_name, chunk_ids):
        chunk_ids = list(chunk_ids)
        self.delete_calls.append((index_name, chunk_ids))
        for chunk_id in chunk_ids:
            self.client.docs.pop(chunk_id, None)
        return len(chunk_ids), []


@pytest.fixture(autouse=True)
def fake_legacy_bulk_upsert(monkeypatch):
    def bulk_upsert(store, index_name, documents):
        accepted = store.bulk_upsert_chunks(index_name, documents)
        errors = []
        if accepted != len(documents):
            errors = [{"index": {"status": 400, "error": "test rejection"}}]
        return accepted, errors

    monkeypatch.setattr(
        repair_module, "_bulk_upsert_legacy_chunks", bulk_upsert
    )

def es_doc(chunk_id, file_id, workspace_id, content="existing"):
    return {
        "_id": chunk_id,
        "_source": {
            "chunk_id": chunk_id,
            "file_id": file_id,
            "workspace_id": workspace_id,
            "workspace_slug": "legacy-repair",
            "content": content,
        },
    }


@pytest.fixture
def repair_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    user = User(
        username="legacy-repair",
        email="legacy-repair@example.com",
        password_hash="x",
        full_name="Legacy Repair",
    )
    db.add(user)
    db.commit()
    workspace = Workspace(
        name="Legacy Repair",
        slug="legacy-repair",
        owner_id=user.id,
    )
    db.add(workspace)
    db.commit()
    live = File(
        uri="/live.md",
        name="live.md",
        owner_id=user.id,
        workspace_id=workspace.id,
        size=10,
    )
    deleted = File(
        uri="/deleted.md",
        name="deleted.md",
        owner_id=user.id,
        workspace_id=workspace.id,
        size=10,
    )
    from datetime import datetime, timezone

    deleted.deleted_at = datetime.now(timezone.utc)
    db.add_all([live, deleted])
    db.commit()
    db.add_all(
        [
            DocumentChunk(
                id=1,
                file_id=live.id,
                workspace_id=workspace.id,
                chunk_id="present",
                chunk_index=0,
                object_key="chunks/present.md",
                block_type="text",
            ),
            DocumentChunk(
                id=2,
                file_id=live.id,
                workspace_id=workspace.id,
                chunk_id="missing",
                chunk_index=1,
                object_key="chunks/missing.md",
                block_type="text",
            ),
            DocumentChunk(
                id=3,
                file_id=deleted.id,
                workspace_id=workspace.id,
                chunk_id="soft-deleted",
                chunk_index=0,
                object_key="chunks/soft-deleted.md",
                block_type="text",
            ),
        ]
    )
    db.commit()
    try:
        yield db, workspace, live
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_preflight_reads_minio_only_for_current_missing_chunks(repair_db):
    db, workspace, live = repair_db
    mapping_with_historical_fields = {
        "properties": dict(LEGACY_MAPPING["properties"])
    }
    mapping_with_historical_fields["properties"]["content_ltks"] = {
        "type": "text"
    }
    store = FakeStore(
        FakeEsClient(
            [es_doc("present", live.id, workspace.id)],
            mapping=mapping_with_historical_fields,
        )
    )
    storage = FakeStorage(
        {(workspace.slug, "chunks/missing.md"): "authoritative L2"}
    )

    report = preflight_workspace(
        db=db,
        store=store,
        storage=storage,
        workspace_id=workspace.id,
    )

    assert report["db_active_chunk_count"] == 2
    assert report["repair_candidate_chunk_ids"] == ["missing"]
    assert report["pre_legacy_audit"]["missing_es_chunk_ids"] == ["missing"]
    assert report["pre_legacy_audit"]["orphan_chunk_count"] == 0
    assert report["l2_preflight"]["readable_l2_count"] == 1
    assert report["l2_preflight"]["missing_l2_count"] == 0
    assert report["mapping_preflight"]["status"] == "MAPPING_COMPATIBLE"
    assert report["mapping_preflight"]["extra_fields"] == ["content_ltks"]
    assert storage.read_calls == [(workspace.slug, "chunks/missing.md")]
    assert store.upsert_calls == []
    assert store.delete_calls == []


def test_missing_l2_and_mapping_mismatch_block_before_writes(repair_db):
    db, workspace, live = repair_db
    missing_l2_store = FakeStore(
        FakeEsClient([es_doc("present", live.id, workspace.id)])
    )

    missing_l2 = repair_missing_chunks(
        db=db,
        store=missing_l2_store,
        storage=FakeStorage(),
        workspace_id=workspace.id,
    )

    bad_mapping = {**LEGACY_MAPPING, "properties": dict(LEGACY_MAPPING["properties"])}
    bad_mapping["properties"]["content"] = {"type": "keyword"}
    bad_mapping_store = FakeStore(
        FakeEsClient(
            [es_doc("present", live.id, workspace.id)],
            mapping=bad_mapping,
        )
    )
    mapping_mismatch = repair_missing_chunks(
        db=db,
        store=bad_mapping_store,
        storage=FakeStorage(
            {(workspace.slug, "chunks/missing.md"): "authoritative L2"}
        ),
        workspace_id=workspace.id,
    )

    assert missing_l2["status"] == "BLOCKED_L2"
    assert mapping_mismatch["status"] == "BLOCKED_MAPPING"
    assert missing_l2_store.upsert_calls == bad_mapping_store.upsert_calls == []
    assert missing_l2_store.ensure_calls == bad_mapping_store.ensure_calls == []


def test_custom_flat_default_analyzer_is_mapping_incompatible(repair_db):
    db, workspace, live = repair_db
    store = FakeStore(
        FakeEsClient(
            [es_doc("present", live.id, workspace.id)],
            settings={
                "index.number_of_shards": "1",
                "index.analysis.analyzer.default.type": "whitespace",
            },
        )
    )

    report = preflight_workspace(
        db=db,
        store=store,
        storage=FakeStorage(
            {(workspace.slug, "chunks/missing.md"): "authoritative L2"}
        ),
        workspace_id=workspace.id,
    )

    assert report["mapping_preflight"]["status"] == "MAPPING_INCOMPATIBLE"
    assert "customized index default analyzer" in str(
        report["mapping_preflight"]["mismatches"]
    )


def test_missing_only_upsert_is_idempotent_and_partial_bulk_fails_closed(repair_db):
    db, workspace, live = repair_db
    client = FakeEsClient(
        [
            es_doc("present", live.id, workspace.id),
            es_doc("orphan", live.id, workspace.id),
        ]
    )
    store = FakeStore(client)
    storage = FakeStorage(
        {
            (workspace.slug, "chunks/missing.md"):
                "2026-08-05 authoritative L2"
        }
    )

    first = repair_missing_chunks(
        db=db,
        store=store,
        storage=storage,
        workspace_id=workspace.id,
    )
    second = repair_missing_chunks(
        db=db,
        store=store,
        storage=storage,
        workspace_id=workspace.id,
    )

    assert first["submitted_count"] == first["accepted_count"] == 1
    assert first["post_legacy_audit"]["missing_es_chunk_count"] == 0
    assert first["post_legacy_audit"]["orphan_chunk_ids"] == ["orphan"]
    assert [doc["chunk_id"] for _index, docs in store.upsert_calls for doc in docs] == [
        "missing"
    ]
    assert set(store.upsert_calls[0][1][0]) == {
        "chunk_id",
        "file_id",
        "workspace_id",
        "workspace_slug",
        "content",
    }
    assert "exact_terms" not in store.upsert_calls[0][1][0]
    assert store.delete_calls == []
    assert second["status"].startswith("PASSED_NOOP")
    assert second["submitted_count"] == second["accepted_count"] == 0

    partial_store = FakeStore(
        FakeEsClient([es_doc("present", live.id, workspace.id)]),
        partial_upsert=True,
    )
    partial = repair_missing_chunks(
        db=db,
        store=partial_store,
        storage=storage,
        workspace_id=workspace.id,
    )
    assert partial["status"] == "FAILED_PARTIAL_BULK"
    assert partial["submitted_count"] == 1
    assert partial["accepted_count"] == 0
    assert partial["write_batches"][0]["error_samples"]
    assert partial["post_legacy_audit"]["missing_es_chunk_count"] == 1


def test_absent_index_requires_explicit_create_approval(repair_db):
    db, workspace, _live = repair_db
    storage = FakeStorage(
        {
            (workspace.slug, "chunks/present.md"): "present L2",
            (workspace.slug, "chunks/missing.md"): "missing L2",
        }
    )
    store = FakeStore(FakeEsClient(index_exists=False))

    blocked = repair_missing_chunks(
        db=db,
        store=store,
        storage=storage,
        workspace_id=workspace.id,
    )
    applied = repair_missing_chunks(
        db=db,
        store=store,
        storage=storage,
        workspace_id=workspace.id,
        allow_create_missing_index=True,
    )

    assert blocked["status"] == "BLOCKED_INDEX_CREATE_APPROVAL"
    assert blocked["submitted_count"] == 0
    assert store.ensure_calls == ["openrag_ws_legacy-repair_chunks"]
    assert applied["index_created"] is True
    assert applied["submitted_count"] == applied["accepted_count"] == 2
    assert applied["post_legacy_audit"]["missing_es_chunk_count"] == 0


def test_missing_plan_requires_exact_hash_and_blocks_data_drift(repair_db):
    db, workspace, live = repair_db
    storage = FakeStorage(
        {(workspace.slug, "chunks/missing.md"): "authoritative L2"}
    )
    client = FakeEsClient([es_doc("present", live.id, workspace.id)])
    store = FakeStore(client)
    plan = build_missing_repair_plan(
        db=db,
        store=store,
        storage=storage,
        workspace_id=workspace.id,
    )

    wrong_hash = repair_approved_missing_chunks(
        db=db,
        store=store,
        storage=storage,
        workspace_id=workspace.id,
        plan=plan,
        approval_sha256="wrong",
    )
    client.docs["missing"] = es_doc("missing", live.id, workspace.id)
    drifted = repair_approved_missing_chunks(
        db=db,
        store=store,
        storage=storage,
        workspace_id=workspace.id,
        plan=plan,
        approval_sha256=plan["manifest_sha256"],
    )

    assert plan["status"] == "AWAITING_MISSING_REPAIR_APPROVAL"
    assert plan["approval_payload"]["repair_candidate_chunk_ids"] == ["missing"]
    assert wrong_hash["status"] == "BLOCKED_MISSING_APPROVAL"
    assert drifted["status"] == "BLOCKED_MISSING_PLAN_DRIFT"
    assert store.upsert_calls == []


def test_approved_missing_plan_repairs_only_planned_chunks(repair_db):
    db, workspace, live = repair_db
    storage = FakeStorage(
        {(workspace.slug, "chunks/missing.md"): "authoritative L2"}
    )
    store = FakeStore(
        FakeEsClient([es_doc("present", live.id, workspace.id)])
    )
    plan = build_missing_repair_plan(
        db=db,
        store=store,
        storage=storage,
        workspace_id=workspace.id,
    )

    result = repair_approved_missing_chunks(
        db=db,
        store=store,
        storage=storage,
        workspace_id=workspace.id,
        plan=plan,
        approval_sha256=plan["manifest_sha256"],
    )

    assert result["status"] == "PASSED_MISSING_REPAIRED"
    assert result["submitted_count"] == result["accepted_count"] == 1
    assert [
        document["chunk_id"]
        for _index, documents in store.upsert_calls
        for document in documents
    ] == ["missing"]


def test_post_audit_failure_preserves_bulk_acceptance_result(repair_db):
    db, workspace, live = repair_db
    store = FakeStore(
        FakeEsClient([es_doc("present", live.id, workspace.id)]),
        fail_audit_after_upsert=True,
    )

    result = repair_missing_chunks(
        db=db,
        store=store,
        storage=FakeStorage(
            {(workspace.slug, "chunks/missing.md"): "authoritative L2"}
        ),
        workspace_id=workspace.id,
    )

    assert result["status"] == "FAILED_POST_AUDIT_ERROR"
    assert result["submitted_count"] == result["accepted_count"] == 1
    assert "audit unavailable" in result["post_legacy_audit_error"]


def test_orphan_delete_requires_exact_manifest_hash_and_clean_missing_audit(repair_db):
    db, workspace, live = repair_db
    client = FakeEsClient(
        [
            es_doc("present", live.id, workspace.id),
            es_doc("missing", live.id, workspace.id),
            es_doc("orphan", live.id, workspace.id),
        ]
    )
    store = FakeStore(client)
    manifest = build_orphan_delete_manifest(
        db=db,
        store=store,
        workspace_id=workspace.id,
    )

    wrong = delete_approved_orphans(
        db=db,
        store=store,
        workspace_id=workspace.id,
        manifest=manifest,
        approval_sha256="wrong",
    )
    deleted = delete_approved_orphans(
        db=db,
        store=store,
        workspace_id=workspace.id,
        manifest=manifest,
        approval_sha256=manifest["manifest_sha256"],
    )

    assert manifest["status"] == "AWAITING_SEPARATE_APPROVAL"
    assert manifest["orphan_chunk_ids"] == ["orphan"]
    assert manifest["orphan_reason_counts"] == {"DB_CHUNK_MISSING": 1}
    assert wrong["status"] == "BLOCKED_APPROVAL"
    assert deleted["status"] == "PASSED_ORPHANS_DELETED"
    assert store.delete_calls == [("openrag_ws_legacy-repair_chunks", ["orphan"])]
    assert deleted["post_legacy_audit"]["orphan_chunk_count"] == 0


def test_action_report_saves_preflight_writes_audit_and_delete_ids(tmp_path):
    result = {
        "status": "PASSED_MISSING_REPAIRED",
        "preflight": {"repair_candidate_chunk_ids": ["missing"]},
        "submitted_count": 1,
        "accepted_count": 1,
        "post_legacy_audit": {"missing_es_chunk_count": 0},
    }

    paths = save_action_report(tmp_path, "apply-missing", result)

    assert sorted(path.name for path in paths) == [
        "03-missing-apply.json",
        "04-post-missing-audit.json",
    ]
    assert json.loads((tmp_path / "03-missing-apply.json").read_text())["accepted_count"] == 1
