"""State and optimistic concurrency operations for index generations."""

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from openrag.models.index_generation import (
    IndexGeneration,
    IndexGenerationFile,
    IndexGenerationFileState,
    IndexGenerationRoute,
    IndexGenerationState,
)


class IndexGenerationError(RuntimeError):
    """Base class for stable index generation control-plane errors."""


class GenerationNotFoundError(IndexGenerationError):
    pass


class GenerationStateTransitionError(IndexGenerationError):
    pass


class GenerationImmutableError(IndexGenerationError):
    pass


class RouteVersionConflictError(IndexGenerationError):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


_ALLOWED_TRANSITIONS: dict[IndexGenerationState, set[IndexGenerationState]] = {
    IndexGenerationState.DRAFT: {
        IndexGenerationState.PROVISIONING,
        IndexGenerationState.FAILED,
    },
    IndexGenerationState.PROVISIONING: {
        IndexGenerationState.BUILDING,
        IndexGenerationState.FAILED,
    },
    IndexGenerationState.BUILDING: {
        IndexGenerationState.RECONCILING,
        IndexGenerationState.VALIDATING,
        IndexGenerationState.FAILED,
    },
    IndexGenerationState.RECONCILING: {
        IndexGenerationState.BUILDING,
        IndexGenerationState.VALIDATING,
        IndexGenerationState.FAILED,
    },
    IndexGenerationState.VALIDATING: {
        IndexGenerationState.BUILDING,
        IndexGenerationState.READY,
        IndexGenerationState.FAILED,
    },
    IndexGenerationState.READY: {
        IndexGenerationState.ACTIVATING,
        IndexGenerationState.RECONCILING,
        IndexGenerationState.FAILED,
    },
    IndexGenerationState.ACTIVATING: {
        IndexGenerationState.ACTIVE,
        IndexGenerationState.READY,
        IndexGenerationState.FAILED,
    },
    IndexGenerationState.ACTIVE: {
        IndexGenerationState.RETIRED,
        IndexGenerationState.FAILED,
    },
    IndexGenerationState.RETIRED: {
        IndexGenerationState.ACTIVATING,
        IndexGenerationState.DELETING,
        IndexGenerationState.FAILED,
    },
    IndexGenerationState.DELETING: {
        IndexGenerationState.DELETED,
        IndexGenerationState.FAILED,
    },
    IndexGenerationState.FAILED: {
        IndexGenerationState.PROVISIONING,
        IndexGenerationState.BUILDING,
    },
    IndexGenerationState.DELETED: set(),
}


_IMMUTABLE_MANIFEST_FIELDS = {
    "source_generation_id",
    "embedding_provider",
    "embedding_model",
    "embedding_revision",
    "embedding_dimension",
    "embedding_fingerprint",
    "embedding_config_ref",
    "vector_normalization",
    "distance_metric",
    "schema_version",
    "chunk_policy_revision",
    "hierarchy_policy_revision",
    "chunk_collection_name",
    "layer_collection_name",
    "es_generation",
    "manifest",
}


def validate_generation_transition(
    current: IndexGenerationState | str,
    target: IndexGenerationState | str,
) -> None:
    current_state = IndexGenerationState(current)
    target_state = IndexGenerationState(target)
    if target_state not in _ALLOWED_TRANSITIONS[current_state]:
        raise GenerationStateTransitionError(
            f"Index generation cannot transition from {current_state.value} to {target_state.value}"
        )


