"""Administrator-only control plane for candidate index generations."""

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from openrag.api.deps import get_current_user, get_db
from openrag.config import get_config
from openrag.indexing.milvus_provisioner import (
    MilvusCollectionProvisioner,
    PyMilvusAdminBackend,
)
from openrag.indexing.activation_service import (
    ActivationConflictError,
    IndexActivationError,
    IndexActivationService,
)
from openrag.indexing.alias_reconciler import AliasReconciler, PyMilvusAliasBackend
from openrag.indexing.reconciliation_service import ReconciliationService
from openrag.indexing.mirror_service import PreviousGenerationMirrorService
from openrag.indexing.metrics import (
    collect_cleanup_failure_metrics,
    collect_generation_progress_metrics,
    collect_previous_lag_metrics,
    collect_task_state_gauges,
)
from openrag.indexing.rollback_service import IndexRollbackError, IndexRollbackService
from openrag.indexing.provision_service import ProvisionService
from openrag.indexing.reindex_service import GenerationReindexService
from openrag.indexing.runtime import (
    IndexRuntimeResolver,
    build_embedding_engine_from_snapshot,
)
from openrag.indexing.source_reader import GenerationSourceReader
from openrag.indexing.validator import (
    IndexGenerationValidator,
    PyMilvusValidationBackend,
    ValidationBackend,
)
from openrag.indexing.schemas import (
    GenerationCreateRequest,
    ActivationRequest,
    RollbackRequest,
    ProvisionRequest,
    ReindexRequest,
    build_generation_plan,
)
from openrag.models.index_generation import IndexGeneration, IndexGenerationState
from openrag.models.user import User
from openrag.hierarchy.hierarchy_storage import HierarchyStorage
from openrag.storage.minio_storage import MinioStorage
from openrag.services.audit_logger import AuditLogger
from openrag.services.index_generation_service import (
    GenerationImmutableError,
    GenerationNotFoundError,
    GenerationStateTransitionError,
    IndexGenerationService,
)

router = APIRouter(prefix="/index-generations", tags=["index-generations"])
logger = logging.getLogger(__name__)


def require_index_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Index administrator privileges required",
        )
    return user


def get_provision_service(db: Session = Depends(get_db)) -> ProvisionService:
    vector = get_config().vector_db
    password = (
        vector.admin_password.get_secret_value()
        if vector.admin_password is not None
        else None
    )
    backend = PyMilvusAdminBackend(
        host=vector.host,
        port=vector.port,
        user=vector.admin_user,
        password=password,
    )
    return ProvisionService(
        IndexGenerationService(db), MilvusCollectionProvisioner(backend)
    )


def get_reindex_service(db: Session = Depends(get_db)) -> GenerationReindexService:
    reader = GenerationSourceReader(
        db,
        minio_storage=MinioStorage(),
        hierarchy_storage=HierarchyStorage(),
    )
    return GenerationReindexService(db, reader)


def get_validation_backend() -> ValidationBackend:
    vector = get_config().vector_db
    password = (
        vector.runtime_password.get_secret_value()
        if vector.runtime_password is not None
        else None
    )
    return PyMilvusValidationBackend(
        host=vector.host,
        port=vector.port,
        user=vector.runtime_user,
        password=password,
        secure=vector.secure,
    )


