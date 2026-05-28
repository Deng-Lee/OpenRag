from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from openrag import database
from openrag.models import (
    Base,
    DocumentParseArtifact,
    EvalDataset,
    EvalJudgment,
    EvalQuery,
    EvalResult,
    EvalRun,
    TraceArtifact,
    TraceRun,
    TraceSnapshot,
    TraceSpan,
)


EXPECTED_TABLE_COLUMNS = {
    "document_parse_artifacts": {
        "id",
        "artifact_id",
        "file_id",
        "workspace_id",
        "source_doc_hash",
        "parser_name",
        "parser_version",
        "canonical_text_hash",
        "canonical_json_bucket",
        "canonical_json_object_key",
        "canonical_json_size_bytes",
        "canonical_md_bucket",
        "canonical_md_object_key",
        "canonical_md_size_bytes",
        "block_count",
        "page_count",
        "block_type_counts",
        "status",
        "error_message",
        "created_at",
        "updated_at",
    },
    "trace_runs": {
        "id",
        "trace_id",
        "trace_type",
        "workspace_id",
        "user_id",
        "file_id",
        "task_id",
        "eval_run_id",
        "eval_query_id",
        "query_hash",
        "query_preview",
        "status",
        "started_at",
        "ended_at",
        "duration_ms",
        "error_stage",
        "error_message",
        "sampling_reason",
        "search_config_snapshot",
        "otel_trace_id",
        "created_at",
    },
    "trace_spans": {
        "id",
        "span_id",
        "trace_id",
        "parent_span_id",
        "stage",
        "status",
        "started_at",
        "ended_at",
        "duration_ms",
        "input_summary",
        "output_summary",
        "metrics",
        "artifact_refs",
        "error_message",
        "otel_span_id",
        "created_at",
    },
    "trace_snapshots": {
        "id",
        "trace_id",
        "span_id",
        "stage",
        "rank",
        "chunk_id",
        "file_id",
        "score",
        "score_parts",
        "metadata",
        "created_at",
    },
    "trace_artifacts": {
        "id",
        "artifact_id",
        "trace_id",
        "span_id",
        "artifact_type",
        "storage_backend",
        "bucket",
        "object_key",
        "content_type",
        "size_bytes",
        "metadata",
        "created_at",
        "expires_at",
    },
    "eval_datasets": {
        "id",
        "name",
        "description",
        "workspace_id",
        "status",
        "metadata",
        "created_by",
        "created_at",
        "updated_at",
    },
    "eval_queries": {
        "id",
        "dataset_id",
        "query_text",
        "query_hash",
        "query_type",
        "expected_answer",
        "metadata",
        "created_at",
        "updated_at",
    },
    "eval_judgments": {
        "id",
        "dataset_id",
        "eval_query_id",
        "chunk_id",
        "file_id",
        "relevance_grade",
        "source",
        "weight",
        "judge_user_id",
        "judge_model",
        "judge_reason_ref",
        "metadata",
        "created_at",
        "updated_at",
    },
    "eval_runs": {
        "id",
        "dataset_id",
        "name",
        "status",
        "search_config_snapshot",
        "code_version",
        "index_version",
        "started_at",
        "ended_at",
        "created_by",
        "metadata",
        "created_at",
    },
    "eval_results": {
        "id",
        "eval_run_id",
        "eval_query_id",
        "trace_id",
        "metric_scope",
        "source_scope",
        "metrics",
        "created_at",
    },
}


def _index_columns(table_name: str) -> set[tuple[str, ...]]:
    return {
        tuple(index.expressions[0].table.c[column.name].name for column in index.columns)
        for index in Base.metadata.tables[table_name].indexes
    }


def test_trace_eval_models_are_registered_with_expected_columns() -> None:
    assert DocumentParseArtifact.__tablename__ == "document_parse_artifacts"
    assert TraceRun.__tablename__ == "trace_runs"
    assert TraceSpan.__tablename__ == "trace_spans"
    assert TraceSnapshot.__tablename__ == "trace_snapshots"
    assert TraceArtifact.__tablename__ == "trace_artifacts"
    assert EvalDataset.__tablename__ == "eval_datasets"
    assert EvalQuery.__tablename__ == "eval_queries"
    assert EvalJudgment.__tablename__ == "eval_judgments"
    assert EvalRun.__tablename__ == "eval_runs"
    assert EvalResult.__tablename__ == "eval_results"

    for table_name, expected_columns in EXPECTED_TABLE_COLUMNS.items():
        assert table_name in Base.metadata.tables
        assert expected_columns <= set(Base.metadata.tables[table_name].columns.keys())


