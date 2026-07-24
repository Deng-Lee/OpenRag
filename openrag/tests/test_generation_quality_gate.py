from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from openrag.config import IndexQualityConfig
from openrag.indexing.quality_gate import QualityGateService
from openrag.models import Base, EvalResult
from openrag.models.index_generation import IndexGeneration, IndexGenerationState
from openrag.services.eval_service import EvalService


def _generation(generation_id, state, model):
    return IndexGeneration(
        id=generation_id,
        scope="global",
        state=state.value,
        embedding_provider="provider",
        embedding_model=model,
        embedding_revision="revision",
        embedding_dimension=3,
        embedding_fingerprint=generation_id[0] * 64,
        embedding_config_ref="embedding/default",
        vector_normalization="none",
        distance_metric="COSINE",
        schema_version=2,
        chunk_policy_revision="chunk-v2",
        hierarchy_policy_revision="hierarchy-v2",
        chunk_collection_name=f"chunks_{generation_id[0]}",
        layer_collection_name=f"layers_{generation_id[0]}",
        manifest={"embedding": {}},
        validation_report={"passed": True},
    )


def _environment():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    active = _generation(
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        IndexGenerationState.ACTIVE,
        "model-a",
    )
    candidate = _generation(
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        IndexGenerationState.READY,
        "model-b",
    )
    db.add_all([active, candidate])
    db.commit()
    service = EvalService(db, search_callable=lambda **_: [])
    dataset = service.create_dataset(name="quality", workspace_id=1)
    active_run = service.create_eval_run(
        dataset.id, {}, generation_id=active.id
    )
    candidate_run = service.create_eval_run(
        dataset.id, {}, generation_id=candidate.id
    )
    active_metrics = {
        "status": "success",
        "recall@10": 0.80,
        "ndcg@10": 0.70,
        "mrr@50": 0.75,
        "latency_p95_ms": 100.0,
    }
    candidate_metrics = {
        "status": "success",
        "recall@10": 0.79,
        "ndcg@10": 0.69,
        "mrr@50": 0.74,
        "latency_p95_ms": 110.0,
    }
    db.add_all(
        [
            EvalResult(
                eval_run_id=active_run.id,
                metric_scope="run_summary",
                source_scope="weighted_all",
                metrics=active_metrics,
            ),
            EvalResult(
                eval_run_id=candidate_run.id,
                metric_scope="run_summary",
                source_scope="weighted_all",
                metrics=candidate_metrics,
            ),
        ]
    )
    db.commit()
    return engine, db, candidate, active_run, candidate_run


def test_same_dataset_different_model_candidate_can_pass_quality_gate():
    engine, db, candidate, active_run, candidate_run = _environment()
    gate = QualityGateService(
        db,
        IndexQualityConfig(
            recall_max_regression=0.02,
            ndcg_max_regression=0.02,
            mrr_max_regression=0.02,
            p95_max_regression=0.20,
        ),
    )
    report = gate.evaluate_candidate(
        candidate.id,
        active_run_id=active_run.id,
        candidate_run_id=candidate_run.id,
        security_results=[
            {"allowed_file_ids": [1, 2], "result_file_ids": [1, 2]}
        ],
    )

    assert active_run.dataset_id == candidate_run.dataset_id
    assert candidate_run.index_version == candidate.id
    assert candidate_run.search_config_snapshot["embedding_fingerprint"] == candidate.embedding_fingerprint
    assert report["passed"] is True
    assert candidate.quality_gate_passed is True
    gate.assert_quality_gate(candidate.id)
    db.close()
    Base.metadata.drop_all(engine)


def test_metric_regression_and_permission_leak_fail_gate():
    engine, db, candidate, active_run, candidate_run = _environment()
    summary = (
        db.query(EvalResult)
        .filter(
            EvalResult.eval_run_id == candidate_run.id,
            EvalResult.metric_scope == "run_summary",
        )
        .one()
    )
    summary.metrics = {**summary.metrics, "recall@10": 0.5}
    db.commit()
    report = QualityGateService(db).evaluate_candidate(
        candidate.id,
        active_run_id=active_run.id,
        candidate_run_id=candidate_run.id,
        security_results=[
            {"allowed_file_ids": [1], "result_file_ids": [1, 999]}
        ],
    )

    assert report["passed"] is False
    assert report["metric_comparisons"]["recall"]["passed"] is False
    assert report["security"]["permission_leak_count"] == 1
    assert candidate.quality_gate_passed is False
    db.close()
    Base.metadata.drop_all(engine)