def _response(item: IndexGeneration) -> dict[str, Any]:
    return {
        "generation_id": item.id,
        "client_request_id": item.client_request_id,
        "state": item.state,
        "manifest": item.manifest,
        "physical_collections": {
            "chunks": item.chunk_collection_name,
            "layers": item.layer_collection_name,
        },
        "validation_summary": item.validation_report,
        "quality_report": item.quality_report,
        "quality_gate_passed": item.quality_gate_passed,
        "build_lag_files": item.build_lag_files,
        "mirror_lag_files": item.mirror_lag_files,
        "deletion_plan": item.deletion_plan,
        "last_error_code": item.last_error_code,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


def _active_source(service: IndexGenerationService) -> str:
    route = service.get_route("global")
    if route is None:
        raise HTTPException(status_code=409, detail="Active index route is unavailable")
    return route.active_generation_id


def _audit(
    db: Session,
    *,
    user_id: int,
    generation_id: str,
    operation: str,
    previous_state: str | None,
    next_state: str,
    client_request_id: str,
) -> None:
    details = json.dumps(
        {
            "generation_id": generation_id,
            "operation": operation,
            "previous_state": previous_state,
            "next_state": next_state,
            "client_request_id": client_request_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    AuditLogger(db).log(
        user_id=user_id,
        action=operation,
        resource_type="index_generation",
        resource_id=0,
        details=details,
    )


@router.post("/preview")
def preview_generation_manifest(
    request: GenerationCreateRequest,
    db: Session = Depends(get_db),
    _: User = Depends(require_index_admin),
):
    service = IndexGenerationService(db)
    plan = build_generation_plan(
        request, get_config().embedding, _active_source(service)
    )
    return {
        "generation_id": plan.generation_id,
        "state": "preview",
        "manifest": plan.manifest,
        "physical_collections": plan.physical_collections,
    }


@router.post("")
def create_index_generation(
    request: GenerationCreateRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(require_index_admin),
):
    service = IndexGenerationService(db)
    plan = build_generation_plan(
        request, get_config().embedding, _active_source(service)
    )
    existing = (
        db.query(IndexGeneration)
        .filter(IndexGeneration.client_request_id == request.client_request_id)
        .one_or_none()
    )
    try:
        item = service.create_idempotent_generation(
            client_request_id=request.client_request_id,
            created_by=admin.id,
            **plan.database_values,
        )
    except GenerationImmutableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if existing is None:
        _audit(
            db,
            user_id=admin.id,
            generation_id=item.id,
            operation="index_generation.create",
            previous_state=None,
            next_state=item.state,
            client_request_id=request.client_request_id,
        )
    return _response(item)


@router.get("")
def list_index_generations(
    db: Session = Depends(get_db),
    _: User = Depends(require_index_admin),
):
    return [_response(item) for item in IndexGenerationService(db).list_generations()]


@router.get("/{generation_id}")
def get_index_generation(
    generation_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_index_admin),
):
    item = IndexGenerationService(db).get_generation(generation_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Index generation not found")
    return _response(item)


@router.post("/{generation_id}/provision")
def provision_index_generation(
    generation_id: str,
    request: ProvisionRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(require_index_admin),
    service: ProvisionService = Depends(get_provision_service),
):
    before = IndexGenerationService(db).get_generation(generation_id)
    if before is None:
        raise HTTPException(status_code=404, detail="Index generation not found")
    previous_state = before.state
    try:
        result = service.provision(generation_id)
    except GenerationNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail="Index generation not found"
        ) from exc
    except GenerationStateTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail="Candidate collection provisioning failed"
        ) from exc
    if result.changed:
        _audit(
            db,
            user_id=admin.id,
            generation_id=result.generation.id,
            operation="index_generation.provision",
            previous_state=previous_state,
            next_state=result.generation.state,
            client_request_id=request.client_request_id,
        )
    return _response(result.generation)


@router.post("/{generation_id}/reindex")
def start_generation_reindex(
    generation_id: str,
    request: ReindexRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(require_index_admin),
    service: GenerationReindexService = Depends(get_reindex_service),
):
    generation = IndexGenerationService(db).get_generation(generation_id)
    if generation is None:
        raise HTTPException(status_code=404, detail="Index generation not found")
    try:
        initialized = service.initialize_generation_files(generation_id)
        tasks = service.enqueue_pending_files(
            generation_id,
            user_id=admin.id,
            limit=request.enqueue_limit,
            max_concurrent=request.max_concurrent,
        )
    except GenerationStateTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail="Candidate source initialization failed"
        ) from exc
    if initialized or tasks:
        _audit(
            db,
            user_id=admin.id,
            generation_id=generation_id,
            operation="index_generation.reindex",
            previous_state=generation.state,
            next_state=generation.state,
            client_request_id=request.client_request_id,
        )
    db.refresh(generation)
    response = _response(generation)
    response.update(
        initialized_file_count=initialized,
        enqueued_task_count=len(tasks),
    )
    return response


