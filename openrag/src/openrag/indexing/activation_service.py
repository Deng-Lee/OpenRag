"""Serialized generation activation with a visible write barrier."""

from datetime import datetime, timedelta, timezone
import time
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.orm import Session

from openrag.indexing.alias_reconciler import AliasReconciler
from openrag.indexing.quality_gate import QualityGateService
from openrag.models.index_generation import (
    IndexGeneration,
    IndexGenerationRoute,
    IndexGenerationState,
)
from openrag.models.task import Task, TaskStatus
from openrag.broker.task_broker import TaskBroker


class IndexActivationError(RuntimeError):
    code = "INDEX_GENERATION_ACTIVATION_FAILED"


class ActivationConflictError(IndexActivationError):
    code = "INDEX_GENERATION_ACTIVATION_CONFLICT"


class IndexActivationService:
    def __init__(
        self,
        db: Session,
        alias_reconciler: AliasReconciler,
        *,
        final_reconcile: Callable[[str], int],
        quick_validate: Callable[[str], bool],
        smoke_test: Callable[[str], bool],
        drain_timeout_seconds: float = 30.0,
    ):
        self.db = db
        self.alias_reconciler = alias_reconciler
        self.final_reconcile = final_reconcile
        self.quick_validate = quick_validate
        self.smoke_test = smoke_test
        self.drain_timeout_seconds = drain_timeout_seconds
        self._lock_held = False

    def activate_generation(
        self,
        generation_id: str,
        *,
        expected_route_version: int,
        activated_by: int,
    ) -> dict[str, Any]:
        self._acquire_activation_lock("global")
        switched = False
        try:
            route, candidate, previous = self._assert_activation_preconditions(
                generation_id, expected_route_version
            )
            self._enable_write_barrier(route)
            self._drain_generation_writes(previous.id)
            self._run_final_reconciliation(candidate.id)
            if not self.quick_validate(candidate.id):
                raise IndexActivationError("Final candidate validation failed")
            route = self._switch_route_transaction(
                route,
                candidate,
                previous,
                activated_by=activated_by,
            )
            switched = True
            alias_result = self.alias_reconciler.sync_active_aliases(candidate)
        finally:
            self._disable_write_barrier("global")
            self._release_activation_lock("global")
        smoke_passed = self._run_post_activation_smoke_test(generation_id) if switched else False
        return {
            "generation_id": generation_id,
            "previous_generation_id": previous.id,
            "old_route_version": expected_route_version,
            "new_route_version": route.route_version,
            "alias_sync_result": alias_result,
            "smoke_test_passed": smoke_passed,
        }

    def _acquire_activation_lock(self, scope: str) -> None:
        if self.db.bind.dialect.name != "postgresql":
            self._lock_held = True
            return
        acquired = self.db.execute(
            text("SELECT pg_try_advisory_lock(hashtext(:key))"),
            {"key": f"openrag:index-activation:{scope}"},
        ).scalar_one()
        if not acquired:
            raise ActivationConflictError("Another activation is in progress")
        self._lock_held = True

    def _release_activation_lock(self, scope: str) -> None:
        if not self._lock_held:
            return
        if self.db.bind.dialect.name == "postgresql":
            self.db.execute(
                text("SELECT pg_advisory_unlock(hashtext(:key))"),
                {"key": f"openrag:index-activation:{scope}"},
            )
        self._lock_held = False

    def _assert_activation_preconditions(self, generation_id, expected_route_version):
        route = (
            self.db.query(IndexGenerationRoute)
            .filter(IndexGenerationRoute.scope == "global")
            .with_for_update()
            .one_or_none()
        )
        if route is None or route.route_version != expected_route_version:
            raise ActivationConflictError("Route version is stale")
        candidate = self.db.get(IndexGeneration, generation_id)
        previous = self.db.get(IndexGeneration, route.active_generation_id)
        if candidate is None or previous is None:
            raise IndexActivationError("Activation generations are unavailable")
        if (
            candidate.state != IndexGenerationState.READY.value
            or candidate.source_generation_id != previous.id
            or candidate.validation_report is None
            or candidate.validation_report.get("passed") is not True
        ):
            raise IndexActivationError("Candidate is not activation-ready")
        QualityGateService(self.db).assert_quality_gate(candidate.id)
        return route, candidate, previous

    def _enable_write_barrier(self, route: IndexGenerationRoute) -> None:
        route.write_barrier = True
        route.updated_at = datetime.now(timezone.utc)
        self.db.commit()

    def _drain_generation_writes(self, generation_id: str) -> None:
        deadline = time.monotonic() + self.drain_timeout_seconds
        while True:
            count = (
                self.db.query(Task)
                .filter(
                    Task.index_generation_id == generation_id,
                    Task.task_type.in_(TaskBroker.INDEX_WRITE_TASK_TYPES),
                    Task.status.in_(
                        [TaskStatus.ASSIGNED.value, TaskStatus.STARTED.value]
                    ),
                )
                .count()
            )
            if count == 0:
                return
            if time.monotonic() >= deadline:
                raise IndexActivationError("Timed out draining generation writes")
            time.sleep(0.05)
            self.db.expire_all()

    def _run_final_reconciliation(self, generation_id: str) -> None:
        lag = self.final_reconcile(generation_id)
        if lag != 0:
            raise IndexActivationError(f"Final reconciliation lag is {lag}")

    def _switch_route_transaction(
        self, route, candidate, previous, *, activated_by: int
    ) -> IndexGenerationRoute:
        now = datetime.now(timezone.utc)
        rollback_seconds = int(candidate.manifest.get("rollback_window_seconds") or 3600)
        route = (
            self.db.query(IndexGenerationRoute)
            .filter(
                IndexGenerationRoute.scope == route.scope,
                IndexGenerationRoute.route_version == route.route_version,
            )
            .with_for_update()
            .one_or_none()
        )
        if route is None:
            raise ActivationConflictError("Route changed during activation")
        previous.state = IndexGenerationState.RETIRED.value
        previous.retired_at = now
        self.db.flush()
        candidate.state = IndexGenerationState.ACTIVE.value
        candidate.activated_at = now
        route.previous_generation_id = previous.id
        route.active_generation_id = candidate.id
        route.route_version += 1
        route.activated_at = now
        route.activated_by = activated_by
        route.rollback_deadline = now + timedelta(seconds=rollback_seconds)
        self.db.commit()
        self.db.refresh(route)
        return route

    def _disable_write_barrier(self, scope: str) -> None:
        route = self.db.get(IndexGenerationRoute, scope)
        if route is not None and route.write_barrier:
            route.write_barrier = False
            route.updated_at = datetime.now(timezone.utc)
            self.db.commit()

    def _run_post_activation_smoke_test(self, generation_id: str) -> bool:
        try:
            return bool(self.smoke_test(generation_id))
        except Exception:
            return False
