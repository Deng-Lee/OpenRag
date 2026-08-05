from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from openrag.models import Base, DocumentChunk, File, User, Workspace
from openrag.search.es_chunk_contract import (
    SCHEMA_VERSION,
    build_chunk_mapping,
    compute_mapping_hash,
)
from scripts.audit_es_chunk_consistency import audit_workspace


LEGACY_MAPPING = {
    "properties": {
        "chunk_id": {"type": "keyword"},
        "file_id": {"type": "long"},
        "workspace_id": {"type": "long"},
        "workspace_slug": {"type": "keyword"},
        "content": {"type": "text"},
    }
}


class FakeEsClient:
    def __init__(self, docs, mapping=None, physical_names=None):
        self.docs = {str(doc["_id"]): doc for doc in docs}
        self._remaining = []
        self.write_calls = []
        self.indices = self.Indices(
            mapping or LEGACY_MAPPING,
            physical_names=physical_names,
        )

    class Indices:
        def __init__(self, mapping, physical_names=None):
            self.mapping = mapping
            self.physical_names = physical_names

        @staticmethod
        def exists(*, index):
            return True

        @staticmethod
        def exists_alias(*, name):
            return False

        def get_mapping(self, *, index):
            return {
                physical_name: {"mappings": self.mapping}
                for physical_name in (self.physical_names or [index])
            }

        @staticmethod
        def get_settings(*, index, flat_settings):
            return {index: {"settings": {}}}

        @staticmethod
        def analyze(*, index, field, text):
            return {"tokens": [{"token": "openrag"}]}

    def search(self, *, index, size, scroll, query, source, sort):
        docs = list(self.docs.values())
        self._remaining = docs[size:]
        return {"_scroll_id": "audit-scroll", "hits": {"hits": docs[:size]}}

    def scroll(self, *, scroll_id, scroll):
        hits = self._remaining
        self._remaining = []
        return {"_scroll_id": scroll_id, "hits": {"hits": hits}}

    def clear_scroll(self, *, scroll_id):
        return {"succeeded": True}

    def mget(self, *, index, ids, source):
        return {"docs": [{"_id": cid, "found": cid in self.docs} for cid in ids]}


class MissingIndexEsClient:
    class Indices:
        @staticmethod
        def exists(*, index):
            return False

        @staticmethod
        def exists_alias(*, name):
            return False

    indices = Indices()

    def search(self, **kwargs):
        raise AssertionError("Missing index must not be searched")

    def mget(self, **kwargs):
        raise AssertionError("Missing index must not be queried")


@pytest.fixture
def audit_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    user = User(
        username="audit-user",
        email="audit@example.com",
        password_hash="x",
        full_name="Audit User",
    )
    db.add(user)
    db.commit()
    workspace = Workspace(name="Audit", slug="audit", owner_id=user.id)
    db.add(workspace)
    db.commit()
    live = File(
        uri="/live.txt",
        name="live.txt",
        owner_id=user.id,
        workspace_id=workspace.id,
        size=1,
    )
    dead = File(
        uri="/dead.txt",
        name="dead.txt",
        owner_id=user.id,
        workspace_id=workspace.id,
        size=1,
        deleted_at=datetime.now(timezone.utc),
    )
    db.add_all([live, dead])
    db.commit()
    chunks = [
        DocumentChunk(
            id=1,
            file_id=live.id,
            workspace_id=workspace.id,
            chunk_id="live-present",
            chunk_index=0,
            object_key="chunks/live-present.md",
        ),
        DocumentChunk(
            id=2,
            file_id=live.id,
            workspace_id=workspace.id,
            chunk_id="live-missing",
            chunk_index=1,
            object_key="chunks/live-missing.md",
        ),
        DocumentChunk(
            id=3,
            file_id=dead.id,
            workspace_id=workspace.id,
            chunk_id="dead-present",
            chunk_index=0,
            object_key="chunks/dead-present.md",
        ),
    ]
    db.add_all(chunks)
    db.commit()
    try:
        yield db, workspace, live, dead
    finally:
        db.close()
        Base.metadata.drop_all(engine)


def _es_doc(chunk_id, file_id, workspace_id):
    return {
        "_id": chunk_id,
        "_source": {
            "chunk_id": chunk_id,
            "file_id": file_id,
            "workspace_id": workspace_id,
            "content": "must never appear in report",
        },
    }


def _v2_es_doc(chunk_id, file_id, workspace_id):
    source = _es_doc(chunk_id, file_id, workspace_id)["_source"]
    source.update(
        {
            "workspace_slug": "audit",
            "doc_type_kwd": "text",
            "content_with_weight": source["content"],
            "mom_with_weight": "",
            "exact_terms": [],
            "schema_version": SCHEMA_VERSION,
            "mapping_hash": compute_mapping_hash(),
        }
    )
    return {"_id": chunk_id, "_source": source}