def _control_reindex(
    generation_id: str,
    request: ProvisionRequest,
    db: Session,
    admin: User,
    service: GenerationReindexService,
    operation: str,
) -> dict[str, Any]:
    generation = IndexGenerationService(db).get_generation(generation_id)
    if generation is None:
        raise HTTPException(status_code=404, detail="Index generation not found")
    previous_state = generation.state
    try:
        getattr(service, f"{operation}_generation")(generation_id)
    except GenerationStateTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.refresh(generation)
    _audit(
        db,
        user_id=admin.id,
        generation_id=generation_id,
        operation=f"index_generation.{operation}",
        previous_state=previous_state,
        next_state=generation.state,
        client_request_id=request.client_request_id,
    )
    return _response(generation)


@router.post("/{generation_id}/pause")
def pause_index_generation(
    generation_id: str,
    request: ProvisionRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(require_index_admin),
    service: GenerationReindexService = Depends(get_reindex_service),
):
    return _control_reindex(generation_id, request, db, admin, service, "pause")


@router.post("/{generation_id}/resume")
def resume_index_generation(
    generation_id: str,
    request: ProvisionRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(require_index_admin),
    service: GenerationReindexService = Depends(get_reindex_service),
):
    return _control_reindex(generation_id, request, db, admin, service, "resume")


@router.post("/{generation_id}/cancel")
def cancel_index_generation(
    generation_id: str,
    request: ProvisionRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(require_index_admin),
    service: GenerationReindexService = Depends(get_reindex_service),
):
    return _control_reindex(generation_id, request, db, admin, service, "cancel")


@router.post("/{generation_id}/validate")
def validate_index_generation(
    generation_id: str,
    request: ProvisionRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(require_index_admin),
    backend: ValidationBackend = Depends(get_validation_backend),
):
    service = IndexGenerationService(db)
    generation = service.get_generation(generation_id)
    if generation is None:
        raise HTTPException(status_code=404, detail="Index generation not found")
    previous_state = generation.state
    try:
        generation = service.start_validation(generation_id)
        snapshot = IndexRuntimeResolver().get_generation_snapshot(db, generation_id)
        report = IndexGenerationValidator(
            db,
            generation,
            backend,
            embedding_probe=lambda: build_embedding_engine_from_snapshot(
                snapshot
            ).embed_text("openrag validation probe"),
        ).build_report()
        generation = service.record_validation_report(generation_id, report)
        if report["passed"]:
            generation = service.mark_ready_if_valid(generation_id)
    except GenerationStateTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _audit(
        db,
        user_id=admin.id,
        generation_id=generation_id,
        operation="index_generation.validate",
        previous_state=previous_state,
        next_state=generation.state,
        client_request_id=request.client_request_id,
    )
    response = _response(generation)
    response["validation_report"] = generation.validation_report
    return response


