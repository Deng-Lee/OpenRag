"""Read-only integrity and serving-readiness validation for one candidate."""

import math
from typing import Any, Callable, Protocol
from uuid import uuid4

from pymilvus import Collection, connections, utility
from sqlalchemy.orm import Session

from openrag.indexing.milvus_schema import (
    chunk_schema_spec,
    describe_collection,
    layer_schema_spec,
)
from openrag.indexing.validation_report import (
    ValidationCheck,
    assemble_validation_report,
    utc_iso,
)
from openrag.models.file import File, ProcessingStatus
from openrag.models.index_generation import (
    IndexGeneration,
    IndexGenerationFile,
    IndexGenerationFileState,
)


class ValidationBackend(Protocol):
    def describe_collection(self, name: str) -> dict[str, Any]: ...
    def entity_count(self, name: str) -> int: ...
    def file_ids(self, name: str) -> set[int]: ...
    def sample_vectors(self, name: str, limit: int) -> list[list[float]]: ...
    def index_state(self, name: str) -> dict[str, Any]: ...
    def load_state(self, name: str) -> dict[str, Any]: ...
    def smoke_search(self, name: str, vector: list[float]) -> bool: ...


class PyMilvusValidationBackend:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        user: str | None = None,
        password: str | None = None,
        secure: bool = False,
    ):
        self.alias = f"openrag_validator_{uuid4().hex}"
        self.connection_kwargs = {
            "alias": self.alias,
            "host": host,
            "port": port,
            "secure": secure,
        }
        if user:
            self.connection_kwargs.update(user=user, password=password or "")
        self.connected = False

    def _ensure_connected(self) -> None:
        if not self.connected:
            connections.connect(**self.connection_kwargs)
            self.connected = True

    def _collection(self, name: str) -> Collection:
        self._ensure_connected()
        if not utility.has_collection(name, using=self.alias):
            raise RuntimeError("VALIDATION_COLLECTION_MISSING")
        return Collection(name, using=self.alias)

    def describe_collection(self, name: str) -> dict[str, Any]:
        return describe_collection(self._collection(name))

    def entity_count(self, name: str) -> int:
        return int(self._collection(name).num_entities)

    def _query_rows(self, name: str, output_fields: list[str], limit=None):
        iterator = self._collection(name).query_iterator(
            batch_size=min(limit or 1000, 1000),
            expr="file_id >= 0",
            output_fields=output_fields,
        )
        rows = []
        try:
            while limit is None or len(rows) < limit:
                batch = iterator.next()
                if not batch:
                    break
                rows.extend(batch)
        finally:
            iterator.close()
        return rows if limit is None else rows[:limit]

    def file_ids(self, name: str) -> set[int]:
        return {int(row["file_id"]) for row in self._query_rows(name, ["file_id"])}

    def sample_vectors(self, name: str, limit: int) -> list[list[float]]:
        return [
            list(row["embedding"])
            for row in self._query_rows(name, ["embedding"], limit=limit)
        ]

    def index_state(self, name: str) -> dict[str, Any]:
        collection = self._collection(name)
        progress = utility.index_building_progress(name, using=self.alias)
        return {
            "has_vector_index": any(
                index.field_name == "embedding" for index in collection.indexes
            ),
            "total_rows": int(progress.get("total_rows") or 0),
            "indexed_rows": int(progress.get("indexed_rows") or 0),
        }

    def load_state(self, name: str) -> dict[str, Any]:
        self._collection(name)
        state = utility.load_state(name, using=self.alias)
        return {"state": getattr(state, "name", str(state))}

    def smoke_search(self, name: str, vector: list[float]) -> bool:
        result = self._collection(name).search(
            data=[vector],
            anns_field="embedding",
            param={"metric_type": "COSINE", "params": {"nprobe": 10}},
            limit=1,
            output_fields=["file_id"],
        )
        return len(result) == 1


