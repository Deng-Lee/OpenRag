"""Stage-oriented legacy Elasticsearch maintenance for one deployment."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
from typing import Any

from sqlalchemy import select

from openrag.database import SessionLocal, get_engine
from openrag.models.workspace import Workspace
from openrag.search.es_chunk_store import create_es_chunk_store_from_config
from openrag.search.workspace_es_slug import build_workspace_chunks_index_name
from openrag.storage.minio_storage import MinioStorage

if __package__:
    from scripts.audit_es_chunk_consistency import audit_workspace
    from scripts.legacy_es_report import render_terminal_table, write_run_reports
    from scripts.repair_legacy_es_chunks import (
        build_missing_repair_plan,
        build_orphan_delete_manifest,
        delete_approved_orphans,
        preflight_workspace,
        repair_approved_missing_chunks,
        save_action_report,
    )
else:
    from audit_es_chunk_consistency import audit_workspace
    from legacy_es_report import render_terminal_table, write_run_reports
    from repair_legacy_es_chunks import (
        build_missing_repair_plan,
        build_orphan_delete_manifest,
        delete_approved_orphans,
        preflight_workspace,
        repair_approved_missing_chunks,
        save_action_report,
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _safe_segment(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in value)
    return safe.strip("-") or "workspace"


def _run_dir(output_root: Path, run_id: str) -> Path:
    return output_root / run_id


def _workspace_dir(run_dir: Path, workspace_id: int, slug: str) -> Path:
    return run_dir / "workspaces" / f"{workspace_id:06d}-{_safe_segment(slug)}"


def _find_workspace_dir(run_dir: Path, workspace_id: int) -> Path:
    matches = list((run_dir / "workspaces").glob(f"{workspace_id:06d}-*"))
    if len(matches) != 1:
        raise ValueError(
            f"Expected one report directory for Workspace {workspace_id}, found {len(matches)}"
        )
    return matches[0]


def _write_json(path: Path, value: dict[str, Any]) -> None:
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite evidence: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _environment_record(environment_label: str) -> dict[str, Any]:
    return {
        "format_version": 1,
        "environment_label": environment_label,
        "hostname": platform.node(),
        "started_at": _utc_now(),
        "hybrid_recall_mode": os.environ.get(
            "ELASTICSEARCH__HYBRID_RECALL_MODE", "legacy"
        ),
        "chunk_index_mode": os.environ.get(
            "ELASTICSEARCH__CHUNK_INDEX_MODE", "legacy"
        ),
    }


def _runtime():
    SessionLocal.configure(bind=get_engine())
    db = SessionLocal()
    try:
        store = create_es_chunk_store_from_config()
        if store is None or not store.ping():
            raise RuntimeError("Elasticsearch is disabled or unavailable")
        return db, store, MinioStorage()
    except Exception:
        db.close()
        raise


def _annotate(result: dict[str, Any], environment_label: str) -> dict[str, Any]:
    result["format_version"] = 1
    result["environment_label"] = environment_label
    return result


def _refresh_reports(run_dir: Path, environment_label: str) -> list[dict[str, Any]]:
    rows = write_run_reports(run_dir, environment_label=environment_label)
    print(render_terminal_table(rows))
    return rows


def audit_all(args) -> int:
    run_id = args.run_id or _default_run_id()
    run_dir = _run_dir(args.output_root, run_id)
    if run_dir.exists():
        raise FileExistsError(f"Run already exists: {run_dir}")
    (run_dir / "workspaces").mkdir(parents=True)
    _write_json(run_dir / "environment.json", _environment_record(args.environment_label))
    db, store, storage = _runtime()
    try:
        query = select(Workspace).order_by(Workspace.id)
        if args.workspace_id:
            query = query.where(Workspace.id.in_(sorted(set(args.workspace_id))))
        workspaces = list(db.execute(query).scalars())
        for workspace in workspaces:
            workspace_dir = _workspace_dir(run_dir, workspace.id, workspace.slug)
            workspace_dir.mkdir(parents=True)
            try:
                result = preflight_workspace(
                    db=db,
                    store=store,
                    storage=storage,
                    workspace_id=workspace.id,
                    batch_size=args.batch_size,
                )
                _write_json(
                    workspace_dir / "01-preflight.json",
                    _annotate(result, args.environment_label),
                )
            except Exception as exc:
                _write_json(
                    workspace_dir / "01-preflight-error.json",
                    {
                        "format_version": 1,
                        "environment_label": args.environment_label,
                        "status": "FAILED_WORKSPACE_AUDIT",
                        "workspace_id": workspace.id,
                        "workspace_name": workspace.name,
                        "workspace_slug": workspace.slug,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
            write_run_reports(run_dir, environment_label=args.environment_label)
    finally:
        db.close()
    rows = _refresh_reports(run_dir, args.environment_label)
    print(json.dumps({"run_id": run_id, "run_dir": str(run_dir)}, ensure_ascii=False))
    return 0 if all(row["next_action"] == "NONE" for row in rows) else 2


def _load_environment(run_dir: Path, expected_label: str) -> dict[str, Any]:
    path = run_dir / "environment.json"
    if not path.exists():
        raise FileNotFoundError(path)
    environment = json.loads(path.read_text(encoding="utf-8"))
    if environment.get("environment_label") != expected_label:
        raise ValueError(
            "Environment label mismatch: "
            f"report={environment.get('environment_label')!r}, "
            f"current={expected_label!r}"
        )
    return environment


def _single_workspace_action(args, action: str) -> int:
    if not args.run_id:
        raise ValueError("--run-id is required for this action")
    run_dir = _run_dir(args.output_root, args.run_id)
    _load_environment(run_dir, args.environment_label)
    workspace_dir = _find_workspace_dir(run_dir, args.workspace_id)
    action_name = {
        "prepare-missing": "prepare-missing",
        "apply-missing": "apply-missing",
        "prepare-orphans": "prepare-orphan-delete",
        "delete-orphans": "apply-orphan-delete",
    }[action]
    db = None
    try:
        db, store, storage = _runtime()
        if action == "prepare-missing":
            result = build_missing_repair_plan(
                db=db,
                store=store,
                storage=storage,
                workspace_id=args.workspace_id,
                batch_size=args.batch_size,
            )
        elif action == "apply-missing":
            plan_path = workspace_dir / "02-missing-plan.json"
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            result = repair_approved_missing_chunks(
                db=db,
                store=store,
                storage=storage,
                workspace_id=args.workspace_id,
                plan=plan,
                approval_sha256=args.approve_sha256,
                allow_create_missing_index=args.allow_create_missing_index,
                batch_size=args.batch_size,
            )
        elif action == "prepare-orphans":
            result = build_orphan_delete_manifest(
                db=db,
                store=store,
                workspace_id=args.workspace_id,
                batch_size=args.batch_size,
            )
        else:
            manifest_path = workspace_dir / "05-orphan-delete-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            result = delete_approved_orphans(
                db=db,
                store=store,
                workspace_id=args.workspace_id,
                manifest=manifest,
                approval_sha256=args.approve_sha256,
                batch_size=args.batch_size,
            )
    except Exception as exc:
        preflight_path = workspace_dir / "01-preflight.json"
        preflight = (
            json.loads(preflight_path.read_text(encoding="utf-8"))
            if preflight_path.exists()
            else {}
        )
        result = {
            "status": f"FAILED_{action.replace('-', '_').upper()}",
            "workspace_id": args.workspace_id,
            "workspace_name": preflight.get("workspace_name"),
            "workspace_slug": preflight.get("workspace_slug"),
            "preflight": preflight,
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        if db is not None:
            db.close()
    save_action_report(
        workspace_dir,
        action_name,
        _annotate(result, args.environment_label),
    )
    rows = _refresh_reports(run_dir, args.environment_label)
    status = str(result.get("status") or result.get("repair_gate") or "")
    return 0 if status.startswith(("PASSED", "READY", "AWAITING", "EMPTY")) else 2


def final_audit(args) -> int:
    if not args.run_id:
        raise ValueError("--run-id is required for final-audit")
    run_dir = _run_dir(args.output_root, args.run_id)
    _load_environment(run_dir, args.environment_label)
    db, store, _storage = _runtime()
    try:
        for workspace_dir in sorted((run_dir / "workspaces").glob("*")):
            preflight_path = workspace_dir / "01-preflight.json"
            if not preflight_path.exists():
                continue
            preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
            workspace_id = int(preflight["workspace_id"])
            try:
                audit = audit_workspace(
                    db=db,
                    es_client=store.client,
                    index_name=build_workspace_chunks_index_name(
                        preflight["workspace_slug"], workspace_id
                    ),
                    workspace_id=workspace_id,
                    batch_size=args.batch_size,
                    sample_limit=20,
                    index_version="legacy",
                    environment_label=args.environment_label,
                )
                _write_json(workspace_dir / "08-final-audit.json", audit)
            except Exception as exc:
                _write_json(
                    workspace_dir / "08-final-audit-error.json",
                    {
                        "status": "FAILED_FINAL_AUDIT",
                        "workspace_id": workspace_id,
                        "workspace_name": preflight.get("workspace_name"),
                        "workspace_slug": preflight.get("workspace_slug"),
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
    finally:
        db.close()
    rows = _refresh_reports(run_dir, args.environment_label)
    return 0 if all(row["next_action"] == "NONE" for row in rows) else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit and repair legacy ES in explicit, report-backed stages"
    )
    parser.add_argument("--environment-label", required=True)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path(os.environ.get("OPENRAG_MAINTENANCE_OUTPUT_ROOT", "/reports/legacy-es")),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    audit_parser = subparsers.add_parser("audit-all")
    audit_parser.add_argument("--run-id")
    audit_parser.add_argument("--batch-size", type=int, default=500)
    audit_parser.add_argument("--workspace-id", action="append", type=int)
    show_parser = subparsers.add_parser("show")
    show_parser.add_argument("--run-id", required=True)
    for command in ("prepare-missing", "apply-missing", "prepare-orphans", "delete-orphans"):
        action_parser = subparsers.add_parser(command)
        action_parser.add_argument("--run-id", required=True)
        action_parser.add_argument("--batch-size", type=int, default=500)
        action_parser.add_argument("--workspace-id", type=int, required=True)
        if command in {"apply-missing", "delete-orphans"}:
            action_parser.add_argument("--approve-sha256", required=True)
        if command == "apply-missing":
            action_parser.add_argument(
                "--allow-create-missing-index", action="store_true"
            )
    final_parser = subparsers.add_parser("final-audit")
    final_parser.add_argument("--run-id", required=True)
    final_parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args(argv)

    if args.command == "audit-all":
        return audit_all(args)
    run_dir = _run_dir(args.output_root, args.run_id)
    if args.command == "show":
        _load_environment(run_dir, args.environment_label)
        _refresh_reports(run_dir, args.environment_label)
        return 0
    if args.command == "final-audit":
        return final_audit(args)
    return _single_workspace_action(args, args.command)


if __name__ == "__main__":
    raise SystemExit(main())
