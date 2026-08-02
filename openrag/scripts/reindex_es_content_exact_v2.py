"""Rebuild one workspace's strict A02 v2 Elasticsearch chunk index."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from openrag.database import SessionLocal, get_engine
from openrag.models.document_chunk import DocumentChunk
from openrag.models.file import File
from openrag.models.workspace import Workspace
from openrag.search.es_chunk_contract import (
    SCHEMA_VERSION,
    build_chunk_mapping,
    build_chunk_search_document,
    compute_mapping_hash,
)
from openrag.search.es_chunk_store import create_es_chunk_store_from_config
from openrag.search.workspace_es_slug import (
    build_workspace_chunks_physical_index_name,
    build_workspace_chunks_read_alias,
    build_workspace_chunks_write_alias,
)
from openrag.storage.minio_storage import MinioStorage


def _active_chunk_rows(
    db: Session, workspace_id: int
) -> list[tuple[DocumentChunk, File, Workspace]]:
    workspace = db.get(Workspace, workspace_id)
    if workspace is None:
        raise ValueError(f"Workspace not found: {workspace_id}")
    ownership_mismatch_count = db.execute(
        select(func.count(DocumentChunk.id))
        .join(File, File.id == DocumentChunk.file_id)
        .where(
            File.deleted_at.is_(None),
            or_(
                and_(
                    DocumentChunk.workspace_id == workspace_id,
                    File.workspace_id != workspace_id,
                ),
                and_(
                    File.workspace_id == workspace_id,
                    DocumentChunk.workspace_id != workspace_id,
                ),
            ),
        )
    ).scalar_one()
    if ownership_mismatch_count:
        raise RuntimeError(
            "DB chunk workspace ownership mismatch; refusing v2 migration: "
            f"{ownership_mismatch_count} chunks"
        )
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
    return [(chunk, file_row, workspace) for chunk, file_row in rows]


def load_authoritative_chunk_text(
    *,
    chunk: DocumentChunk,
    workspace_slug: str,
    storage,
) -> str | None:
    """Read full L2 text without falling back to the possibly truncated preview."""
    if chunk.local_chunk_path:
        local_path = Path(chunk.local_chunk_path)
        if local_path.is_file():
            try:
                return local_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                pass
    if chunk.object_key:
        try:
            text = storage.get_object_text(workspace_slug, chunk.object_key)
        except Exception:
            return None
        return text if isinstance(text, str) else None
    return None


def _scan_rows(
    *,
    db: Session,
    storage,
    workspace_id: int,
    sample_limit: int = 20,
) -> tuple[dict[str, Any], list[tuple[DocumentChunk, File, Workspace, str]]]:
    loaded: list[tuple[DocumentChunk, File, Workspace, str]] = []
    missing_ids: list[str] = []
    rows = _active_chunk_rows(db, workspace_id)
    workspace = rows[0][2] if rows else db.get(Workspace, workspace_id)
    if workspace is None:
        raise ValueError(f"Workspace not found: {workspace_id}")

    for chunk, file_row, current_workspace in rows:
        text = load_authoritative_chunk_text(
            chunk=chunk,
            workspace_slug=current_workspace.slug,
            storage=storage,
        )
        if text is None:
            if len(missing_ids) < sample_limit:
                missing_ids.append(chunk.chunk_id)
            continue
        loaded.append((chunk, file_row, current_workspace, text))

    report = {
        "workspace_id": int(workspace_id),
        "workspace_slug": workspace.slug,
        "v2_index_name": build_workspace_chunks_physical_index_name(
            workspace.slug, workspace.id
        ),
        "read_alias": build_workspace_chunks_read_alias(
            workspace.slug, workspace.id
        ),
        "write_alias": build_workspace_chunks_write_alias(
            workspace.slug, workspace.id
        ),
        "db_active_chunk_count": len(rows),
        "readable_l2_count": len(loaded),
        "missing_l2_count": len(rows) - len(loaded),
        "missing_l2_chunk_ids": missing_ids,
        "expected_v2_document_count": len(rows),
        "dry_run": True,
    }
    return report, loaded


def scan_workspace(
    *,
    db: Session,
    storage,
    workspace_id: int,
    sample_limit: int = 20,
) -> dict[str, Any]:
    """Return the non-mutating L2 readiness report for one workspace."""
    report, _loaded = _scan_rows(
        db=db,
        storage=storage,
        workspace_id=workspace_id,
        sample_limit=sample_limit,
    )
    return report


def _batches(values: list[Any], size: int) -> Iterator[list[Any]]:
    if size <= 0:
        raise ValueError("batch_size must be positive")
    for start in range(0, len(values), size):
        yield values[start : start + size]


def reindex_workspace(
    *,
    db: Session,
    store,
    storage,
    workspace_id: int,
    apply: bool = False,
    batch_size: int = 500,
) -> dict[str, Any]:
    """Dry-run or idempotently rebuild one workspace's v2 physical index."""
    report, loaded = _scan_rows(
        db=db,
        storage=storage,
        workspace_id=workspace_id,
    )
    if not apply:
        return report
    if report["missing_l2_count"]:
        raise RuntimeError(
            "L2 source missing; refusing v2 reindex: "
            f"{report['missing_l2_count']} chunks"
        )

    index_name = report["v2_index_name"]
    store.ensure_versioned_index(index_name)
    written_count = 0
    for batch in _batches(loaded, batch_size):
        documents = [
            build_chunk_search_document(
                chunk_id=chunk.chunk_id,
                file_id=file_row.id,
                workspace_id=workspace.id,
                workspace_slug=workspace.slug,
                content=text,
                metadata={
                    "doc_type_kwd": chunk.block_type,
                    "mom_with_weight": "",
                },
            )
            for chunk, file_row, workspace, text in batch
        ]
        accepted = store.bulk_upsert_chunks(index_name, documents)
        if accepted != len(documents):
            raise RuntimeError(
                "Elasticsearch partial bulk failure; refusing alias switch: "
                f"accepted={accepted}, expected={len(documents)}"
            )
        written_count += accepted

    report.update(dry_run=False, written_count=written_count)
    return report


