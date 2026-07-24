"""Stable, JSON-serializable candidate validation results."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ValidationCheck:
    name: str
    status: str
    error_code: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "error_code": self.error_code,
            "details": self.details,
        }


def assemble_validation_report(
    *,
    checks: list[ValidationCheck],
    expected_counts: dict[str, int],
    observed_counts: dict[str, int],
    sampled_vector_count: int,
    orphan_file_ids: list[int],
    deleted_file_ids_found: list[int],
    schema_snapshot: dict[str, Any],
    index_state: dict[str, Any],
    load_state: dict[str, Any],
    started_at: str,
) -> dict[str, Any]:
    failed = [check.name for check in checks if check.status == "failed"]
    warnings = [check.name for check in checks if check.status == "warning"]
    return {
        "checks": [check.to_dict() for check in checks],
        "passed": not failed,
        "failed_checks": failed,
        "warnings": warnings,
        "expected_counts": expected_counts,
        "observed_counts": observed_counts,
        "sampled_vector_count": sampled_vector_count,
        "orphan_file_ids": orphan_file_ids,
        "deleted_file_ids_found": deleted_file_ids_found,
        "schema_snapshot": schema_snapshot,
        "index_state": index_state,
        "load_state": load_state,
        "started_at": started_at,
        "completed_at": utc_iso(),
    }