def test_audit_reports_orphan_missing_mismatch_and_soft_deleted_without_writes(audit_db):
    db, workspace, live, dead = audit_db
    es = FakeEsClient(
        [
            _es_doc("live-present", live.id, workspace.id),
            _es_doc("orphan", live.id, workspace.id),
            _es_doc("dead-present", dead.id, workspace.id),
            _es_doc("live-present", dead.id, workspace.id + 99),
        ]
    )

    report = audit_workspace(
        db=db,
        es_client=es,
        index_name="audit-chunks",
        workspace_id=workspace.id,
        batch_size=2,
    )

    assert report["orphan_chunk_count"] == 1
    assert report["missing_es_chunk_count"] == 1
    assert report["file_id_mismatch_count"] == 1
    assert report["deleted_file_chunk_count"] == 1
    assert report["orphan_chunk_ids"] == ["orphan"]
    assert report["missing_es_chunk_ids"] == ["live-missing"]
    assert "must never appear in report" not in str(report).lower()
    assert "must never appear" not in str(report)
    assert es.write_calls == []


def test_audit_clean_index_has_zero_anomalies(audit_db):
    db, workspace, live, _dead = audit_db
    db.query(DocumentChunk).filter(DocumentChunk.chunk_id == "live-missing").delete()
    db.commit()
    es = FakeEsClient([_es_doc("live-present", live.id, workspace.id)])

    report = audit_workspace(
        db=db,
        es_client=es,
        index_name="audit-chunks",
        workspace_id=workspace.id,
        batch_size=1,
    )

    assert report["anomaly_count"] == 0
    assert report["es_doc_count"] == report["db_active_chunk_count"] == 1


def test_audit_missing_index_reports_all_active_db_chunks_as_missing(audit_db):
    db, workspace, _live, _dead = audit_db

    report = audit_workspace(
        db=db,
        es_client=MissingIndexEsClient(),
        index_name="missing-chunks",
        workspace_id=workspace.id,
        batch_size=1,
    )

    assert report["index_exists"] is False
    assert report["db_active_chunk_count"] == 2
    assert report["missing_es_chunk_count"] == 2
    assert report["anomaly_count"] == 2


def test_v2_audit_covers_mapping_version_required_and_forbidden_fields(audit_db):
    db, workspace, live, _dead = audit_db
    db.query(DocumentChunk).filter(DocumentChunk.chunk_id == "live-missing").delete()
    db.commit()
    document = _v2_es_doc("live-present", live.id, workspace.id)
    document["_source"]["schema_version"] = "wrong"
    document["_source"]["content_ltks"] = ["forbidden"]
    document["_source"].pop("doc_type_kwd")
    bad_mapping = build_chunk_mapping()
    bad_mapping["_meta"]["mapping_hash"] = "wrong"

    report = audit_workspace(
        db=db,
        es_client=FakeEsClient([document], mapping=bad_mapping),
        index_name="audit-chunks-v2",
        workspace_id=workspace.id,
        batch_size=1,
        index_version="v2",
    )

    assert report["mapping_contract_mismatch_count"] == 1
    assert report["schema_version_mismatch_count"] == 1
    assert report["missing_required_field_count"] == 1
    assert report["forbidden_field_count"] == 1
    assert report["anomaly_count"] == 4
    assert "must never appear in report" not in str(report).lower()


def test_v2_audit_clean_contract_has_full_field_coverage(audit_db):
    db, workspace, live, _dead = audit_db
    db.query(DocumentChunk).filter(DocumentChunk.chunk_id == "live-missing").delete()
    db.commit()

    report = audit_workspace(
        db=db,
        es_client=FakeEsClient(
            [_v2_es_doc("live-present", live.id, workspace.id)],
            mapping=build_chunk_mapping(),
        ),
        index_name="audit-chunks-v2",
        workspace_id=workspace.id,
        batch_size=1,
        index_version="v2",
    )

    assert report["anomaly_count"] == 0
    assert report["required_field_coverage"] == 1.0


def test_alias_audit_requires_exactly_one_physical_target(audit_db):
    db, workspace, live, _dead = audit_db
    db.query(DocumentChunk).filter(DocumentChunk.chunk_id == "live-missing").delete()
    db.commit()

    report = audit_workspace(
        db=db,
        es_client=FakeEsClient(
            [_v2_es_doc("live-present", live.id, workspace.id)],
            mapping=build_chunk_mapping(),
            physical_names=["chunks-v2-a", "chunks-v2-b"],
        ),
        index_name="audit-chunks-read",
        workspace_id=workspace.id,
        batch_size=1,
        index_version="alias",
    )

    assert report["alias_target_mismatch_count"] == 1
    assert report["anomaly_count"] == 1
