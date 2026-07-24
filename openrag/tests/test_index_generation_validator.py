from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from openrag.indexing.milvus_schema import chunk_schema_spec, layer_schema_spec
from openrag.indexing.validation_report import (
    ValidationCheck,
    assemble_validation_report,
)
from openrag.indexing.validator import IndexGenerationValidator
from openrag.models import Base
from openrag.models.file import File, ProcessingStatus
from openrag.models.index_generation import (
    IndexGeneration,
    IndexGenerationFile,
    IndexGenerationFileState,
    IndexGenerationState,
)
from openrag.models.user import User
from openrag.models.workspace import Workspace
from openrag.services.index_generation_service import (
    GenerationStateTransitionError,
    IndexGenerationService,
)


class FakeValidationBackend:
    def __init__(self, generation, file_id):
        self.descriptions = {
            generation.chunk_collection_name: self._description(
                generation, "chunks", chunk_schema_spec(3, 2).to_dict()
            ),
            generation.layer_collection_name: self._description(
                generation, "layers", layer_schema_spec(3, 2).to_dict()
            ),
        }
        self.counts = {
            generation.chunk_collection_name: 1,
            generation.layer_collection_name: 1,
        }
        self.ids = {
            generation.chunk_collection_name: {file_id},
            generation.layer_collection_name: {file_id},
        }
        self.vectors = {
            generation.chunk_collection_name: [[1.0, 0.0, 0.0]],
            generation.layer_collection_name: [[0.0, 1.0, 0.0]],
        }
        self.indexes = {
            name: {"has_vector_index": True, "total_rows": 1, "indexed_rows": 1}
            for name in self.counts
        }
        self.loads = {name: {"state": "Loaded"} for name in self.counts}
        self.searches = {name: True for name in self.counts}

    @staticmethod
    def _description(generation, role, spec):
        return {
            "fields": spec["fields"],
            "indexes": [{"field_name": "embedding", **spec["index_params"]}],
            "metadata": {
                "generation_id": generation.id,
                "embedding_fingerprint": generation.embedding_fingerprint,
                "schema_version": "2",
                "collection_role": role,
            },
        }

    def describe_collection(self, name):
        return self.descriptions[name]

    def entity_count(self, name):
        return self.counts[name]

    def file_ids(self, name):
        return self.ids[name]

    def sample_vectors(self, name, limit):
        return self.vectors[name][:limit]

    def index_state(self, name):
        return self.indexes[name]

    def load_state(self, name):
        return self.loads[name]

    def smoke_search(self, name, vector):
        return self.searches[name]


@pytest.fixture()
def validation_env():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    user = User(
        username="validator",
        email="validator@example.com",
        password_hash="hash",
        full_name="Validator",
        is_active=True,
    )
    db.add(user)
    db.flush()
    workspace = Workspace(name="Validation", slug="validation", owner_id=user.id)
    db.add(workspace)
    db.flush()
    file = File(
        uri="docs/valid.txt",
        name="valid.txt",
        owner_id=user.id,
        workspace_id=workspace.id,
        is_directory=False,
        size=1,
        processing_status=ProcessingStatus.completed,
    )
    db.add(file)
    db.flush()
    generation_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    manifest = {
        "generation_id": generation_id,
        "embedding_fingerprint": "a" * 64,
        "embedding_dimension": 3,
        "schema_version": 2,
        "chunk_collection_name": "chunks_validation",
        "layer_collection_name": "layers_validation",
    }
    generation = IndexGeneration(
        id=generation_id,
        scope="global",
        state=IndexGenerationState.RECONCILING.value,
        embedding_provider="provider",
        embedding_model="model",
        embedding_revision="revision",
        embedding_dimension=3,
        embedding_fingerprint="a" * 64,
        embedding_config_ref="embedding/default",
        vector_normalization="none",
        distance_metric="COSINE",
        schema_version=2,
        chunk_policy_revision="chunk-v2",
        hierarchy_policy_revision="hierarchy-v2",
        chunk_collection_name="chunks_validation",
        layer_collection_name="layers_validation",
        manifest=manifest,
        build_lag_files=0,
    )
    db.add(generation)
    db.flush()
    row = IndexGenerationFile(
        generation_id=generation.id,
        file_id=file.id,
        workspace_id=workspace.id,
        state=IndexGenerationFileState.SUCCESS.value,
        source_content_hash="b" * 64,
        source_updated_at=datetime.now(timezone.utc),
        expected_chunk_count=1,
        written_chunk_count=1,
        expected_layer_count=1,
        written_layer_count=1,
    )
    db.add(row)
    db.commit()
    backend = FakeValidationBackend(generation, file.id)
    try:
        yield db, generation, row, file, backend, user, workspace
    finally:
        db.close()
        Base.metadata.drop_all(engine)


def _report(env):
    db, generation, _, _, backend, _, _ = env
    return IndexGenerationValidator(
        db,
        generation,
        backend,
        embedding_probe=lambda: [1.0, 0.0, 0.0],
    ).build_report()


def _codes(report):
    return {
        check["error_code"]
        for check in report["checks"]
        if check["status"] == "failed"
    }


