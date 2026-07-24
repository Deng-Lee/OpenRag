"""CAS rollback guarded by measurable previous-generation RPO."""

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy.orm import Session

from openrag.indexing.activation_service import (
    ActivationConflictError,
    IndexActivationError,
    IndexActivationService,
)
from openrag.indexing.alias_reconciler import AliasReconciler
from openrag.models.index_generation import (
    IndexGeneration,
    IndexGenerationRoute,
    IndexGenerationState,
)


class IndexRollbackError(IndexActivationError):
    code = "INDEX_GENERATION_ROLLBACK_FAILED"


class IndexRollbackService:
    def __init__(
        self,
        db: Session,
        alias_reconciler: AliasReconciler,
        *,
        previous_lag: Callable[[], int],
        previous_available: Callable[[str], bool],
        smoke_test: Callable[[str], bool],
        drain_timeout_seconds: float = 30.0,
    ):
        self.db = db
        self.alias_reconciler = alias_reconciler
        self.previous_lag = previous_lag
        self.previous_available = previous_available
        self.smoke_test = smoke_test
        self.protocol = IndexActivationService(
            db,
            alias_reconciler,
            final_reconcile=lambda _id: 0,
            quick_validate=lambda _id: True,
            smoke_test=smoke_test,
            drain_timeout_seconds=drain_timeout_seconds,
        )

    def rollback_to_previous(
        self,
        *,
        expected_route_version: int,
        rolled_back_by: int,
        accept_rpo: bool = False,
    ) -> dict[str, Any]:
        self.protocol._acquire_activation_lock("global")
        switched = False
        try:
            route, current, previous = self._assert_previous_available(
                expected_route_version
            )
            lag = self._reconcile_previous_before_rollback()
            self._assert_rollback_rpo(lag, accept_rpo=accept_rpo)
            self.protocol._enable_write_barrier(route)
            self._drain_active_writes(current.id)
            route = self._switch_route_transaction(
                route, current, previous, rolled_back_by=rolled_back_by
            )
            switched = True
            alias_result = self.alias_reconciler.sync_active_aliases(previous)
        finally:
            self.protocol._disable_write_barrier("global")
            self.protocol._release_activation_lock("global")
        smoke_passed = (
            self._run_post_rollback_smoke_test(previous.id) if switched else False
        )
        return {
            "active_generation_id": previous.id,
            "previous_generation_id": current.id,
            "new_route_version": route.route_version,
            "accepted_rpo_lag_files": lag if accept_rpo else 0,
            "alias_sync_result": alias_result,
            "smoke_test_passed": smoke_passed,
        }

    def _assert_previous_available(self, expected_route_version):
        route = (
            self.db.query(IndexGenerationRoute)
            .filter(IndexGenerationRoute.scope == "global")
            .with_for_update()
            .one_or_none()
        )
        if route is None or route.route_version != expected_route_version:
            raise ActivationConflictError("Route version is stale")
        if route.previous_generation_id is None:
            raise IndexRollbackError("Route has no previous generation")
        deadline = route.rollback_deadline
        if deadline is None:
            raise IndexRollbackError("Rollback window is unavailable")
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        if deadline <= datetime.now(timezone.utc):
            raise IndexRollbackError("Rollback window has expired")
        current = self.db.get(IndexGeneration, route.active_generation_id)
        previous = self.db.get(IndexGeneration, route.previous_generation_id)
        if (
            current is None
            or previous is None
            or current.state != IndexGenerationState.ACTIVE.value
            or previous.state != IndexGenerationState.RETIRED.value
            or not self.previous_available(previous.id)
        ):
            raise IndexRollbackError("Previous generation is not serving-ready")
        return route, current, previous

    @staticmethod
    def _assert_rollback_rpo(lag: int, *, accept_rpo: bool) -> None:
        if lag != 0 and not accept_rpo:
            raise IndexRollbackError(
                f"Lossless rollback requires zero mirror lag; observed {lag}"
            )

    def _drain_active_writes(self, generation_id: str) -> None:
        self.protocol._drain_generation_writes(generation_id)

    def _reconcile_previous_before_rollback(self) -> int:
        return int(self.previous_lag())

    def _switch_route_transaction(
        self, route, current, previous, *, rolled_back_by: int
    ):
        now = datetime.now(timezone.utc)
        locked = (
            self.db.query(IndexGenerationRoute)
            .filter(
                IndexGenerationRoute.scope == route.scope,
                IndexGenerationRoute.route_version == route.route_version,
            )
            .with_for_update()
            .one_or_none()
        )
        if locked is None:
            raise ActivationConflictError("Route changed during rollback")
        current.state = IndexGenerationState.RETIRED.value
        current.retired_at = now
        self.db.flush()
        previous.state = IndexGenerationState.ACTIVE.value
        previous.activated_at = now
        locked.active_generation_id = previous.id
        locked.previous_generation_id = current.id
        locked.route_version += 1
        locked.activated_at = now
        locked.activated_by = rolled_back_by
        rollback_seconds = int(previous.manifest.get("rollback_window_seconds") or 3600)
        locked.rollback_deadline = now + timedelta(seconds=rollback_seconds)
        self.db.commit()
        self.db.refresh(locked)
        return locked

    def _run_post_rollback_smoke_test(self, generation_id: str) -> bool:
        try:
            return bool(self.smoke_test(generation_id))
        except Exception:
            return False
