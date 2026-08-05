"""Safely repair one Workspace's legacy Elasticsearch chunk index."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from openrag.database import SessionLocal, get_engine
from openrag.models.document_chunk import DocumentChunk
from openrag.models.file import File
from openrag.models.workspace import Workspace
from openrag.search.es_chunk_store import create_es_chunk_store_from_config
from openrag.search.workspace_es_slug import (
    build_workspace_chunks_index_name,
    normalize_workspace_slug_segment,
)
from openrag.storage.minio_storage import MinioStorage

if __package__:
    from scripts.audit_es_chunk_consistency import (
        audit_workspace,
        inspect_legacy_mapping,
    )
    from scripts.legacy_es_report import build_workspace_row, render_terminal_table
else:
    from audit_es_chunk_consistency import audit_workspace, inspect_legacy_mapping
    from legacy_es_report import build_workspace_row, render_terminal_table


_FULL_ID_LIMIT = 2**31 - 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _batches(values: list[Any], size: int) -> Iterator[list[Any]]:
    if size <= 0:
        raise ValueError("batch_size must be positive")
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _build_legacy_chunk_document(
    *,
    chunk_id: str,
    file_id: int,
    workspace_id: int,
    workspace_slug: str,
    content: str,
) -> dict[str, Any]:
    """Build only the five fields declared by the legacy index mapping."""
    return {
        "chunk_id": str(chunk_id or "")[:64],
        "file_id": int(file_id),
        "workspace_id": int(workspace_id),
        "workspace_slug": normalize_workspace_slug_segment(
            workspace_slug, int(workspace_id)
        ),
        "content": str(content or "")[:65000],
    }


def _bulk_upsert_legacy_chunks(
    store, index_name: str, documents: list[dict[str, Any]]
) -> tuple[int, list[Any]]:
    if not documents:
        return 0, []
    from elasticsearch.helpers import bulk

    actions = [
        {
            "_op_type": "index",
            "_index": index_name,
            "_id": document["chunk_id"],
            "_source": document,
        }
        for document in documents
    ]
    accepted, errors = bulk(
        store.client,
        actions,
        refresh="wait_for",
        raise_on_error=False,
    )
    return int(accepted), list(errors or [])


def _json_safe_error_samples(errors: list[Any]) -> list[Any]:
    return json.loads(json.dumps(errors[:3], ensure_ascii=False, default=str))


def _active_rows(
    db: Session, workspace_id: int
) -> tuple[Workspace, list[tuple[DocumentChunk, File]]]:
    workspace = db.get(Workspace, workspace_id)
    if workspace is None:
        raise ValueError(f"Workspace not found: {workspace_id}")
    rows = db.execute(
        select(DocumentChunk, File)
        .join(File, File.id == DocumentChunk.file_id)
        .where(
            DocumentChunk.workspace_id == workspace_id,
            File.workspace_id == workspace_id,
            File.deleted_at.is_(None),
        )
        .order_by(DocumentChunk.id)
    ).all()
    return workspace, list(rows)


def _missing_rows(client, index_name: str, rows, batch_size: int):
    missing = []
    for batch in _batches(rows, batch_size):
        ids = [str(chunk.chunk_id) for chunk, _file in batch]
        response = client.mget(index=index_name, ids=ids, source=False)
        found = {
            str(document.get("_id"))
            for document in response.get("docs", [])
            if document.get("found")
        }
        missing.extend(
            (chunk, file_row)
            for chunk, file_row in batch
            if str(chunk.chunk_id) not in found
        )
    return missing


def _load_minio_l2(storage, workspace: Workspace, candidates):
    loaded = []
    failures = []
    readable_bytes = 0
    empty_count = 0
    for chunk, file_row in candidates:
        failure = {
            "chunk_id": str(chunk.chunk_id),
            "file_id": int(file_row.id),
            "object_key": str(chunk.object_key or ""),
        }
        if not chunk.object_key:
            failures.append(
                {
                    **failure,
                    "category": "MISSING_OBJECT_KEY",
                    "reason": "empty object_key",
                }
            )
            continue
        try:
            raw = storage.read_object_bytes(workspace.slug, chunk.object_key)
            text = raw.decode("utf-8")
        except Exception as exc:
            failures.append(
                {
                    **failure,
                    "category": "MINIO_L2_UNREADABLE",
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        readable_bytes += len(raw)
        empty_count += int(not bool(text))
        loaded.append((chunk, file_row, workspace, text))
    return loaded, {
        "candidate_count": len(candidates),
        "readable_l2_count": len(loaded),
        "readable_l2_bytes": readable_bytes,
        "empty_l2_count": empty_count,
        "missing_l2_count": len(failures),
        "failures": failures,
        "status": (
            "FAILED_L2"
            if failures
            else ("PASSED" if candidates else "NO_REPAIR_CANDIDATES")
        ),
    }


def _scan_workspace(
    *,
    db: Session,
    store,
    storage,
    workspace_id: int,
    batch_size: int,
):
    workspace, rows = _active_rows(db, workspace_id)
    index_name = build_workspace_chunks_index_name(
        workspace.slug, workspace.id
    )
    mapping = inspect_legacy_mapping(
        store.client,
        index_name=index_name,
        active_chunk_count=len(rows),
    )
    if mapping["resource_type"] == "physical_index":
        candidates = _missing_rows(
            store.client, index_name, rows, batch_size
        )
    else:
        candidates = rows
    loaded, l2 = _load_minio_l2(storage, workspace, candidates)

    pre_audit = None
    audit_error = None
    candidate_drift = False
    if mapping["resource_type"] in {"physical_index", "absent"}:
        try:
            pre_audit = _legacy_audit(
                db=db,
                store=store,
                workspace_id=workspace_id,
                index_name=index_name,
                batch_size=batch_size,
            )
            candidate_ids = {
                str(chunk.chunk_id) for chunk, _file in candidates
            }
            audited_missing_ids = set(pre_audit["missing_es_chunk_ids"])
            candidate_drift = (
                pre_audit["db_active_chunk_count"] != len(rows)
                or pre_audit["missing_es_chunk_count"] != len(candidates)
                or audited_missing_ids != candidate_ids
            )
        except Exception as exc:
            audit_error = f"{type(exc).__name__}: {exc}"

    if l2["status"] == "FAILED_L2":
        gate = "BLOCKED_L2"
    elif mapping["status"] == "MAPPING_INCOMPATIBLE":
        gate = "BLOCKED_MAPPING"
    elif mapping["status"] in {
        "ALIAS_CONFLICT",
        "RESOURCE_TARGET_MISMATCH",
        "ES_PREFLIGHT_ERROR",
    }:
        gate = "BLOCKED_ES_RESOURCE"
    elif audit_error:
        gate = "BLOCKED_LEGACY_AUDIT"
    elif candidate_drift:
        gate = "BLOCKED_CONCURRENT_DRIFT"
    elif pre_audit and (
        pre_audit["file_id_mismatch_count"]
        or pre_audit["deleted_file_chunk_count"]
    ):
        gate = "BLOCKED_LEGACY_AUDIT"
    elif mapping["status"] == "INDEX_ABSENT":
        gate = "READY_AFTER_APPROVED_INDEX_CREATE"
    elif mapping["status"] == "EMPTY_INDEX_NOT_REQUIRED":
        gate = "EMPTY_PASSED"
    elif candidates:
        gate = "READY_FOR_MISSING_ONLY_UPSERT"
    else:
        gate = "PASSED_NO_REPAIR_NEEDED"

    report = {
        "format_version": 1,
        "workspace_id": int(workspace.id),
        "workspace_name": workspace.name,
        "workspace_slug": workspace.slug,
        "legacy_index_name": index_name,
        "checked_at": _utc_now(),
        "read_only": True,
        "db_active_chunk_count": len(rows),
        "repair_candidate_count": len(candidates),
        "repair_candidate_chunk_ids": [
            str(chunk.chunk_id) for chunk, _file in candidates
        ],
        "mapping_preflight": mapping,
        "l2_preflight": l2,
        "pre_legacy_audit": pre_audit,
        "pre_legacy_audit_error": audit_error,
        "repair_gate": gate,
    }
    return report, loaded


def preflight_workspace(
    *,
    db: Session,
    store,
    storage,
    workspace_id: int,
    batch_size: int = 500,
) -> dict[str, Any]:
    report, _loaded = _scan_workspace(
        db=db,
        store=store,
        storage=storage,
        workspace_id=workspace_id,
        batch_size=batch_size,
    )
    return report


def _legacy_audit(
    *, db: Session, store, workspace_id: int, index_name: str, batch_size: int
) -> dict[str, Any]:
    return audit_workspace(
        db=db,
        es_client=store.client,
        index_name=index_name,
        workspace_id=workspace_id,
        batch_size=batch_size,
        sample_limit=_FULL_ID_LIMIT,
        index_version="legacy",
    )


def _mapping_snapshot_sha256(mapping: dict[str, Any]) -> str:
    return _manifest_hash(
        {
            "resource_type": mapping.get("resource_type"),
            "status": mapping.get("status"),
            "raw_mapping": mapping.get("raw_mapping"),
        }
    )


def _missing_plan_payload(preflight: dict[str, Any]) -> dict[str, Any]:
    return {
        "format_version": 1,
        "workspace_id": int(preflight["workspace_id"]),
        "workspace_slug": preflight["workspace_slug"],
        "legacy_index_name": preflight["legacy_index_name"],
        "db_active_chunk_count": int(preflight["db_active_chunk_count"]),
        "repair_candidate_chunk_ids": sorted(
            str(value) for value in preflight["repair_candidate_chunk_ids"]
        ),
        "mapping_status": preflight["mapping_preflight"]["status"],
        "mapping_snapshot_sha256": _mapping_snapshot_sha256(
            preflight["mapping_preflight"]
        ),
        "requires_index_create": (
            preflight["mapping_preflight"]["status"] == "INDEX_ABSENT"
        ),
    }


def build_missing_repair_plan(
    *,
    db: Session,
    store,
    storage,
    workspace_id: int,
    batch_size: int = 500,
) -> dict[str, Any]:
    preflight = preflight_workspace(
        db=db,
        store=store,
        storage=storage,
        workspace_id=workspace_id,
        batch_size=batch_size,
    )
    gate = preflight["repair_gate"]
    if gate not in {
        "READY_FOR_MISSING_ONLY_UPSERT",
        "READY_AFTER_APPROVED_INDEX_CREATE",
    }:
        return {
            "format_version": 1,
            "status": (
                "PASSED_NO_MISSING_REPAIR_NEEDED"
                if gate in {"PASSED_NO_REPAIR_NEEDED", "EMPTY_PASSED"}
                else gate
            ),
            "workspace_id": preflight["workspace_id"],
            "workspace_name": preflight["workspace_name"],
            "workspace_slug": preflight["workspace_slug"],
            "preflight": preflight,
            "approval_payload": None,
            "manifest_sha256": None,
        }
    payload = _missing_plan_payload(preflight)
    return {
        "format_version": 1,
        "status": "AWAITING_MISSING_REPAIR_APPROVAL",
        "generated_at": _utc_now(),
        "workspace_id": preflight["workspace_id"],
        "workspace_name": preflight["workspace_name"],
        "workspace_slug": preflight["workspace_slug"],
        "preflight": preflight,
        "approval_payload": payload,
        "manifest_sha256": _manifest_hash(payload),
    }


def _preflight_matches_missing_plan(
    preflight: dict[str, Any], payload: dict[str, Any]
) -> bool:
    return _missing_plan_payload(preflight) == payload


def _blocked_missing_result(status: str, preflight: dict[str, Any]):
    return {
        "format_version": 1,
        "status": status,
        "preflight": preflight,
        "index_created": False,
        "submitted_count": 0,
        "accepted_count": 0,
        "write_batches": [],
        "post_legacy_audit": None,
        "post_legacy_audit_error": None,
    }


def repair_missing_chunks(
    *,
    db: Session,
    store,
    storage,
    workspace_id: int,
    allow_create_missing_index: bool = False,
    batch_size: int = 500,
    approved_plan_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    preflight, loaded = _scan_workspace(
        db=db,
        store=store,
        storage=storage,
        workspace_id=workspace_id,
        batch_size=batch_size,
    )
    if approved_plan_payload is not None and not _preflight_matches_missing_plan(
        preflight, approved_plan_payload
    ):
        return _blocked_missing_result("BLOCKED_MISSING_PLAN_DRIFT", preflight)
    mapping_status = preflight["mapping_preflight"]["status"]
    if preflight["l2_preflight"]["status"] == "FAILED_L2":
        return _blocked_missing_result("BLOCKED_L2", preflight)
    if mapping_status == "MAPPING_INCOMPATIBLE":
        return _blocked_missing_result("BLOCKED_MAPPING", preflight)
    if mapping_status in {
        "ALIAS_CONFLICT",
        "RESOURCE_TARGET_MISMATCH",
        "ES_PREFLIGHT_ERROR",
    }:
        return _blocked_missing_result("BLOCKED_ES_RESOURCE", preflight)
    if preflight["repair_gate"] in {
        "BLOCKED_LEGACY_AUDIT",
        "BLOCKED_CONCURRENT_DRIFT",
    }:
        return _blocked_missing_result(preflight["repair_gate"], preflight)
    if mapping_status == "INDEX_ABSENT" and not allow_create_missing_index:
        return _blocked_missing_result(
            "BLOCKED_INDEX_CREATE_APPROVAL", preflight
        )

    index_name = preflight["legacy_index_name"]
    index_created = False
    if mapping_status == "INDEX_ABSENT":
        store.ensure_index(index_name)
        index_created = True
        after_create = inspect_legacy_mapping(
            store.client,
            index_name=index_name,
            active_chunk_count=preflight["db_active_chunk_count"],
        )
        if not after_create["compatible"]:
            result = _blocked_missing_result("BLOCKED_MAPPING", preflight)
            result.update(
                index_created=True,
                mapping_after_index_create=after_create,
            )
            return result

    result: dict[str, Any] = {
        "format_version": 1,
        "status": "RUNNING",
        "preflight": preflight,
        "index_created": index_created,
        "submitted_count": 0,
        "accepted_count": 0,
        "write_batches": [],
        "post_legacy_audit": None,
    }
    bulk_failed = False
    for batch_number, batch in enumerate(_batches(loaded, batch_size), start=1):
        documents = [
            _build_legacy_chunk_document(
                chunk_id=chunk.chunk_id,
                file_id=file_row.id,
                workspace_id=workspace.id,
                workspace_slug=workspace.slug,
                content=text,
            )
            for chunk, file_row, workspace, text in batch
        ]
        submitted = len(documents)
        result["submitted_count"] += submitted
        try:
            accepted, errors = _bulk_upsert_legacy_chunks(
                store, index_name, documents
            )
        except Exception as exc:
            result["write_batches"].append(
                {
                    "batch_number": batch_number,
                    "submitted": submitted,
                    "accepted": 0,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            result["status"] = "FAILED_BULK_EXCEPTION"
            bulk_failed = True
            break
        result["accepted_count"] += accepted
        batch_result = {
            "batch_number": batch_number,
            "submitted": submitted,
            "accepted": accepted,
        }
        if errors:
            batch_result["error_samples"] = _json_safe_error_samples(errors)
        result["write_batches"].append(batch_result)
        if accepted != submitted:
            result["status"] = "FAILED_PARTIAL_BULK"
            bulk_failed = True
            break

    try:
        result["post_legacy_audit"] = _legacy_audit(
            db=db,
            store=store,
            workspace_id=workspace_id,
            index_name=index_name,
            batch_size=batch_size,
        )
    except Exception as exc:
        result["status"] = "FAILED_POST_AUDIT_ERROR"
        result["post_legacy_audit_error"] = (
            f"{type(exc).__name__}: {exc}"
        )
        return result
    if bulk_failed:
        return result

    audit = result["post_legacy_audit"]
    if (
        audit["missing_es_chunk_count"]
        or audit["file_id_mismatch_count"]
        or audit["deleted_file_chunk_count"]
    ):
        result["status"] = "FAILED_POST_AUDIT"
    elif audit["orphan_chunk_count"] != (
        preflight.get("pre_legacy_audit") or {}
    ).get("orphan_chunk_count", 0):
        result["status"] = "FAILED_POST_AUDIT_ORPHAN_DRIFT"
    elif not result["submitted_count"]:
        result["status"] = (
            "PASSED_NOOP_ORPHANS_REMAIN"
            if audit["orphan_chunk_count"]
            else "PASSED_NOOP"
        )
    elif audit["orphan_chunk_count"]:
        result["status"] = "PASSED_MISSING_REPAIRED_ORPHANS_REMAIN"
    else:
        result["status"] = "PASSED_MISSING_REPAIRED"
    return result


def repair_approved_missing_chunks(
    *,
    db: Session,
    store,
    storage,
    workspace_id: int,
    plan: dict[str, Any],
    approval_sha256: str,
    allow_create_missing_index: bool = False,
    batch_size: int = 500,
) -> dict[str, Any]:
    payload = plan.get("approval_payload")
    if not isinstance(payload, dict):
        return {
            "format_version": 1,
            "status": "BLOCKED_MISSING_APPROVAL",
            "workspace_id": workspace_id,
        }
    expected_hash = _manifest_hash(payload)
    if (
        plan.get("status") != "AWAITING_MISSING_REPAIR_APPROVAL"
        or plan.get("manifest_sha256") != expected_hash
        or approval_sha256 != expected_hash
        or int(payload.get("workspace_id", -1)) != int(workspace_id)
    ):
        return {
            "format_version": 1,
            "status": "BLOCKED_MISSING_APPROVAL",
            "workspace_id": workspace_id,
        }
    if payload.get("requires_index_create") and not allow_create_missing_index:
        return {
            "format_version": 1,
            "status": "BLOCKED_INDEX_CREATE_APPROVAL",
            "workspace_id": workspace_id,
        }
    return repair_missing_chunks(
        db=db,
        store=store,
        storage=storage,
        workspace_id=workspace_id,
        allow_create_missing_index=allow_create_missing_index,
        batch_size=batch_size,
        approved_plan_payload=payload,
    )


def _manifest_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _diagnose_orphan_ids(
    db: Session,
    *,
    workspace_id: int,
    orphan_ids: list[str],
    batch_size: int,
    sample_limit: int = 20,
) -> dict[str, Any]:
    chunks: dict[str, DocumentChunk] = {}
    for batch in _batches(orphan_ids, batch_size):
        chunks.update(
            {
                str(row.chunk_id): row
                for row in db.execute(
                    select(DocumentChunk).where(DocumentChunk.chunk_id.in_(batch))
                ).scalars()
            }
        )
    file_ids = sorted({chunk.file_id for chunk in chunks.values()})
    files: dict[int, File] = {}
    for batch in _batches(file_ids, batch_size):
        files.update(
            {
                int(row.id): row
                for row in db.execute(select(File).where(File.id.in_(batch))).scalars()
            }
        )
    reason_counts: dict[str, int] = {}
    samples: list[dict[str, Any]] = []
    for chunk_id in orphan_ids:
        chunk = chunks.get(chunk_id)
        file_row = files.get(chunk.file_id) if chunk is not None else None
        if chunk is None:
            reason = "DB_CHUNK_MISSING"
        elif file_row is None:
            reason = "FILE_MISSING"
        elif file_row.deleted_at is not None:
            reason = "FILE_SOFT_DELETED"
        elif int(chunk.workspace_id) != int(workspace_id):
            reason = "CHUNK_WORKSPACE_MISMATCH"
        elif int(file_row.workspace_id) != int(workspace_id):
            reason = "FILE_WORKSPACE_MISMATCH"
        else:
            reason = "UNCLASSIFIED"
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
        if len(samples) < sample_limit:
            samples.append(
                {
                    "chunk_id": chunk_id,
                    "reason": reason,
                    "db_chunk_id": int(chunk.id) if chunk is not None else None,
                    "file_id": int(chunk.file_id) if chunk is not None else None,
                }
            )
    return {
        "reason_counts": dict(sorted(reason_counts.items())),
        "samples": samples,
    }


def build_orphan_delete_manifest(
    *,
    db: Session,
    store,
    workspace_id: int,
    batch_size: int = 500,
) -> dict[str, Any]:
    workspace, rows = _active_rows(db, workspace_id)
    index_name = build_workspace_chunks_index_name(
        workspace.slug, workspace.id
    )
    mapping = inspect_legacy_mapping(
        store.client,
        index_name=index_name,
        active_chunk_count=len(rows),
    )
    if not mapping["compatible"]:
        return {
            "format_version": 1,
            "status": "BLOCKED_MAPPING",
            "workspace_id": workspace.id,
            "legacy_index_name": index_name,
            "mapping_preflight": mapping,
            "orphan_chunk_ids": [],
        }
    audit = _legacy_audit(
        db=db,
        store=store,
        workspace_id=workspace_id,
        index_name=index_name,
        batch_size=batch_size,
    )
    if (
        audit["missing_es_chunk_count"]
        or audit["file_id_mismatch_count"]
        or audit["deleted_file_chunk_count"]
    ):
        return {
            "format_version": 1,
            "status": "BLOCKED_LEGACY_AUDIT",
            "workspace_id": workspace.id,
            "legacy_index_name": index_name,
            "mapping_preflight": mapping,
            "legacy_audit": audit,
            "orphan_chunk_ids": [],
        }
    orphan_ids = sorted(audit["orphan_chunk_ids"])
    if len(orphan_ids) != audit["orphan_chunk_count"]:
        return {
            "format_version": 1,
            "status": "BLOCKED_INCOMPLETE_ORPHAN_LIST",
            "workspace_id": workspace.id,
            "legacy_index_name": index_name,
            "legacy_audit": audit,
            "orphan_chunk_ids": orphan_ids,
        }
    if not orphan_ids:
        return {
            "format_version": 1,
            "status": "PASSED_NO_ORPHANS",
            "workspace_id": workspace.id,
            "workspace_slug": workspace.slug,
            "legacy_index_name": index_name,
            "legacy_audit": audit,
            "orphan_chunk_ids": [],
        }
    diagnosis = _diagnose_orphan_ids(
        db,
        workspace_id=workspace_id,
        orphan_ids=orphan_ids,
        batch_size=batch_size,
    )
    approval_payload = {
        "format_version": 1,
        "workspace_id": int(workspace.id),
        "workspace_slug": workspace.slug,
        "legacy_index_name": index_name,
        "orphan_chunk_ids": orphan_ids,
    }
    return {
        "format_version": 1,
        "status": "AWAITING_SEPARATE_APPROVAL",
        "generated_at": _utc_now(),
        "approval_payload": approval_payload,
        "manifest_sha256": _manifest_hash(approval_payload),
        "workspace_id": workspace.id,
        "workspace_slug": workspace.slug,
        "legacy_index_name": index_name,
        "orphan_chunk_count": len(orphan_ids),
        "orphan_chunk_ids": orphan_ids,
        "orphan_reason_counts": diagnosis["reason_counts"],
        "orphan_reason_samples": diagnosis["samples"],
        "legacy_audit": audit,
    }


def delete_approved_orphans(
    *,
    db: Session,
    store,
    workspace_id: int,
    manifest: dict[str, Any],
    approval_sha256: str,
    batch_size: int = 500,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "format_version": 1,
        "status": "BLOCKED_APPROVAL",
        "workspace_id": workspace_id,
        "submitted_delete_count": 0,
        "accepted_delete_count": 0,
        "submitted_delete_chunk_ids": [],
        "post_legacy_audit": None,
        "post_legacy_audit_error": None,
    }
    payload = manifest.get("approval_payload")
    if not isinstance(payload, dict):
        return result
    expected_hash = _manifest_hash(payload)
    if (
        manifest.get("status") != "AWAITING_SEPARATE_APPROVAL"
        or manifest.get("manifest_sha256") != expected_hash
        or approval_sha256 != expected_hash
        or int(payload.get("workspace_id", -1)) != int(workspace_id)
    ):
        return result

    workspace, rows = _active_rows(db, workspace_id)
    index_name = build_workspace_chunks_index_name(
        workspace.slug, workspace.id
    )
    if (
        payload.get("workspace_slug") != workspace.slug
        or payload.get("legacy_index_name") != index_name
    ):
        return result
    mapping = inspect_legacy_mapping(
        store.client,
        index_name=index_name,
        active_chunk_count=len(rows),
    )
    if not mapping["compatible"]:
        result["status"] = "BLOCKED_MAPPING"
        result["mapping_preflight"] = mapping
        return result

    current_audit = _legacy_audit(
        db=db,
        store=store,
        workspace_id=workspace_id,
        index_name=index_name,
        batch_size=batch_size,
    )
    if (
        current_audit["missing_es_chunk_count"]
        or current_audit["file_id_mismatch_count"]
        or current_audit["deleted_file_chunk_count"]
    ):
        result["status"] = "BLOCKED_LEGACY_AUDIT"
        result["pre_delete_legacy_audit"] = current_audit
        return result
    approved_ids = sorted(str(value) for value in payload["orphan_chunk_ids"])
    current_ids = sorted(current_audit["orphan_chunk_ids"])
    if approved_ids != current_ids:
        result["status"] = "BLOCKED_ORPHAN_DRIFT"
        result["approved_orphan_chunk_ids"] = approved_ids
        result["current_orphan_chunk_ids"] = current_ids
        return result

    result["submitted_delete_chunk_ids"] = approved_ids
    result["submitted_delete_count"] = len(approved_ids)
    try:
        accepted, errors = store.bulk_delete_chunks(index_name, approved_ids)
        if errors:
            result["delete_error_samples"] = _json_safe_error_samples(errors)
    except Exception as exc:
        result["status"] = "FAILED_DELETE_EXCEPTION"
        result["error"] = f"{type(exc).__name__}: {exc}"
        accepted = 0
    result["accepted_delete_count"] = accepted
    try:
        result["post_legacy_audit"] = _legacy_audit(
            db=db,
            store=store,
            workspace_id=workspace_id,
            index_name=index_name,
            batch_size=batch_size,
        )
    except Exception as exc:
        result["status"] = "FAILED_POST_AUDIT_ERROR"
        result["post_legacy_audit_error"] = (
            f"{type(exc).__name__}: {exc}"
        )
        return result
    if result["status"] == "FAILED_DELETE_EXCEPTION":
        return result
    if accepted != len(approved_ids):
        result["status"] = "FAILED_PARTIAL_DELETE"
    elif result["post_legacy_audit"]["anomaly_count"]:
        result["status"] = "FAILED_POST_AUDIT"
    else:
        result["status"] = "PASSED_ORPHANS_DELETED"
    return result


def _write_json(path: Path, value: dict[str, Any]) -> Path:
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite report: {path}")
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def save_action_report(
    output_dir: Path, action: str, result: dict[str, Any]
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    if action == "preflight":
        return [_write_json(output_dir / "01-preflight.json", result)]
    if action == "prepare-missing":
        return [_write_json(output_dir / "02-missing-plan.json", result)]
    if action == "apply-missing":
        paths = [
            _write_json(output_dir / "03-missing-apply.json", result),
        ]
        if result.get("post_legacy_audit") is not None:
            paths.append(
                _write_json(
                    output_dir / "04-post-missing-audit.json",
                    result["post_legacy_audit"],
                )
            )
        return paths
    if action == "prepare-orphan-delete":
        return [
            _write_json(
                output_dir / "05-orphan-delete-manifest.json", result
            )
        ]
    if action == "apply-orphan-delete":
        paths = [
            _write_json(output_dir / "06-orphan-delete.json", result)
        ]
        if result.get("post_legacy_audit") is not None:
            paths.append(
                _write_json(
                    output_dir / "07-post-orphan-audit.json",
                    result["post_legacy_audit"],
                )
            )
        return paths
    raise ValueError(f"Unsupported action: {action}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Preflight or repair one Workspace legacy ES chunk index"
    )
    parser.add_argument("--workspace-id", type=int, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=500)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--preflight", action="store_true")
    action.add_argument("--prepare-missing", action="store_true")
    action.add_argument("--apply-missing", action="store_true")
    action.add_argument("--prepare-orphan-delete", action="store_true")
    action.add_argument("--apply-orphan-delete", action="store_true")
    parser.add_argument("--allow-create-missing-index", action="store_true")
    parser.add_argument("--missing-plan", type=Path)
    parser.add_argument("--approve-missing-sha256")
    parser.add_argument("--orphan-manifest", type=Path)
    parser.add_argument("--approve-orphan-delete-sha256")
    args = parser.parse_args(argv)

    if args.allow_create_missing_index and not args.apply_missing:
        parser.error("--allow-create-missing-index requires --apply-missing")
    if args.apply_missing and (
        args.missing_plan is None or not args.approve_missing_sha256
    ):
        parser.error(
            "--apply-missing requires --missing-plan and "
            "--approve-missing-sha256"
        )
    if args.apply_orphan_delete and (
        args.orphan_manifest is None
        or not args.approve_orphan_delete_sha256
    ):
        parser.error(
            "--apply-orphan-delete requires --orphan-manifest and "
            "--approve-orphan-delete-sha256"
        )
    args.output_dir.mkdir(parents=True, exist_ok=True)

    SessionLocal.configure(bind=get_engine())
    db = SessionLocal()
    try:
        store = create_es_chunk_store_from_config()
        if store is None or not store.ping():
            raise RuntimeError("Elasticsearch is disabled or unavailable")
        storage = MinioStorage()
        if args.preflight:
            action_name = "preflight"
            result = preflight_workspace(
                db=db,
                store=store,
                storage=storage,
                workspace_id=args.workspace_id,
                batch_size=args.batch_size,
            )
        elif args.prepare_missing:
            action_name = "prepare-missing"
            result = build_missing_repair_plan(
                db=db,
                store=store,
                storage=storage,
                workspace_id=args.workspace_id,
                batch_size=args.batch_size,
            )
        elif args.apply_missing:
            action_name = "apply-missing"
            plan = json.loads(args.missing_plan.read_text(encoding="utf-8"))
            result = repair_approved_missing_chunks(
                db=db,
                store=store,
                storage=storage,
                workspace_id=args.workspace_id,
                plan=plan,
                approval_sha256=args.approve_missing_sha256,
                allow_create_missing_index=args.allow_create_missing_index,
                batch_size=args.batch_size,
            )
        elif args.prepare_orphan_delete:
            action_name = "prepare-orphan-delete"
            result = build_orphan_delete_manifest(
                db=db,
                store=store,
                workspace_id=args.workspace_id,
                batch_size=args.batch_size,
            )
        else:
            action_name = "apply-orphan-delete"
            manifest = json.loads(
                args.orphan_manifest.read_text(encoding="utf-8")
            )
            result = delete_approved_orphans(
                db=db,
                store=store,
                workspace_id=args.workspace_id,
                manifest=manifest,
                approval_sha256=args.approve_orphan_delete_sha256,
                batch_size=args.batch_size,
            )
        paths = save_action_report(args.output_dir, action_name, result)
        print(render_terminal_table([build_workspace_row(result)]))
        print(
            json.dumps(
                {
                    "status": result.get("status", result.get("repair_gate")),
                    "reports": [str(path) for path in paths],
                },
                ensure_ascii=False,
            )
        )
        status = str(result.get("status") or result.get("repair_gate") or "")
        return (
            0
            if status.startswith(("PASSED", "READY", "AWAITING", "EMPTY"))
            else 2
        )
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