def validate_workspace_v2(
    *,
    db: Session,
    store,
    workspace_id: int,
    index_name: str | None = None,
    sample_limit: int = 20,
) -> dict[str, Any]:
    """Apply every hard v2 pre-switch consistency check."""
    rows = _active_chunk_rows(db, workspace_id)
    workspace = rows[0][2] if rows else db.get(Workspace, workspace_id)
    if workspace is None:
        raise ValueError(f"Workspace not found: {workspace_id}")
    target = index_name or build_workspace_chunks_physical_index_name(
        workspace.slug, workspace.id
    )
    mapping_contract_mismatch_count = 0
    try:
        store.validate_index_contract(target)
    except Exception:
        mapping_contract_mismatch_count = 1

    expected = {
        chunk.chunk_id: (file_row.id, workspace.id)
        for chunk, file_row, workspace in rows
    }
    actual: dict[str, dict[str, Any]] = {}
    duplicate_chunk_ids: list[str] = []
    for source in store.iter_chunk_sources(target):
        chunk_id = str(source.get("chunk_id") or "")
        if chunk_id in actual and len(duplicate_chunk_ids) < sample_limit:
            duplicate_chunk_ids.append(chunk_id)
        actual[chunk_id] = source

    expected_ids = set(expected)
    actual_ids = set(actual)
    missing_ids = sorted(expected_ids - actual_ids)
    orphan_ids = sorted(actual_ids - expected_ids)
    ownership_mismatches: list[str] = []
    schema_mismatches: list[str] = []
    mapping_hash_mismatches: list[str] = []
    missing_required_fields: list[str] = []
    forbidden_fields: list[str] = []
    mapping = build_chunk_mapping()
    allowed_fields = set(mapping["properties"])
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
    expected_hash = compute_mapping_hash()
    for chunk_id, source in actual.items():
        if chunk_id in expected:
            expected_file_id, expected_workspace_id = expected[chunk_id]
            if (
                source.get("file_id") != expected_file_id
                or source.get("workspace_id") != expected_workspace_id
            ):
                ownership_mismatches.append(chunk_id)
        if source.get("schema_version") != SCHEMA_VERSION:
            schema_mismatches.append(chunk_id)
        if source.get("mapping_hash") != expected_hash:
            mapping_hash_mismatches.append(chunk_id)
        if required_fields - set(source):
            missing_required_fields.append(chunk_id)
        unknown = set(source) - allowed_fields
        if unknown or any("token" in field.lower() for field in source):
            forbidden_fields.append(chunk_id)

    counts = {
        "mapping_contract_mismatch_count": mapping_contract_mismatch_count,
        "missing_es_chunk_count": len(missing_ids),
        "orphan_chunk_count": len(orphan_ids),
        "duplicate_chunk_count": len(duplicate_chunk_ids),
        "ownership_mismatch_count": len(ownership_mismatches),
        "schema_version_mismatch_count": len(schema_mismatches),
        "mapping_hash_mismatch_count": len(mapping_hash_mismatches),
        "missing_required_field_count": len(missing_required_fields),
        "forbidden_field_count": len(forbidden_fields),
    }
    return {
        "workspace_id": int(workspace_id),
        "index_name": target,
        "db_active_chunk_count": len(expected),
        "es_doc_count": len(actual),
        **counts,
        "missing_es_chunk_ids": missing_ids[:sample_limit],
        "orphan_chunk_ids": orphan_ids[:sample_limit],
        "duplicate_chunk_ids": duplicate_chunk_ids[:sample_limit],
        "ownership_mismatch_chunk_ids": ownership_mismatches[:sample_limit],
        "schema_version_mismatch_chunk_ids": schema_mismatches[:sample_limit],
        "mapping_hash_mismatch_chunk_ids": mapping_hash_mismatches[:sample_limit],
        "missing_required_field_chunk_ids": missing_required_fields[:sample_limit],
        "forbidden_field_chunk_ids": forbidden_fields[:sample_limit],
        "anomaly_count": sum(counts.values()),
    }


