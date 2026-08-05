"""Render legacy Elasticsearch maintenance results for humans and automation."""

from __future__ import annotations

import csv
import json
from pathlib import Path
import unicodedata
from typing import Any, Iterable


FORMAT_VERSION = 1
_TABLE_COLUMNS = (
    ("workspace_id", "ID"),
    ("workspace_slug", "SLUG"),
    ("db_active_chunk_count", "PG_CHUNKS"),
    ("es_doc_count", "ES_DOCS"),
    ("index_status", "INDEX"),
    ("mapping_status", "MAPPING"),
    ("missing_es_chunk_count", "MISSING"),
    ("orphan_chunk_count", "ORPHAN"),
    ("deleted_file_chunk_count", "DELETED"),
    ("file_id_mismatch_count", "MISMATCH"),
    ("missing_l2_count", "L2_BAD"),
    ("gate", "GATE"),
    ("next_action", "NEXT_ACTION"),
)


def _display_width(value: object) -> int:
    return sum(
        2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        for char in str(value)
    )


def _pad(value: object, width: int) -> str:
    text = str(value)
    return text + " " * max(0, width - _display_width(text))


def _audit_from_result(result: dict[str, Any]) -> dict[str, Any]:
    return (
        result.get("post_legacy_audit")
        or result.get("legacy_audit")
        or result.get("pre_legacy_audit")
        or {}
    )


def _preflight_from_result(result: dict[str, Any]) -> dict[str, Any]:
    return result.get("preflight") or result


def _next_action(gate: str, orphan_count: int) -> str:
    if gate == "READY_FOR_MISSING_ONLY_UPSERT":
        return "PREPARE_MISSING"
    if gate == "READY_AFTER_APPROVED_INDEX_CREATE":
        return "PREPARE_MISSING_AND_APPROVE_INDEX"
    if gate in {"BLOCKED_L2", "BLOCKED_MAPPING", "BLOCKED_ES_RESOURCE"}:
        return "MANUAL_REVIEW"
    if gate in {"BLOCKED_LEGACY_AUDIT", "BLOCKED_CONCURRENT_DRIFT"}:
        return "DIAGNOSE_DATA"
    if gate == "AWAITING_MISSING_REPAIR_APPROVAL":
        return "APPLY_MISSING_AFTER_APPROVAL"
    if gate == "AWAITING_SEPARATE_APPROVAL":
        return "DELETE_ORPHANS_AFTER_APPROVAL"
    if gate == "BLOCKED_INDEX_CREATE_APPROVAL":
        return "APPROVE_INDEX_CREATE"
    if orphan_count:
        return "PREPARE_ORPHANS"
    if gate.startswith("FAILED"):
        return "RETRY_AFTER_DIAGNOSIS"
    return "NONE"


def _explain_result(
    *,
    result: dict[str, Any],
    gate: str,
    mapping_status: str,
    index_status: str,
    missing_count: int,
    orphan_count: int,
    missing_l2_count: int,
    deleted_count: int,
    mismatch_count: int,
) -> tuple[str, str]:
    if gate.startswith("FAILED"):
        return (
            str(result.get("error") or "该阶段执行失败，详见阶段 JSON 证据。"),
            "先根据错误证据修复连接、权限或服务异常，再使用新的 run-id 重试。",
        )
    if mapping_status not in {
        "MAPPING_COMPATIBLE",
        "INDEX_ABSENT",
        "EMPTY_INDEX_NOT_REQUIRED",
        "UNKNOWN",
    }:
        return (
            f"legacy 索引 Mapping 预检未通过：{mapping_status}。",
            "人工核对索引资源与 Mapping；在兼容性确认前禁止自动写入或删除。",
        )
    if missing_l2_count:
        return (
            f"{missing_l2_count} 个待补 Chunk 缺少或无法读取 MinIO L2。",
            "先恢复对应 L2；本流程不会运行 Parser、Chunker 或修改其他存储。",
        )
    if index_status == "ABSENT" and missing_count:
        return (
            "PostgreSQL 存在有效 Chunk，但对应 legacy 物理索引不存在。",
            "生成 missing 修复计划，单独批准创建索引后再执行 missing-only upsert。",
        )
    if deleted_count or mismatch_count:
        return (
            f"发现 deleted={deleted_count}、mismatch={mismatch_count} 的跨存储不一致。",
            "先人工诊断 PostgreSQL 与 ES 归属关系；禁止进入自动修复或删除。",
        )
    if missing_count:
        return (
            f"PostgreSQL 有效 Chunk 中有 {missing_count} 条未出现在 legacy ES。",
            "生成并审核 missing 计划，仅从现有 MinIO L2 幂等补写缺失文档。",
        )
    if orphan_count:
        return (
            f"legacy ES 中有 {orphan_count} 条文档不属于 PostgreSQL 当前有效 Chunk。",
            "生成精确孤儿 ID 清单并单独审批，审批后仅删除该清单中的 ES 文档。",
        )
    if gate.startswith("BLOCKED"):
        return (
            f"安全门禁阻止当前阶段：{gate}。",
            "查看阶段 JSON 中的 preflight 和错误信息，消除阻塞后使用新的 run-id 重试。",
        )
    return (
        "PostgreSQL、legacy ES 与本阶段要求一致，未发现待处理异常。",
        "无需修复；可进入下一阶段或保留报告作为审计证据。",
    )