class IndexGenerationService:
    def __init__(self, db: Session):
        self.db = db

    def create_generation(self, **values: Any) -> IndexGeneration:
        requested_state = values.pop("state", None)
        if requested_state not in (
            None,
            IndexGenerationState.DRAFT.value,
            IndexGenerationState.DRAFT,
        ):
            raise GenerationStateTransitionError(
                "New index generation must start in draft"
            )
        item = IndexGeneration(
            id=str(values.pop("id", uuid4())),
            state=IndexGenerationState.DRAFT.value,
            lock_version=0,
            **values,
        )
        self.db.add(item)
        self.db.commit()
        self.db.refresh(item)
        return item

    def create_idempotent_generation(
        self, *, client_request_id: str, **values: Any
    ) -> IndexGeneration:
        existing = (
            self.db.query(IndexGeneration)
            .filter(IndexGeneration.client_request_id == client_request_id)
            .one_or_none()
        )
        if existing is not None:
            self.assert_manifest_immutable(existing, values["manifest"])
            return existing
        try:
            return self.create_generation(
                client_request_id=client_request_id,
                **values,
            )
        except IntegrityError:
            self.db.rollback()
            existing = (
                self.db.query(IndexGeneration)
                .filter(IndexGeneration.client_request_id == client_request_id)
                .one_or_none()
            )
            if existing is None:
                raise
            self.assert_manifest_immutable(existing, values["manifest"])
            return existing

    @staticmethod
    def reserve_collection_names(generation_id: str) -> tuple[str, str]:
        compact = generation_id.replace("-", "")
        return (
            f"openrag_chunks_g_{compact}",
            f"openrag_layers_g_{compact}",
        )

    @staticmethod
    def assert_manifest_immutable(
        generation: IndexGeneration, expected_manifest: dict[str, Any]
    ) -> None:
        if generation.manifest != expected_manifest:
            raise GenerationImmutableError(
                "client_request_id is already bound to a different manifest"
            )

    def record_provision_result(
        self, generation_id: str, *, success: bool, error: str | None = None
    ) -> IndexGeneration:
        if success:
            return self.transition_state(generation_id, IndexGenerationState.BUILDING)
        return self.mark_failed(
            generation_id,
            error_code="INDEX_PROVISION_FAILED",
            error=(error or "Candidate collection provisioning failed")[:500],
        )

    def get_generation(self, generation_id: str) -> Optional[IndexGeneration]:
        return self.db.get(IndexGeneration, generation_id)

    def list_generations(
        self,
        *,
        scope: Optional[str] = None,
        state: Optional[IndexGenerationState | str] = None,
    ) -> list[IndexGeneration]:
        query = self.db.query(IndexGeneration)
        if scope is not None:
            query = query.filter(IndexGeneration.scope == scope)
        if state is not None:
            query = query.filter(
                IndexGeneration.state == IndexGenerationState(state).value
            )
        return query.order_by(IndexGeneration.created_at.desc()).all()

    def transition_state(
        self,
        generation_id: str,
        target: IndexGenerationState | str,
    ) -> IndexGeneration:
        item = self._require_generation(generation_id)
        target_state = IndexGenerationState(target)
        validate_generation_transition(item.state, target_state)
        item.state = target_state.value
        item.lock_version += 1
        now = utcnow()
        if (
            target_state is IndexGenerationState.BUILDING
            and item.build_started_at is None
        ):
            item.build_started_at = now
        elif target_state is IndexGenerationState.READY:
            item.ready_at = now
        elif target_state is IndexGenerationState.ACTIVE:
            item.activated_at = now
        elif target_state is IndexGenerationState.RETIRED:
            item.retired_at = now
        self.db.commit()
        self.db.refresh(item)
        return item

    def start_validation(self, generation_id: str) -> IndexGeneration:
        item = self._require_generation(generation_id)
        if item.build_lag_files != 0:
            raise GenerationStateTransitionError(
                "Generation validation requires zero build lag"
            )
        if item.state == IndexGenerationState.RECONCILING.value:
            item = self.transition_state(
                generation_id, IndexGenerationState.VALIDATING
            )
        elif item.state != IndexGenerationState.VALIDATING.value:
            raise GenerationStateTransitionError(
                "Only a reconciled generation can be validated"
            )
        item.validation_started_at = utcnow()
        item.validation_completed_at = None
        item.validation_error_code = None
        self.db.commit()
        self.db.refresh(item)
        return item

    def record_validation_report(
        self, generation_id: str, report: dict[str, Any]
    ) -> IndexGeneration:
        item = self._require_generation(generation_id)
        if item.state != IndexGenerationState.VALIDATING.value:
            raise GenerationStateTransitionError(
                "Validation reports require validating state"
            )
        failed = [
            check
            for check in report.get("checks", [])
            if check.get("status") == "failed"
        ]
        item.validation_report = report
        item.validation_completed_at = utcnow()
        item.validation_error_code = (
            failed[0].get("error_code") if failed else None
        )
        self.db.commit()
        self.db.refresh(item)
        return item

    def mark_ready_if_valid(self, generation_id: str) -> IndexGeneration:
        item = self._require_generation(generation_id)
        report = item.validation_report or {}
        if (
            item.state != IndexGenerationState.VALIDATING.value
            or report.get("passed") is not True
            or report.get("failed_checks")
        ):
            raise GenerationStateTransitionError(
                "Generation validation hard gates have not passed"
            )
        return self.transition_state(generation_id, IndexGenerationState.READY)

    def update_manifest_fields(
        self, generation_id: str, **values: Any
    ) -> IndexGeneration:
        item = self._require_generation(generation_id)
        unknown = set(values) - _IMMUTABLE_MANIFEST_FIELDS
        if unknown:
            raise ValueError(
                f"Unsupported generation manifest fields: {sorted(unknown)}"
            )
        if item.state != IndexGenerationState.DRAFT.value:
            raise GenerationImmutableError(
                f"Generation manifest is immutable in state {item.state}"
            )
        for field, value in values.items():
            setattr(item, field, value)
        item.lock_version += 1
        self.db.commit()
        self.db.refresh(item)
        return item

    def get_route(self, scope: str = "global") -> Optional[IndexGenerationRoute]:
        return self.db.get(IndexGenerationRoute, scope)

    def compare_and_swap_route(
        self,
        *,
        scope: str,
        expected_route_version: int,
        active_generation_id: str,
        previous_generation_id: Optional[str],
        activated_by: Optional[int] = None,
        rollback_deadline: Optional[datetime] = None,
    ) -> IndexGenerationRoute:
        now = utcnow()
        updated = (
            self.db.query(IndexGenerationRoute)
            .filter(
                IndexGenerationRoute.scope == scope,
                IndexGenerationRoute.route_version == expected_route_version,
            )
            .update(
                {
                    IndexGenerationRoute.active_generation_id: active_generation_id,
                    IndexGenerationRoute.previous_generation_id: previous_generation_id,
                    IndexGenerationRoute.route_version: expected_route_version + 1,
                    IndexGenerationRoute.activated_at: now,
                    IndexGenerationRoute.activated_by: activated_by,
                    IndexGenerationRoute.rollback_deadline: rollback_deadline,
                    IndexGenerationRoute.updated_at: now,
                },
                synchronize_session=False,
            )
        )
        if updated != 1:
            self.db.rollback()
            raise RouteVersionConflictError(
                f"Route {scope!r} is missing or no longer at version {expected_route_version}"
            )
        self.db.commit()
        route = self.get_route(scope)
        if route is None:  # pragma: no cover - guarded by row count
            raise RouteVersionConflictError(f"Route {scope!r} disappeared after update")
        return route

    def mark_failed(
        self,
        generation_id: str,
        *,
        error_code: str,
        error: str,
    ) -> IndexGeneration:
        item = self._require_generation(generation_id)
        if item.state == IndexGenerationState.DELETED.value:
            raise GenerationStateTransitionError(
                "Deleted generation cannot be marked failed"
            )
        item.state = IndexGenerationState.FAILED.value
        item.last_error_code = error_code
        item.last_error = error
        item.lock_version += 1
        self.db.commit()
        self.db.refresh(item)
        return item

    def recalculate_counts(self, generation_id: str) -> IndexGeneration:
        item = self._require_generation(generation_id)
        files = (
            self.db.query(IndexGenerationFile)
            .filter(IndexGenerationFile.generation_id == generation_id)
            .all()
        )
        included = [
            row for row in files if row.state != IndexGenerationFileState.DELETED.value
        ]
        succeeded = [
            row
            for row in included
            if row.state == IndexGenerationFileState.SUCCESS.value
        ]
        item.expected_file_count = len(included)
        item.expected_chunk_count = sum(row.expected_chunk_count for row in included)
        item.expected_layer_count = sum(row.expected_layer_count for row in included)
        item.indexed_file_count = len(succeeded)
        item.indexed_chunk_count = sum(row.written_chunk_count for row in succeeded)
        item.indexed_layer_count = sum(row.written_layer_count for row in succeeded)
        item.failed_file_count = sum(
            row.state == IndexGenerationFileState.FAILED.value for row in included
        )
        self.db.commit()
        self.db.refresh(item)
        return item

    def _require_generation(self, generation_id: str) -> IndexGeneration:
        item = self.get_generation(generation_id)
        if item is None:
            raise GenerationNotFoundError(
                f"Index generation {generation_id!r} was not found"
            )
        return item