def switch_workspace_aliases(
    *,
    db: Session,
    store,
    storage,
    workspace_id: int,
    read_alias: str | None = None,
    write_alias: str | None = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Validate, atomically switch, smoke-test, and auto-restore on failure."""
    rows = _active_chunk_rows(db, workspace_id)
    workspace = rows[0][2] if rows else db.get(Workspace, workspace_id)
    if workspace is None:
        raise ValueError(f"Workspace not found: {workspace_id}")
    index_name = build_workspace_chunks_physical_index_name(
        workspace.slug, workspace.id
    )
    validation = validate_workspace_v2(
        db=db,
        store=store,
        workspace_id=workspace_id,
        index_name=index_name,
    )
    if validation["anomaly_count"]:
        raise RuntimeError(
            "v2 audit failed; refusing alias switch: "
            f"anomalies={validation['anomaly_count']}"
        )

    target_read_alias = read_alias or build_workspace_chunks_read_alias(
        workspace.slug, workspace.id
    )
    target_write_alias = write_alias or build_workspace_chunks_write_alias(
        workspace.slug, workspace.id
    )
    previous = store.update_workspace_aliases(
        read_alias=target_read_alias,
        write_alias=target_write_alias,
        target_index=index_name,
    )
    try:
        smoke_hit = (
            _run_smoke_query(
                rows=rows,
                store=store,
                storage=storage,
                read_alias=target_read_alias,
                chunk_index_mode="v2_alias",
            )
            if rows
            else True
        )
        if not smoke_hit:
            raise RuntimeError("v2 alias smoke query returned no authorized hit")
    except Exception:
        store.restore_workspace_aliases(previous)
        raise
    return previous


def restore_workspace_aliases(
    *,
    db: Session,
    store,
    storage,
    workspace_id: int,
    state: dict[str, dict[str, dict[str, Any]]],
    read_alias: str | None = None,
    write_alias: str | None = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Atomically restore saved aliases and prove the restored read path works."""
    rows = _active_chunk_rows(db, workspace_id)
    workspace = rows[0][2] if rows else db.get(Workspace, workspace_id)
    if workspace is None:
        raise ValueError(f"Workspace not found: {workspace_id}")
    target_read_alias = read_alias or build_workspace_chunks_read_alias(
        workspace.slug, workspace.id
    )
    target_write_alias = write_alias or build_workspace_chunks_write_alias(
        workspace.slug, workspace.id
    )
    expected_aliases = {target_read_alias, target_write_alias}
    if set(state) != expected_aliases:
        raise RuntimeError(
            "Saved alias state does not belong to the requested workspace"
        )

    current = store.get_alias_state(sorted(expected_aliases))
    store.restore_workspace_aliases(state)
    try:
        smoke_hit = (
            _run_smoke_query(
                rows=rows,
                store=store,
                storage=storage,
                read_alias=target_read_alias,
                chunk_index_mode="legacy",
            )
            if rows
            else True
        )
        if not smoke_hit:
            raise RuntimeError("restored alias smoke query returned no authorized hit")
    except Exception:
        store.restore_workspace_aliases(current)
        raise
    return current


def _run_smoke_query(
    *,
    rows,
    store,
    storage,
    read_alias: str,
    chunk_index_mode: str,
) -> bool:
    for chunk, file_row, workspace in rows:
        text = load_authoritative_chunk_text(
            chunk=chunk,
            workspace_slug=workspace.slug,
            storage=storage,
        )
        if not text or not text.strip():
            continue
        hits = store.search_chunks(
            index_names=[read_alias],
            query_text=text.strip()[:200],
            file_ids=[file_row.id],
            top_k=1,
            chunk_index_mode=chunk_index_mode,
        )
        return bool(hits)
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dry-run, rebuild, validate, or switch one A02 v2 workspace index"
    )
    parser.add_argument("--workspace-id", type=int, required=True)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--dry-run", action="store_true")
    action.add_argument("--apply", action="store_true")
    action.add_argument("--switch-aliases", action="store_true")
    action.add_argument("--restore-aliases-from", type=Path)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    SessionLocal.configure(bind=get_engine())
    db = SessionLocal()
    try:
        store = create_es_chunk_store_from_config()
        storage = MinioStorage()
        if args.apply or args.switch_aliases or args.restore_aliases_from:
            if store is None:
                raise RuntimeError("Elasticsearch is disabled or unavailable")
        if args.restore_aliases_from:
            saved = json.loads(
                args.restore_aliases_from.read_text(encoding="utf-8")
            )
            state = saved.get("previous_alias_state", saved)
            current = restore_workspace_aliases(
                db=db,
                store=store,
                storage=storage,
                workspace_id=args.workspace_id,
                state=state,
            )
            report = {
                "workspace_id": args.workspace_id,
                "aliases_restored": True,
                "replaced_alias_state": current,
            }
        elif args.switch_aliases:
            previous = switch_workspace_aliases(
                db=db,
                store=store,
                storage=storage,
                workspace_id=args.workspace_id,
            )
            report: dict[str, Any] = {
                "workspace_id": args.workspace_id,
                "aliases_switched": True,
                "previous_alias_state": previous,
                "validation": validate_workspace_v2(
                    db=db,
                    store=store,
                    workspace_id=args.workspace_id,
                ),
            }
        elif args.apply:
            report = reindex_workspace(
                db=db,
                store=store,
                storage=storage,
                workspace_id=args.workspace_id,
                apply=True,
                batch_size=args.batch_size,
            )
            report["validation"] = validate_workspace_v2(
                db=db,
                store=store,
                workspace_id=args.workspace_id,
            )
        else:
            report = scan_workspace(
                db=db,
                storage=storage,
                workspace_id=args.workspace_id,
            )

        output = json.dumps(report, ensure_ascii=False, indent=2)
        if args.output:
            args.output.write_text(output, encoding="utf-8")
        print(output)
        validation = report.get("validation")
        return 0 if not validation or validation["anomaly_count"] == 0 else 2
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