def test_create_all_builds_trace_eval_tables_in_sqlite() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    inspector = inspect(engine)
    for table_name in EXPECTED_TABLE_COLUMNS:
        assert table_name in inspector.get_table_names()


def test_trace_eval_indexes_match_query_patterns() -> None:
    assert ("file_id", "source_doc_hash", "parser_name", "parser_version") in _index_columns(
        "document_parse_artifacts"
    )
    assert ("trace_id",) in _index_columns("trace_runs")
    assert ("trace_type", "created_at") in _index_columns("trace_runs")
    assert ("workspace_id", "created_at") in _index_columns("trace_runs")
    assert ("file_id", "created_at") in _index_columns("trace_runs")
    assert ("eval_run_id", "eval_query_id") in _index_columns("trace_runs")
    assert ("query_hash", "created_at") in _index_columns("trace_runs")
    assert ("trace_id", "stage", "rank") in _index_columns("trace_snapshots")
    assert ("chunk_id", "created_at") in _index_columns("trace_snapshots")


def test_eval_judgment_constraints_and_default_weights() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    with SessionLocal() as session:
        dataset = EvalDataset(name="mini", workspace_id=1)
        query = EvalQuery(dataset_id=1, query_text="人民币汇率", query_hash="qh")
        session.add_all([dataset, query])
        session.flush()

        session.add_all(
            [
                EvalJudgment(
                    dataset_id=dataset.id,
                    eval_query_id=query.id,
                    chunk_id="chunk-a",
                    relevance_grade=3,
                    source="gold_manual",
                ),
                EvalJudgment(
                    dataset_id=dataset.id,
                    eval_query_id=query.id,
                    chunk_id="chunk-b",
                    relevance_grade=2,
                    source="business_import",
                ),
                EvalJudgment(
                    dataset_id=dataset.id,
                    eval_query_id=query.id,
                    chunk_id="chunk-c",
                    relevance_grade=1,
                    source="llm_assisted",
                ),
            ]
        )
        session.commit()

        weights = {
            judgment.source: judgment.weight
            for judgment in session.query(EvalJudgment).order_by(EvalJudgment.chunk_id)
        }
        assert weights == {
            "gold_manual": 1.0,
            "business_import": 0.7,
            "llm_assisted": 0.4,
        }

        session.add(
            EvalJudgment(
                dataset_id=dataset.id,
                eval_query_id=query.id,
                chunk_id="bad-grade",
                relevance_grade=4,
                source="gold_manual",
            )
        )
        with pytest.raises((IntegrityError, ValueError)):
            session.commit()
        session.rollback()

        session.add(
            EvalJudgment(
                dataset_id=dataset.id,
                eval_query_id=query.id,
                chunk_id="bad-source",
                relevance_grade=1,
                source="other",
            )
        )
        with pytest.raises((IntegrityError, ValueError)):
            session.commit()


def test_init_db_imports_models_before_create_all(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    monkeypatch.setattr(database, "get_engine", lambda: engine)

    database.init_db()

    inspector = inspect(engine)
    for table_name in EXPECTED_TABLE_COLUMNS:
        assert table_name in inspector.get_table_names()


def test_alembic_configuration_and_first_revision_exist() -> None:
    project_root = Path(__file__).resolve().parent.parent
    alembic_ini = project_root / "alembic.ini"
    env_py = project_root / "alembic" / "env.py"
    revision_py = project_root / "alembic" / "versions" / "20260527_0001_trace_eval_tables.py"

    assert alembic_ini.exists()
    assert env_py.exists()
    assert revision_py.exists()

    env_text = env_py.read_text(encoding="utf-8")
    assert "get_database_url" in env_text
    assert "Base.metadata" in env_text

    revision_text = revision_py.read_text(encoding="utf-8")
    for table_name in EXPECTED_TABLE_COLUMNS:
        assert table_name in revision_text
