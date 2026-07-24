import hashlib
import time

from openrag.config import IndexQualityConfig
from openrag.indexing.shadow_search import ShadowSearchService


def test_candidate_shadow_never_changes_active_response_and_records_no_query_text():
    recorded = []
    active = [{"file_id": 1, "text": "active"}]
    original = list(active)
    query = "confidential query"
    service = ShadowSearchService(
        IndexQualityConfig(shadow_sample_rate=1.0, shadow_budget_ms=100),
        recorder=recorded.append,
    )
    comparison = service.run_candidate_shadow(
        request_key="request-1",
        query_hash=hashlib.sha256(query.encode()).hexdigest(),
        active_results=active,
        candidate_search=lambda: [{"file_id": 2, "text": "candidate"}],
        allowed_file_ids={1},
    )

    assert active == original
    assert comparison["candidate_permission_leak_count"] == 1
    assert "query" not in comparison
    assert query not in str(recorded)


def test_candidate_failure_or_timeout_stays_inside_shadow_budget():
    service = ShadowSearchService(
        IndexQualityConfig(shadow_sample_rate=1.0, shadow_budget_ms=5)
    )
    started = time.monotonic()
    timed_out = service.run_candidate_shadow(
        request_key="request-2",
        query_hash="hash",
        active_results=[],
        candidate_search=lambda: (time.sleep(0.05) or []),
        allowed_file_ids=set(),
    )
    elapsed_ms = (time.monotonic() - started) * 1000

    assert timed_out["status"] == "timeout"
    assert elapsed_ms < 40

    failed = service.run_candidate_shadow(
        request_key="request-3",
        query_hash="hash",
        active_results=[],
        candidate_search=lambda: (_ for _ in ()).throw(RuntimeError("down")),
        allowed_file_ids=set(),
    )
    assert failed["status"] == "failed"
