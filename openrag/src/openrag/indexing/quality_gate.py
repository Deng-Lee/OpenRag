"""Offline quality and fixed-zero authorization leakage gate."""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from openrag.config import IndexQualityConfig, get_config
from openrag.models.index_generation import IndexGenerationState
from openrag.services.eval_service import EvalService
from openrag.services.index_generation_service import IndexGenerationService


class GenerationQualityGateError(RuntimeError):
    code = "INDEX_GENERATION_QUALITY_GATE_FAILED"


def _metric(metrics: dict[str, Any], prefix: str) -> float | None:
    for key in sorted(metrics):
        if key.startswith(prefix) and isinstance(metrics[key], (int, float)):
            return float(metrics[key])
    return None


class QualityGateService:
    def __init__(
        self,
        db: Session,
        config: IndexQualityConfig | None = None,
    ):
        self.db = db
        self.config = config or get_config().index_quality

    def compare_metrics(
        self, active: dict[str, Any], candidate: dict[str, Any]
    ) -> dict[str, Any]:
        thresholds = {
            "recall": self.config.recall_max_regression,
            "ndcg": self.config.ndcg_max_regression,
            "mrr": self.config.mrr_max_regression,
        }
        comparisons = {}
        for name, threshold in thresholds.items():
            active_value = _metric(active, f"{name}@")
            candidate_value = _metric(candidate, f"{name}@")
            regression = (
                None
                if active_value is None or candidate_value is None
                else active_value - candidate_value
            )
            comparisons[name] = {
                "active": active_value,
                "candidate": candidate_value,
                "regression": regression,
                "max_regression": threshold,
                "passed": regression is not None and regression <= threshold,
            }
        active_p95 = active.get("latency_p95_ms")
        candidate_p95 = candidate.get("latency_p95_ms")
        if isinstance(active_p95, (int, float)) and active_p95 > 0 and isinstance(
            candidate_p95, (int, float)
        ):
            p95_regression = float(candidate_p95) / float(active_p95) - 1.0
        else:
            p95_regression = None
        comparisons["latency_p95"] = {
            "active": active_p95,
            "candidate": candidate_p95,
            "regression": p95_regression,
            "max_regression": self.config.p95_max_regression,
            "passed": p95_regression is not None
            and p95_regression <= self.config.p95_max_regression,
        }
        return comparisons

    @staticmethod
    def validate_security_results(
        security_results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        leaked = set()
        for item in security_results:
            allowed = {int(value) for value in item.get("allowed_file_ids", [])}
            returned = {int(value) for value in item.get("result_file_ids", [])}
            leaked.update(returned - allowed)
        return {
            "permission_leak_count": len(leaked),
            "leaked_file_ids": sorted(leaked),
            "passed": not leaked,
        }

    def build_quality_report(
        self,
        comparison: dict[str, Any],
        security: dict[str, Any],
    ) -> dict[str, Any]:
        metric_comparisons = self.compare_metrics(
            comparison["active_metrics"], comparison["candidate_metrics"]
        )
        passed = all(item["passed"] for item in metric_comparisons.values()) and security[
            "passed"
        ]
        return {
            "passed": passed,
            "dataset_id": comparison["dataset_id"],
            "active_run_id": comparison["active_run_id"],
            "candidate_run_id": comparison["candidate_run_id"],
            "active_generation_id": comparison["active_generation_id"],
            "candidate_generation_id": comparison["candidate_generation_id"],
            "metric_comparisons": metric_comparisons,
            "security": security,
            "validated_at": datetime.now(timezone.utc).isoformat(),
        }

    def evaluate_candidate(
        self,
        generation_id: str,
        *,
        active_run_id: int,
        candidate_run_id: int,
        security_results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        generation = IndexGenerationService(self.db)._require_generation(generation_id)
        if generation.state != IndexGenerationState.READY.value:
            raise GenerationQualityGateError("Candidate must be ready before quality gate")
        comparison = EvalService(self.db).compare_eval_runs(
            active_run_id, candidate_run_id
        )
        if comparison["candidate_generation_id"] != generation_id:
            raise GenerationQualityGateError(
                "Candidate eval run targets a different generation"
            )
        report = self.build_quality_report(
            comparison, self.validate_security_results(security_results)
        )
        generation.quality_report = report
        generation.quality_gate_passed = report["passed"]
        generation.quality_validated_at = datetime.now(timezone.utc)
        self.db.commit()
        return report

    def assert_quality_gate(self, generation_id: str) -> None:
        generation = IndexGenerationService(self.db)._require_generation(generation_id)
        if generation.quality_gate_passed is not True or not generation.quality_report:
            raise GenerationQualityGateError(
                "Candidate quality and security gates have not passed"
            )
