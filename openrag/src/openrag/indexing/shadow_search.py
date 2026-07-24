"""Budgeted internal candidate search that can never replace the active response."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
import hashlib
import time
from typing import Any, Callable

from openrag.config import IndexQualityConfig, get_config


class ShadowSearchService:
    def __init__(
        self,
        config: IndexQualityConfig | None = None,
        recorder: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.config = config or get_config().index_quality
        self.recorder = recorder

    def should_shadow(self, request_key: str) -> bool:
        if self.config.shadow_sample_rate <= 0:
            return False
        bucket = int(hashlib.sha256(request_key.encode("utf-8")).hexdigest()[:8], 16)
        return bucket / 0xFFFFFFFF < self.config.shadow_sample_rate

    def run_candidate_shadow(
        self,
        *,
        request_key: str,
        query_hash: str,
        active_results: list[dict[str, Any]],
        candidate_search: Callable[[], list[dict[str, Any]]],
        allowed_file_ids: set[int],
    ) -> dict[str, Any] | None:
        if not self.should_shadow(request_key):
            return None
        started = time.monotonic()
        status = "success"
        candidate_results: list[dict[str, Any]] = []
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(candidate_search)
        try:
            candidate_results = future.result(
                timeout=self.config.shadow_budget_ms / 1000
            )
        except TimeoutError:
            status = "timeout"
            future.cancel()
        except Exception:
            status = "failed"
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        comparison = self.record_shadow_comparison(
            query_hash=query_hash,
            status=status,
            active_results=active_results,
            candidate_results=candidate_results,
            allowed_file_ids=allowed_file_ids,
            elapsed_ms=(time.monotonic() - started) * 1000,
        )
        return comparison

    def record_shadow_comparison(
        self,
        *,
        query_hash: str,
        status: str,
        active_results: list[dict[str, Any]],
        candidate_results: list[dict[str, Any]],
        allowed_file_ids: set[int],
        elapsed_ms: float,
    ) -> dict[str, Any]:
        candidate_ids = {
            int(item["file_id"])
            for item in candidate_results
            if item.get("file_id") is not None
        }
        comparison = {
            "query_hash": query_hash,
            "status": status,
            "active_result_count": len(active_results),
            "candidate_result_count": len(candidate_results),
            "candidate_permission_leak_count": len(candidate_ids - allowed_file_ids),
            "elapsed_ms": elapsed_ms,
        }
        if self.recorder is not None:
            self.recorder(comparison)
        return comparison