class IndexGenerationValidator:
    def __init__(
        self,
        db: Session,
        generation: IndexGeneration,
        backend: ValidationBackend,
        embedding_probe: Callable[[], list[float]],
        *,
        sample_limit: int = 20,
    ):
        self.db = db
        self.generation = generation
        self.backend = backend
        self.embedding_probe = embedding_probe
        self.sample_limit = sample_limit
        self.checks: list[ValidationCheck] = []
        self.expected_counts = {"files": 0, "chunks": 0, "layers": 0}
        self.observed_counts = {"files": 0, "chunks": 0, "layers": 0}
        self.sampled_vector_count = 0
        self.orphan_file_ids: list[int] = []
        self.deleted_file_ids_found: list[int] = []
        self.schema_snapshot: dict[str, Any] = {}
        self.index_states: dict[str, Any] = {}
        self.load_states: dict[str, Any] = {}
        self.probe_vector: list[float] | None = None

    def _check(self, name: str, code: str, callback) -> None:
        try:
            passed, details = callback()
            self.checks.append(
                ValidationCheck(
                    name=name,
                    status="passed" if passed else "failed",
                    error_code=None if passed else code,
                    details=details,
                )
            )
        except Exception as exc:
            self.checks.append(
                ValidationCheck(
                    name=name,
                    status="failed",
                    error_code=code,
                    details={"reason": type(exc).__name__},
                )
            )

    def validate_manifest(self):
        required = {
            "generation_id": self.generation.id,
            "embedding_fingerprint": self.generation.embedding_fingerprint,
            "embedding_dimension": self.generation.embedding_dimension,
            "schema_version": self.generation.schema_version,
            "chunk_collection_name": self.generation.chunk_collection_name,
            "layer_collection_name": self.generation.layer_collection_name,
        }
        mismatches = {
            key: {"expected": value, "observed": self.generation.manifest.get(key)}
            for key, value in required.items()
            if self.generation.manifest.get(key) != value
        }
        return not mismatches, {"mismatches": mismatches}

    def validate_embedding_probe(self):
        vector = self.embedding_probe()
        valid = (
            isinstance(vector, list)
            and len(vector) == self.generation.embedding_dimension
            and all(isinstance(value, (int, float)) and math.isfinite(value) for value in vector)
            and any(float(value) != 0.0 for value in vector)
        )
        if valid:
            self.probe_vector = [float(value) for value in vector]
        return valid, {"observed_dimension": len(vector) if isinstance(vector, list) else None}

    def _validate_collection_schema(self, role: str):
        name = (
            self.generation.chunk_collection_name
            if role == "chunks"
            else self.generation.layer_collection_name
        )
        expected = (
            chunk_schema_spec(
                self.generation.embedding_dimension, self.generation.schema_version
            )
            if role == "chunks"
            else layer_schema_spec(
                self.generation.embedding_dimension, self.generation.schema_version
            )
        ).to_dict()
        observed = self.backend.describe_collection(name)
        self.schema_snapshot[role] = observed
        metadata = observed.get("metadata", {})
        vector_indexes = [
            item
            for item in observed.get("indexes", [])
            if item.get("field_name") == "embedding"
        ]
        schema_matches = observed.get("fields") == expected["fields"]
        index_matches = any(
            str(item.get("metric_type", "")).upper()
            == expected["index_params"]["metric_type"]
            and item.get("index_type") == expected["index_params"]["index_type"]
            for item in vector_indexes
        )
        metadata_matches = (
            str(metadata.get("generation_id", "")) == self.generation.id
            and str(metadata.get("embedding_fingerprint", ""))
            == self.generation.embedding_fingerprint
            and str(metadata.get("schema_version", ""))
            == str(self.generation.schema_version)
            and str(metadata.get("collection_role", "")) == role
        )
        return schema_matches and index_matches and metadata_matches, {
            "schema_matches": schema_matches,
            "index_matches": index_matches,
            "metadata_matches": metadata_matches,
        }

    def validate_chunk_collection_schema(self):
        return self._validate_collection_schema("chunks")

    def validate_layer_collection_schema(self):
        return self._validate_collection_schema("layers")

    def validate_index_build_state(self):
        for role, name in self._collections().items():
            self.index_states[role] = self.backend.index_state(name)
        valid = all(
            item.get("has_vector_index")
            and int(item.get("indexed_rows") or 0) >= int(item.get("total_rows") or 0)
            for item in self.index_states.values()
        )
        return valid, self.index_states

    def validate_collection_load_state(self):
        for role, name in self._collections().items():
            self.load_states[role] = self.backend.load_state(name)
        valid = all(
            str(item.get("state", "")).lower() == "loaded"
            for item in self.load_states.values()
        )
        return valid, self.load_states

    def _generation_rows(self):
        return (
            self.db.query(IndexGenerationFile)
            .filter(IndexGenerationFile.generation_id == self.generation.id)
            .all()
        )

    def validate_file_coverage(self):
        active_ids = {
            row[0]
            for row in self.db.query(File.id)
            .filter(
                File.is_directory.is_(False),
                File.deleted_at.is_(None),
                File.processing_status == ProcessingStatus.completed,
            )
            .all()
        }
        rows = self._generation_rows()
        row_by_file = {row.file_id: row for row in rows}
        missing = sorted(active_ids - set(row_by_file))
        incomplete = sorted(
            file_id
            for file_id in active_ids & set(row_by_file)
            if row_by_file[file_id].state != IndexGenerationFileState.SUCCESS.value
        )
        self.expected_counts["files"] = len(active_ids)
        self.observed_counts["files"] = sum(
            row.state == IndexGenerationFileState.SUCCESS.value for row in rows
        )
        return not missing and not incomplete, {
            "missing_file_ids": missing,
            "incomplete_file_ids": incomplete,
        }

    def validate_file_counts(self):
        rows = [
            row
            for row in self._generation_rows()
            if row.state == IndexGenerationFileState.SUCCESS.value
        ]
        chunk_mismatch = sorted(
            row.file_id
            for row in rows
            if row.expected_chunk_count != row.written_chunk_count
        )
        layer_mismatch = sorted(
            row.file_id
            for row in rows
            if row.expected_layer_count != row.written_layer_count
        )
        self.expected_counts.update(
            chunks=sum(row.expected_chunk_count for row in rows),
            layers=sum(row.expected_layer_count for row in rows),
        )
        self.observed_counts.update(
            chunks=self.backend.entity_count(self.generation.chunk_collection_name),
            layers=self.backend.entity_count(self.generation.layer_collection_name),
        )
        collection_mismatch = {
            role: {
                "expected": self.expected_counts[role],
                "observed": self.observed_counts[role],
            }
            for role in ("chunks", "layers")
            if self.expected_counts[role] != self.observed_counts[role]
        }
        return not chunk_mismatch and not layer_mismatch and not collection_mismatch, {
            "chunk_mismatch_file_ids": chunk_mismatch,
            "layer_mismatch_file_ids": layer_mismatch,
            "collection_mismatch": collection_mismatch,
        }

    def validate_deleted_file_absence(self):
        deleted_ids = {
            row[0]
            for row in self.db.query(File.id).filter(File.deleted_at.is_not(None)).all()
        }
        observed = self._all_observed_file_ids()
        self.deleted_file_ids_found = sorted(deleted_ids & observed)
        return not self.deleted_file_ids_found, {
            "deleted_file_ids_found": self.deleted_file_ids_found
        }

    def validate_orphan_entities(self):
        known_ids = {row[0] for row in self.db.query(File.id).all()}
        self.orphan_file_ids = sorted(self._all_observed_file_ids() - known_ids)
        return not self.orphan_file_ids, {"orphan_file_ids": self.orphan_file_ids}

    def validate_vector_samples(self):
        vectors = []
        invalid = 0
        for name in self._collections().values():
            for vector in self.backend.sample_vectors(name, self.sample_limit):
                vectors.append(vector)
                if (
                    len(vector) != self.generation.embedding_dimension
                    or not all(isinstance(value, (int, float)) and math.isfinite(value) for value in vector)
                    or not any(float(value) != 0.0 for value in vector)
                ):
                    invalid += 1
        self.sampled_vector_count = len(vectors)
        expected_entities = self.observed_counts["chunks"] + self.observed_counts["layers"]
        valid = invalid == 0 and (expected_entities == 0 or bool(vectors))
        return valid, {"sampled": len(vectors), "invalid": invalid}

    def run_smoke_search(self):
        if self.probe_vector is None:
            return False, {"reason": "embedding_probe_unavailable"}
        results = {
            role: self.backend.smoke_search(name, self.probe_vector)
            for role, name in self._collections().items()
        }
        return all(results.values()), results

    def _collections(self):
        return {
            "chunks": self.generation.chunk_collection_name,
            "layers": self.generation.layer_collection_name,
        }

    def _all_observed_file_ids(self):
        observed = set()
        for name in self._collections().values():
            observed.update(self.backend.file_ids(name))
        return observed

    def build_report(self) -> dict[str, Any]:
        started_at = utc_iso()
        validations = [
            ("manifest", "VALIDATION_MANIFEST_MISMATCH", self.validate_manifest),
            ("embedding_probe", "VALIDATION_EMBEDDING_PROBE_FAILED", self.validate_embedding_probe),
            ("chunk_schema", "VALIDATION_CHUNK_SCHEMA_MISMATCH", self.validate_chunk_collection_schema),
            ("layer_schema", "VALIDATION_LAYER_SCHEMA_MISMATCH", self.validate_layer_collection_schema),
            ("index_state", "VALIDATION_INDEX_INCOMPLETE", self.validate_index_build_state),
            ("load_state", "VALIDATION_COLLECTION_NOT_LOADED", self.validate_collection_load_state),
            ("file_coverage", "VALIDATION_FILE_COVERAGE_INCOMPLETE", self.validate_file_coverage),
            ("file_counts", "VALIDATION_FILE_COUNT_MISMATCH", self.validate_file_counts),
            ("deleted_absence", "VALIDATION_DELETED_FILE_PRESENT", self.validate_deleted_file_absence),
            ("orphan_entities", "VALIDATION_ORPHAN_ENTITY", self.validate_orphan_entities),
            ("vector_samples", "VALIDATION_VECTOR_SAMPLE_INVALID", self.validate_vector_samples),
            ("smoke_search", "VALIDATION_SMOKE_SEARCH_FAILED", self.run_smoke_search),
        ]
        for name, code, callback in validations:
            self._check(name, code, callback)
        return assemble_validation_report(
            checks=self.checks,
            expected_counts=self.expected_counts,
            observed_counts=self.observed_counts,
            sampled_vector_count=self.sampled_vector_count,
            orphan_file_ids=self.orphan_file_ids,
            deleted_file_ids_found=self.deleted_file_ids_found,
            schema_snapshot=self.schema_snapshot,
            index_state=self.index_states,
            load_state=self.load_states,
            started_at=started_at,
        )