@router.post("/{generation_id}/activate")
def activate_index_generation(
    generation_id: str,
    request: ActivationRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(require_index_admin),
):
    vector = get_config().vector_db
    admin_password = (
        vector.admin_password.get_secret_value()
        if vector.admin_password is not None
        else None
    )
    alias_reconciler = AliasReconciler(
        PyMilvusAliasBackend(
            host=vector.host,
            port=vector.port,
            user=vector.admin_user,
            password=admin_password,
            secure=vector.secure,
        ),
        reporter=lambda result: logger.error("milvus_alias_drift %s", result),
    )

    def final_reconcile(candidate_id: str) -> int:
        reader = GenerationSourceReader(
            db,
            minio_storage=MinioStorage(),
            hierarchy_storage=HierarchyStorage(),
        )
        reconciliation = ReconciliationService(db, reader)
        reconciliation.scan_changed_files(candidate_id)
        reconciliation.scan_deleted_files(candidate_id)
        lag = reconciliation.calculate_build_lag(candidate_id)
        if lag:
            item = db.get(IndexGeneration, candidate_id)
            if item.state == IndexGenerationState.READY.value:
                IndexGenerationService(db).transition_state(
                    candidate_id, IndexGenerationState.RECONCILING
                )
                item = db.get(IndexGeneration, candidate_id)
                item.quality_gate_passed = None
                db.commit()
        return lag

    def quick_validate(candidate_id: str) -> bool:
        item = db.get(IndexGeneration, candidate_id)
        return bool(
            item
            and item.build_lag_files == 0
            and (item.validation_report or {}).get("passed") is True
            and item.quality_gate_passed is True
        )

    resolver = IndexRuntimeResolver()
    activation = IndexActivationService(
        db,
        alias_reconciler,
        final_reconcile=final_reconcile,
        quick_validate=quick_validate,
        smoke_test=lambda _candidate_id: bool(resolver.probe_active_runtime(db)),
    )
    try:
        result = activation.activate_generation(
            generation_id,
            expected_route_version=request.expected_route_version,
            activated_by=admin.id,
        )
    except ActivationConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IndexActivationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _audit(
        db,
        user_id=admin.id,
        generation_id=generation_id,
        operation="index_generation.activate",
        previous_state=IndexGenerationState.READY.value,
        next_state=IndexGenerationState.ACTIVE.value,
        client_request_id=request.client_request_id,
    )
    return result


@router.post("/rollback")
def rollback_index_generation(
    request: RollbackRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(require_index_admin),
):
    vector = get_config().vector_db
    admin_password = (
        vector.admin_password.get_secret_value()
        if vector.admin_password is not None
        else None
    )
    alias_reconciler = AliasReconciler(
        PyMilvusAliasBackend(
            host=vector.host,
            port=vector.port,
            user=vector.admin_user,
            password=admin_password,
            secure=vector.secure,
        ),
        reporter=lambda result: logger.error("milvus_alias_drift %s", result),
    )
    reader = GenerationSourceReader(
        db,
        minio_storage=MinioStorage(),
        hierarchy_storage=HierarchyStorage(),
    )
    mirror = PreviousGenerationMirrorService(db, reader)
    resolver = IndexRuntimeResolver()

    def previous_available(generation_id: str) -> bool:
        try:
            snapshot = resolver.get_generation_snapshot(db, generation_id)
            runtime = resolver.get_runtime(snapshot)
            runtime.embedding_engine.probe()
            runtime.vector_store.probe()
            if runtime.layer_store is not None:
                runtime.layer_store.probe()
            return True
        except Exception:
            return False

    service = IndexRollbackService(
        db,
        alias_reconciler,
        previous_lag=mirror.calculate_previous_lag,
        previous_available=previous_available,
        smoke_test=lambda _generation_id: bool(resolver.probe_active_runtime(db)),
    )
    try:
        result = service.rollback_to_previous(
            expected_route_version=request.expected_route_version,
            rolled_back_by=admin.id,
            accept_rpo=request.accept_rpo,
        )
    except (ActivationConflictError, IndexRollbackError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _audit(
        db,
        user_id=admin.id,
        generation_id=result["active_generation_id"],
        operation="index_generation.rollback",
        previous_state=IndexGenerationState.RETIRED.value,
        next_state=IndexGenerationState.ACTIVE.value,
        client_request_id=request.client_request_id,
    )
    return result


@router.get("/status/metrics")
def get_index_generation_metrics(
    db: Session = Depends(get_db),
    _: User = Depends(require_index_admin),
):
    return {
        "generation_progress": collect_generation_progress_metrics(db),
        "previous_lag": collect_previous_lag_metrics(db),
        "cleanup_failures": collect_cleanup_failure_metrics(db),
        "task_state_gauges": collect_task_state_gauges(db),
    }
