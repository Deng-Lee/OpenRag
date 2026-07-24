from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from openrag.indexing.reconciliation_service import ReconciliationService
from openrag.indexing.reindex_service import GenerationReindexService
from openrag.indexing.source_reader import GenerationSourceSnapshot
from openrag.models import Base
from openrag.models.file import File, ProcessingStatus
from openrag.models.index_generation import (
    IndexGenerationFile,
    IndexGenerationFileState,
    IndexGenerationState,
)
from openrag.models.task import TaskType
from openrag.models.user import User
from openrag.models.workspace import Workspace
from openrag.services.index_generation_service import IndexGenerationService


def _generation_values():
    return {
        "scope": "global",
        "embedding_provider": "provider",
        "embedding_model": "model",
        "embedding_revision": "revision",
        "embedding_dimension": 3,
        "embedding_fingerprint": "c" * 64,
        "embedding_config_ref": "embedding/candidate",
        "vector_normalization": "none",
        "distance_metric": "COSINE",
        "schema_version": 2,
        "chunk_policy_revision": "chunk-v2",
        "hierarchy_policy_revision": "hierarchy-v2",
        "chunk_collection_name": "chunks_reconcile_candidate",
        "layer_collection_name": "layers_reconcile_candidate",
        "manifest": {},
    }


class MutableReader:
    def __init__(self, db, revisions):
        self.db = db
        self.revisions = revisions

    def list_active_source_files(self):
        return (
            self.db.query(File)
            .filter(
                File.is_directory.is_(False),
                File.deleted_at.is_(None),
                File.processing_status == ProcessingStatus.completed,
            )
            .order_by(File.id)
            .all()
        )

    def get_source_revision(self, file_id):
        file = self.db.get(File, file_id)
        revision = self.revisions[file_id]
        return GenerationSourceSnapshot(
            file_id=file.id,
            workspace_id=file.workspace_id,
            source_updated_at=revision["updated_at"],
            source_content_hash=revision["hash"],
            chunks=tuple(SimpleNamespace() for _ in range(revision["chunks"])),
            layers=tuple(("l0", "summary") for _ in range(revision["layers"])),
        )


def _file(db, user_id, workspace_id, name):
    item = File(
        uri=f"docs/{name}",
        name=name,
        owner_id=user_id,
        workspace_id=workspace_id,
        is_directory=False,
        size=1,
        processing_status=ProcessingStatus.completed,
    )
    db.add(item)
    db.flush()
    return item


def test_reconciliation_detects_new_updated_deleted_and_lag_can_reopen():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    user = User(
        username="reconcile-user",
        email="reconcile@example.com",
        password_hash="hash",
        full_name="Reconcile User",
        is_active=True,
    )
    db.add(user)
    db.flush()
    workspace = Workspace(name="Reconcile", slug="reconcile", owner_id=user.id)
    db.add(workspace)
    db.flush()
    original = _file(db, user.id, workspace.id, "original.txt")
    now = datetime.now(timezone.utc)
    revisions = {
        original.id: {"hash": "a" * 64, "updated_at": now, "chunks": 2, "layers": 1}
    }
    reader = MutableReader(db, revisions)
    generation = IndexGenerationService(db).create_generation(**_generation_values())
    generation.state = IndexGenerationState.BUILDING.value
    db.commit()

    reindex = GenerationReindexService(db, reader)
    assert reindex.initialize_generation_files(generation.id) == 1
    original_row = db.get(IndexGenerationFile, (generation.id, original.id))
    original_row.state = IndexGenerationFileState.SUCCESS.value
    original_row.written_chunk_count = 2
    original_row.written_layer_count = 1
    db.commit()

    service = ReconciliationService(db, reader)
    assert service.calculate_build_lag(generation.id) == 0
    first_reconciled_at = generation.last_reconciled_at
    assert first_reconciled_at is not None

    added = _file(db, user.id, workspace.id, "added.txt")
    revisions[added.id] = {
        "hash": "b" * 64,
        "updated_at": now,
        "chunks": 1,
        "layers": 1,
    }
    db.commit()
    assert [row.file_id for row in service.scan_changed_files(generation.id)] == [
        added.id
    ]
    assert service.calculate_build_lag(generation.id) == 1
    tasks = service.enqueue_reconciliation_tasks(generation.id, user_id=user.id)
    assert len(tasks) == 1
    assert tasks[0].task_type == TaskType.RECONCILE_GENERATION_FILE.value
    assert tasks[0].file_id == added.id

    revisions[original.id] = {
        "hash": "d" * 64,
        "updated_at": datetime.now(timezone.utc),
        "chunks": 3,
        "layers": 1,
    }
    changed = service.scan_changed_files(generation.id)
    assert [row.file_id for row in changed] == [original.id]
    original_row = db.get(IndexGenerationFile, (generation.id, original.id))
    assert original_row.state == IndexGenerationFileState.STALE.value
    assert original_row.source_content_hash == "d" * 64

    added.deleted_at = datetime.now(timezone.utc)
    db.commit()
    deleted = service.scan_deleted_files(generation.id)
    assert [row.file_id for row in deleted] == [added.id]
    purge_tasks = service.enqueue_deleted_files(deleted, user_id=user.id)
    assert len(purge_tasks) == 1
    assert purge_tasks[0].task_type == TaskType.PURGE_FILE_FROM_GENERATIONS.value
    assert service.calculate_build_lag(generation.id) == 1

    original_row.state = IndexGenerationFileState.SUCCESS.value
    db.commit()
    assert service.calculate_build_lag(generation.id) == 0
    assert generation.last_reconciled_at >= first_reconciled_at

    revisions[original.id]["hash"] = "e" * 64
    service.scan_changed_files(generation.id)
    assert service.calculate_build_lag(generation.id) == 1

    generation.state = IndexGenerationState.BUILDING.value
    db.commit()
    assert service.reconcile_until_stable(
        generation.id, user_id=user.id, max_passes=1
    ) == 1
    assert generation.state == IndexGenerationState.RECONCILING.value

    db.close()
    Base.metadata.drop_all(engine)