def test_valid_candidate_report_allows_ready_transition(validation_env):
    db, generation, _, _, _, _, _ = validation_env
    service = IndexGenerationService(db)
    service.start_validation(generation.id)
    report = _report(validation_env)
    service.record_validation_report(generation.id, report)
    ready = service.mark_ready_if_valid(generation.id)

    assert report["passed"] is True
    assert ready.state == IndexGenerationState.READY.value
    assert ready.validation_started_at is not None
    assert ready.validation_completed_at is not None
    assert ready.validation_error_code is None


def test_missing_file_status_row_fails(validation_env):
    db, _, row, _, _, _, _ = validation_env
    db.delete(row)
    db.commit()
    report = _report(validation_env)
    assert "VALIDATION_FILE_COVERAGE_INCOMPLETE" in _codes(report)


def test_written_chunk_count_mismatch_fails(validation_env):
    db, _, row, _, _, _, _ = validation_env
    row.written_chunk_count = 0
    db.commit()
    assert "VALIDATION_FILE_COUNT_MISMATCH" in _codes(_report(validation_env))


def test_layer_count_mismatch_fails(validation_env):
    db, _, row, _, _, _, _ = validation_env
    row.written_layer_count = 0
    db.commit()
    assert "VALIDATION_FILE_COUNT_MISMATCH" in _codes(_report(validation_env))


def test_failed_file_state_fails(validation_env):
    db, _, row, _, _, _, _ = validation_env
    row.state = IndexGenerationFileState.FAILED.value
    db.commit()
    assert "VALIDATION_FILE_COVERAGE_INCOMPLETE" in _codes(_report(validation_env))


def test_collection_schema_mismatch_fails(validation_env):
    _, generation, _, _, backend, _, _ = validation_env
    backend.descriptions[generation.chunk_collection_name]["fields"] = []
    assert "VALIDATION_CHUNK_SCHEMA_MISMATCH" in _codes(_report(validation_env))


def test_fingerprint_metadata_mismatch_fails(validation_env):
    _, generation, _, _, backend, _, _ = validation_env
    backend.descriptions[generation.chunk_collection_name]["metadata"][
        "embedding_fingerprint"
    ] = "wrong"
    assert "VALIDATION_CHUNK_SCHEMA_MISMATCH" in _codes(_report(validation_env))


def test_deleted_file_still_in_collection_fails(validation_env):
    db, generation, _, file, backend, user, workspace = validation_env
    deleted = File(
        uri="docs/deleted.txt",
        name="deleted.txt",
        owner_id=user.id,
        workspace_id=workspace.id,
        is_directory=False,
        size=1,
        processing_status=ProcessingStatus.completed,
        deleted_at=datetime.now(timezone.utc),
    )
    db.add(deleted)
    db.commit()
    for ids in backend.ids.values():
        ids.clear()
        ids.add(deleted.id)
    report = _report(validation_env)
    assert "VALIDATION_DELETED_FILE_PRESENT" in _codes(report)
    assert report["deleted_file_ids_found"] == [deleted.id]


def test_unknown_file_id_fails_as_orphan(validation_env):
    _, _, _, _, backend, _, _ = validation_env
    for ids in backend.ids.values():
        ids.clear()
        ids.add(999999)
    report = _report(validation_env)
    assert "VALIDATION_ORPHAN_ENTITY" in _codes(report)
    assert report["orphan_file_ids"] == [999999]


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("index", "VALIDATION_INDEX_INCOMPLETE"),
        ("load", "VALIDATION_COLLECTION_NOT_LOADED"),
        ("search", "VALIDATION_SMOKE_SEARCH_FAILED"),
    ],
)
def test_milvus_serving_state_failures_are_hard_gates(
    validation_env, mutation, expected_code
):
    _, generation, _, _, backend, _, _ = validation_env
    name = generation.chunk_collection_name
    if mutation == "index":
        backend.indexes[name]["indexed_rows"] = 0
    elif mutation == "load":
        backend.loads[name]["state"] = "NotLoad"
    else:
        backend.searches[name] = False
    assert expected_code in _codes(_report(validation_env))


def test_failed_report_cannot_be_marked_ready(validation_env):
    db, generation, _, _, backend, _, _ = validation_env
    backend.searches[generation.chunk_collection_name] = False
    service = IndexGenerationService(db)
    service.start_validation(generation.id)
    report = _report(validation_env)
    item = service.record_validation_report(generation.id, report)

    assert item.validation_error_code == "VALIDATION_SMOKE_SEARCH_FAILED"
    with pytest.raises(GenerationStateTransitionError):
        service.mark_ready_if_valid(generation.id)


def test_warning_is_not_reported_as_a_passed_check():
    report = assemble_validation_report(
        checks=[ValidationCheck(name="sample_coverage", status="warning")],
        expected_counts={},
        observed_counts={},
        sampled_vector_count=0,
        orphan_file_ids=[],
        deleted_file_ids_found=[],
        schema_snapshot={},
        index_state={},
        load_state={},
        started_at="start",
    )
    assert report["passed"] is True
    assert report["warnings"] == ["sample_coverage"]
    assert report["checks"][0]["status"] == "warning"