def build_workspace_row(result: dict[str, Any]) -> dict[str, Any]:
    """Normalize any maintenance-stage result into one summary row."""
    preflight = _preflight_from_result(result)
    audit = _audit_from_result(result) or _audit_from_result(preflight)
    mapping = audit.get("mapping_preflight") or preflight.get("mapping_preflight") or {}
    l2 = preflight.get("l2_preflight") or {}
    latest_status = result.get("status")
    status = str(latest_status or preflight.get("repair_gate") or "FAILED")
    gate = str(latest_status or preflight.get("repair_gate") or status)
    orphan_count = int(audit.get("orphan_chunk_count") or 0)
    index_exists = audit.get("index_exists")
    if mapping.get("resource_type") == "absent" or index_exists is False:
        index_status = "ABSENT"
    elif mapping.get("resource_type") == "physical_index" or index_exists is True:
        index_status = "EXISTS"
    else:
        index_status = str(mapping.get("resource_type") or "UNKNOWN").upper()
    missing_count = int(audit.get("missing_es_chunk_count") or 0)
    deleted_count = int(audit.get("deleted_file_chunk_count") or 0)
    mismatch_count = int(audit.get("file_id_mismatch_count") or 0)
    missing_l2_count = int(l2.get("missing_l2_count") or 0)
    mapping_status = str(mapping.get("status") or "UNKNOWN")
    reason, solution = _explain_result(
        result=result,
        gate=gate,
        mapping_status=mapping_status,
        index_status=index_status,
        missing_count=missing_count,
        orphan_count=orphan_count,
        missing_l2_count=missing_l2_count,
        deleted_count=deleted_count,
        mismatch_count=mismatch_count,
    )
    return {
        "format_version": FORMAT_VERSION,
        "workspace_id": result.get("workspace_id", preflight.get("workspace_id", "-")),
        "workspace_name": result.get(
            "workspace_name", preflight.get("workspace_name", "-")
        ),
        "workspace_slug": result.get(
            "workspace_slug", preflight.get("workspace_slug", "-")
        ),
        "db_active_chunk_count": int(
            audit.get(
                "db_active_chunk_count",
                preflight.get("db_active_chunk_count") or 0,
            )
        ),
        "es_doc_count": int(audit.get("es_doc_count") or 0),
        "index_status": index_status,
        "mapping_status": mapping_status,
        "missing_es_chunk_count": missing_count,
        "orphan_chunk_count": orphan_count,
        "deleted_file_chunk_count": deleted_count,
        "file_id_mismatch_count": mismatch_count,
        "missing_l2_count": missing_l2_count,
        "gate": gate,
        "status": status,
        "next_action": _next_action(gate, orphan_count),
        "reason": reason,
        "recommended_solution": solution,
    }


