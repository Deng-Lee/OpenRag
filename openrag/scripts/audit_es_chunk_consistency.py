"""Read-only consistency audit for a workspace Elasticsearch chunk index."""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from openrag.database import SessionLocal, get_engine
from openrag.models.document_chunk import DocumentChunk
from openrag.models.file import File
from openrag.models.workspace import Workspace
from openrag.search.es_chunk_store import (
    _LEGACY_CHUNK_MAPPINGS,
    create_es_chunk_store_from_config,
)
from openrag.search.es_chunk_contract import (
    SCHEMA_VERSION,
    build_chunk_mapping,
    compute_mapping_hash,
)
from openrag.search.workspace_es_slug import (
    build_workspace_chunks_index_name,
    build_workspace_chunks_physical_index_name,
    build_workspace_chunks_read_alias,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def inspect_legacy_mapping(
    client, *, index_name: str, active_chunk_count: int
) -> dict[str, Any]:
    """Inspect the legacy physical-index contract without mutating ES."""
    result: dict[str, Any] = {
        "index_name": index_name,
        "expected_mapping": copy.deepcopy(_LEGACY_CHUNK_MAPPINGS),
        "resource_type": "unknown",
        "status": "FAILED",
        "compatible": False,
        "mismatches": [],
        "extra_fields": [],
        "raw_mapping": None,
        "raw_settings": None,
        "analyze_probe_tokens": [],
    }
    try:
        if bool(client.indices.exists_alias(name=index_name)):
            result.update(resource_type="alias", status="ALIAS_CONFLICT")
            result["mismatches"].append(
                "expected legacy physical index name is occupied by an alias"
            )
            return result
        if not bool(client.indices.exists(index=index_name)):
            result.update(
                resource_type="absent",
                status=(
                    "INDEX_ABSENT"
                    if active_chunk_count
                    else "EMPTY_INDEX_NOT_REQUIRED"
                ),
            )
            return result

        mapping_response = dict(client.indices.get_mapping(index=index_name))
        settings_response = dict(
            client.indices.get_settings(index=index_name, flat_settings=True)
        )
        result.update(
            resource_type="physical_index",
            raw_mapping=mapping_response,
            raw_settings=settings_response,
        )
        if set(mapping_response) != {index_name}:
            result["status"] = "RESOURCE_TARGET_MISMATCH"
            result["mismatches"].append(
                f"expected physical index {index_name}, got {sorted(mapping_response)}"
            )
            return result

        expected_properties = _LEGACY_CHUNK_MAPPINGS["properties"]
        actual_mapping = mapping_response[index_name].get("mappings") or {}
        actual_properties = actual_mapping.get("properties") or {}
        for field, expected in expected_properties.items():
            actual = actual_properties.get(field)
            if actual is None:
                result["mismatches"].append(
                    f"missing required field mapping: {field}"
                )
            elif actual.get("type") != expected.get("type"):
                result["mismatches"].append(
                    f"field {field} type={actual.get('type')!r}, "
                    f"expected={expected.get('type')!r}"
                )
        result["extra_fields"] = sorted(
            set(actual_properties) - set(expected_properties)
        )

        content_mapping = actual_properties.get("content") or {}
        field_analyzer = content_mapping.get("analyzer")
        search_analyzer = content_mapping.get("search_analyzer")
        flat_settings = (
            (settings_response.get(index_name) or {}).get("settings") or {}
        )
        default_analyzer_settings = {
            key: value
            for key, value in flat_settings.items()
            if key.startswith("index.analysis.analyzer.default")
        }
        if field_analyzer not in (None, "standard"):
            result["mismatches"].append(
                f"content analyzer={field_analyzer!r}, expected='standard'"
            )
        if search_analyzer not in (None, "standard"):
            result["mismatches"].append(
                f"content search_analyzer={search_analyzer!r}, expected='standard'"
            )
        if field_analyzer is None and default_analyzer_settings not in (
            {},
            {"index.analysis.analyzer.default.type": "standard"},
        ):
            result["mismatches"].append(
                "content uses a customized index default analyzer: "
                + json.dumps(
                    default_analyzer_settings,
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )

        try:
            probe = client.indices.analyze(
                index=index_name,
                field="content",
                text="OpenRag Alpha-123 中国",
            )
            result["analyze_probe_tokens"] = [
                str(token.get("token")) for token in probe.get("tokens", [])
            ]
        except Exception as exc:
            result["mismatches"].append(
                f"content analyzer probe failed: {type(exc).__name__}: {exc}"
            )

        if result["mismatches"]:
            result["status"] = "MAPPING_INCOMPATIBLE"
        else:
            result.update(status="MAPPING_COMPATIBLE", compatible=True)
        return result
    except Exception as exc:
        result["status"] = "ES_PREFLIGHT_ERROR"
        result["mismatches"].append(f"{type(exc).__name__}: {exc}")
        return result


def _append_sample(report: dict[str, Any], key: str, value: str, limit: int) -> None:
    samples = report[key]
    if len(samples) < limit and value not in samples:
        samples.append(value)


def _iter_es_hits(
    es_client,
    index_name: str,
    batch_size: int,
    *,
    include_v2_contract: bool,
) -> Iterator[dict]:
    response = es_client.search(
        index=index_name,
        size=batch_size,
        scroll="1m",
        query={"match_all": {}},
        source=(
            True
            if include_v2_contract
            else ["chunk_id", "file_id", "workspace_id"]
        ),
        sort=["_doc"],
    )
    scroll_id = response.get("_scroll_id")
    try:
        while True:
            hits = response.get("hits", {}).get("hits", [])
            if not hits:
                break
            yield from hits
            response = es_client.scroll(scroll_id=scroll_id, scroll="1m")
            scroll_id = response.get("_scroll_id", scroll_id)
    finally:
        if scroll_id:
            es_client.clear_scroll(scroll_id=scroll_id)


def _iter_batches(values: Iterator[dict], batch_size: int) -> Iterator[list[dict]]:
    batch: list[dict] = []
    for value in values:
        batch.append(value)
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def audit_workspace(
    *,
    db: Session,
    es_client,
    index_name: str,
    workspace_id: int,
    batch_size: int = 500,
    sample_limit: int = 20,
    index_version: str = "legacy",
    environment_label: str | None = None,
) -> dict[str, Any]:
    """Compare ES and active DB chunks without mutating either system."""
    if index_version not in {"legacy", "v2", "alias"}:
        raise ValueError(f"Unsupported index version: {index_version}")
    audit_v2_contract = index_version in {"v2", "alias"}
    workspace = db.get(Workspace, workspace_id)
    report: dict[str, Any] = {
        "format_version": 1,
        "checked_at": _utc_now(),
        "environment_label": environment_label,
        "workspace_id": int(workspace_id),
        "workspace_name": workspace.name if workspace is not None else None,
        "workspace_slug": workspace.slug if workspace is not None else None,
        "index_name": index_name,
        "dry_run": True,
        "index_exists": True,
        "es_doc_count": 0,
        "db_active_chunk_count": 0,
        "orphan_chunk_count": 0,
        "missing_es_chunk_count": 0,
        "file_id_mismatch_count": 0,
        "deleted_file_chunk_count": 0,
        "orphan_chunk_ids": [],
        "missing_es_chunk_ids": [],
        "file_id_mismatches": [],
        "deleted_file_chunk_ids": [],
        "index_version": index_version,
        "physical_index_names": [],
        "mapping_contract_mismatch_count": 0,
        "alias_target_mismatch_count": 0,
        "schema_version_mismatch_count": 0,
        "mapping_hash_mismatch_count": 0,
        "missing_required_field_count": 0,
        "forbidden_field_count": 0,
        "schema_version_mismatch_chunk_ids": [],
        "mapping_hash_mismatch_chunk_ids": [],
        "missing_required_field_chunk_ids": [],
        "forbidden_field_chunk_ids": [],
        "required_field_coverage": 1.0,
        "mapping_preflight": None,
        "legacy_mapping_mismatch_count": 0,
    }

    indices = getattr(es_client, "indices", None)
    if indices is not None:
        report["index_exists"] = bool(indices.exists(index=index_name))
        if report["index_exists"] and audit_v2_contract:
            try:
                mapping_response = indices.get_mapping(index=index_name)
                report["physical_index_names"] = sorted(mapping_response)
                if (
                    index_version == "alias"
                    and len(report["physical_index_names"]) != 1
                ):
                    report["alias_target_mismatch_count"] = 1
                actual_mapping = next(iter(mapping_response.values())).get(
                    "mappings"
                )
                if _normalize_mapping_from_es(actual_mapping) != build_chunk_mapping():
                    report["mapping_contract_mismatch_count"] = 1
            except Exception:
                report["mapping_contract_mismatch_count"] = 1
        elif audit_v2_contract:
            report["mapping_contract_mismatch_count"] = 1

    es_hits = (
        _iter_es_hits(
            es_client,
            index_name,
            batch_size,
            include_v2_contract=audit_v2_contract,
        )
        if report["index_exists"]
        else iter(())
    )
    for hits in _iter_batches(es_hits, batch_size):
        report["es_doc_count"] += len(hits)
        chunk_ids = [str(hit.get("_id")) for hit in hits if hit.get("_id") is not None]
        chunks = {
            row.chunk_id: row
            for row in db.query(DocumentChunk)
            .filter(DocumentChunk.chunk_id.in_(chunk_ids))
            .all()
        }
        file_ids = {row.file_id for row in chunks.values()}
        files = {
            row.id: row
            for row in db.query(File).filter(File.id.in_(file_ids)).all()
        }
        for hit in hits:
            chunk_id = str(hit.get("_id") or "")
            source = hit.get("_source") or {}
            if audit_v2_contract:
                _audit_v2_source(
                    report=report,
                    source=source,
                    chunk_id=chunk_id,
                    sample_limit=sample_limit,
                )
            chunk = chunks.get(chunk_id)
            if chunk is None:
                report["orphan_chunk_count"] += 1
                _append_sample(report, "orphan_chunk_ids", chunk_id, sample_limit)
                continue
            file_row = files.get(chunk.file_id)
            if file_row is None:
                report["orphan_chunk_count"] += 1
                _append_sample(report, "orphan_chunk_ids", chunk_id, sample_limit)
                continue
            if file_row.deleted_at is not None:
                report["deleted_file_chunk_count"] += 1
                _append_sample(
                    report, "deleted_file_chunk_ids", chunk_id, sample_limit
                )
                continue
            source_file_id = source.get("file_id")
            source_workspace_id = source.get("workspace_id")
            mismatched = (
                chunk.workspace_id != workspace_id
                or file_row.workspace_id != workspace_id
                or source_file_id is None
                or int(source_file_id) != chunk.file_id
                or source_workspace_id is None
                or int(source_workspace_id) != workspace_id
            )
            if mismatched:
                report["file_id_mismatch_count"] += 1
                _append_sample(report, "file_id_mismatches", chunk_id, sample_limit)

    last_id = 0
    while True:
        rows = db.execute(
            select(DocumentChunk.id, DocumentChunk.chunk_id)
            .join(File, File.id == DocumentChunk.file_id)
            .where(
                DocumentChunk.id > last_id,
                DocumentChunk.workspace_id == workspace_id,
                File.workspace_id == workspace_id,
                File.deleted_at.is_(None),
            )
            .order_by(DocumentChunk.id)
            .limit(batch_size)
        ).all()
        if not rows:
            break
        report["db_active_chunk_count"] += len(rows)
        ids = [str(row.chunk_id) for row in rows]
        response = (
            es_client.mget(index=index_name, ids=ids, source=False)
            if report["index_exists"]
            else {"docs": [{"_id": chunk_id, "found": False} for chunk_id in ids]}
        )
        for doc in response.get("docs", []):
            if not doc.get("found"):
                chunk_id = str(doc.get("_id") or "")
                report["missing_es_chunk_count"] += 1
                _append_sample(
                    report, "missing_es_chunk_ids", chunk_id, sample_limit
                )
        last_id = int(rows[-1].id)

    if audit_v2_contract and report["es_doc_count"]:
        report["required_field_coverage"] = (
            report["es_doc_count"] - report["missing_required_field_count"]
        ) / report["es_doc_count"]

    if index_version == "legacy":
        report["mapping_preflight"] = inspect_legacy_mapping(
            es_client,
            index_name=index_name,
            active_chunk_count=report["db_active_chunk_count"],
        )
        mapping_status = report["mapping_preflight"]["status"]
        if mapping_status not in {
            "MAPPING_COMPATIBLE",
            "INDEX_ABSENT",
            "EMPTY_INDEX_NOT_REQUIRED",
        }:
            report["legacy_mapping_mismatch_count"] = 1

    anomaly_keys = [
        "orphan_chunk_count",
        "missing_es_chunk_count",
        "file_id_mismatch_count",
        "deleted_file_chunk_count",
    ]
    if audit_v2_contract:
        anomaly_keys.extend(
            [
                "mapping_contract_mismatch_count",
                "alias_target_mismatch_count",
                "schema_version_mismatch_count",
                "mapping_hash_mismatch_count",
                "missing_required_field_count",
                "forbidden_field_count",
            ]
        )
    else:
        anomaly_keys.append("legacy_mapping_mismatch_count")
    report["anomaly_count"] = sum(
        report[key]
        for key in anomaly_keys
    )
    return report


def _audit_v2_source(
    *,
    report: dict[str, Any],
    source: dict[str, Any],
    chunk_id: str,
    sample_limit: int,
) -> None:
    allowed_fields = set(build_chunk_mapping()["properties"])
    required_fields = {
        "chunk_id",
        "file_id",
        "workspace_id",
        "workspace_slug",
        "content",
        "doc_type_kwd",
        "schema_version",
        "mapping_hash",
    }
    if source.get("schema_version") != SCHEMA_VERSION:
        report["schema_version_mismatch_count"] += 1
        _append_sample(
            report,
            "schema_version_mismatch_chunk_ids",
            chunk_id,
            sample_limit,
        )
    if source.get("mapping_hash") != compute_mapping_hash():
        report["mapping_hash_mismatch_count"] += 1
        _append_sample(
            report,
            "mapping_hash_mismatch_chunk_ids",
            chunk_id,
            sample_limit,
        )
    if required_fields - set(source):
        report["missing_required_field_count"] += 1
        _append_sample(
            report,
            "missing_required_field_chunk_ids",
            chunk_id,
            sample_limit,
        )
    unknown_fields = set(source) - allowed_fields
    has_token_field = any("token" in field.lower() for field in source)
    if unknown_fields or has_token_field:
        report["forbidden_field_count"] += 1
        _append_sample(
            report,
            "forbidden_field_chunk_ids",
            chunk_id,
            sample_limit,
        )


def _normalize_mapping_from_es(mapping: Any) -> Any:
    if not isinstance(mapping, dict):
        return mapping
    normalized = copy.deepcopy(mapping)
    for field in normalized.get("properties", {}).values():
        if (
            isinstance(field, dict)
            and field.get("type") == "text"
            and "analyzer" in field
            and "search_analyzer" not in field
        ):
            field["search_analyzer"] = field["analyzer"]
    return normalized


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only audit of Elasticsearch chunks against PostgreSQL"
    )
    parser.add_argument("--workspace-id", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--environment-label")
    parser.add_argument(
        "--index-version",
        choices=("legacy", "v2", "alias"),
        default="legacy",
    )
    args = parser.parse_args(argv)

    SessionLocal.configure(bind=get_engine())
    db = SessionLocal()
    try:
        workspace = db.get(Workspace, args.workspace_id)
        if workspace is None:
            raise ValueError(f"Workspace not found: {args.workspace_id}")
        store = create_es_chunk_store_from_config()
        if store is None:
            raise RuntimeError("Elasticsearch is disabled or unavailable")
        if args.index_version == "legacy":
            index_name = build_workspace_chunks_index_name(
                workspace.slug, workspace.id
            )
        elif args.index_version == "v2":
            index_name = build_workspace_chunks_physical_index_name(
                workspace.slug, workspace.id
            )
        else:
            index_name = build_workspace_chunks_read_alias(
                workspace.slug, workspace.id
            )
        report = audit_workspace(
            db=db,
            es_client=store.client,
            index_name=index_name,
            workspace_id=workspace.id,
            batch_size=args.batch_size,
            index_version=args.index_version,
            environment_label=args.environment_label,
        )
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["anomaly_count"] == 0 else 2
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