def render_terminal_table(rows: Iterable[dict[str, Any]]) -> str:
    normalized = list(rows)
    headers = [label for _key, label in _TABLE_COLUMNS]
    values = [[row.get(key, "-") for key, _label in _TABLE_COLUMNS] for row in normalized]
    widths = [
        max([_display_width(headers[index])] + [_display_width(row[index]) for row in values])
        for index in range(len(headers))
    ]
    lines = [
        "  ".join(_pad(value, widths[index]) for index, value in enumerate(headers))
    ]
    lines.append("  ".join("-" * width for width in widths))
    lines.extend(
        "  ".join(_pad(value, widths[index]) for index, value in enumerate(row))
        for row in values
    )
    return "\n".join(lines)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _workspace_latest_result(workspace_dir: Path) -> dict[str, Any]:
    preflight_path = workspace_dir / "01-preflight.json"
    preflight = _read_json(preflight_path) if preflight_path.exists() else {}
    action_paths = (
        workspace_dir / "08-final-audit.json",
        workspace_dir / "08-final-audit-error.json",
        workspace_dir / "07-post-orphan-audit.json",
        workspace_dir / "06-orphan-delete.json",
        workspace_dir / "05-orphan-delete-manifest.json",
        workspace_dir / "04-post-missing-audit.json",
        workspace_dir / "03-missing-apply.json",
        workspace_dir / "02-missing-plan.json",
        preflight_path,
        workspace_dir / "01-preflight-error.json",
    )
    for path in action_paths:
        if not path.exists():
            continue
        result = _read_json(path)
        if path.name.endswith("audit.json") and preflight:
            return {
                "status": (
                    "PASSED_FINAL_AUDIT"
                    if int(result.get("anomaly_count") or 0) == 0
                    else "FAILED_FINAL_AUDIT"
                ),
                "workspace_id": preflight.get("workspace_id"),
                "workspace_name": preflight.get("workspace_name"),
                "workspace_slug": preflight.get("workspace_slug"),
                "preflight": preflight,
                "post_legacy_audit": result,
            }
        if path.name.endswith("audit-error.json") and preflight:
            return {
                **result,
                "workspace_id": preflight.get("workspace_id"),
                "workspace_name": preflight.get("workspace_name"),
                "workspace_slug": preflight.get("workspace_slug"),
                "preflight": preflight,
            }
        return result
    return {
        "status": "FAILED_MISSING_REPORT",
        "workspace_id": "-",
        "workspace_slug": workspace_dir.name,
    }


def _markdown_table(rows: list[dict[str, Any]]) -> str:
    headers = [label for _key, label in _TABLE_COLUMNS]
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    for row in rows:
        lines.append(
            "| "
            + " | ".join(str(row.get(key, "-")).replace("|", "\\|") for key, _ in _TABLE_COLUMNS)
            + " |"
        )
    return "\n".join(lines)


def write_run_reports(run_dir: Path, *, environment_label: str) -> list[dict[str, Any]]:
    """Rebuild derived reports from immutable per-stage JSON evidence."""
    workspace_root = run_dir / "workspaces"
    workspace_dirs = sorted(path for path in workspace_root.glob("*") if path.is_dir())
    results = [_workspace_latest_result(path) for path in workspace_dirs]
    rows = [build_workspace_row(result) for result in results]
    summary = {
        "format_version": FORMAT_VERSION,
        "environment_label": environment_label,
        "workspace_count": len(rows),
        "passed_count": sum(row["next_action"] == "NONE" for row in rows),
        "action_required_count": sum(row["next_action"] != "NONE" for row in rows),
        "rows": rows,
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (run_dir / "summary.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else ["format_version"])
        writer.writeheader()
        writer.writerows(rows)
    (run_dir / "summary.md").write_text(
        "# Legacy ES Workspace audit summary\n\n"
        f"Environment: `{environment_label}`\n\n"
        + _markdown_table(rows)
        + "\n",
        encoding="utf-8",
    )
    for workspace_dir, row in zip(workspace_dirs, rows):
        evidence = sorted(path.name for path in workspace_dir.glob("*.json"))
        (workspace_dir / "execution.md").write_text(
            f"# Workspace {row['workspace_id']} / {row['workspace_slug']}\n\n"
            + _markdown_table([row])
            + "\n\n## 结果解释\n\n"
            + f"- 原因：{row['reason']}\n"
            + f"- 解决方案：{row['recommended_solution']}\n"
            + f"- 下一步：`{row['next_action']}`\n"
            + "\n## Evidence\n\n"
            + "\n".join(f"- `{name}`" for name in evidence)
            + "\n",
            encoding="utf-8",
        )
    return rows
