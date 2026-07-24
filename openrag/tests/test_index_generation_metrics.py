from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from openrag.indexing.metrics import (
    collect_cleanup_failure_metrics,
    collect_generation_progress_metrics,
    collect_task_state_gauges,
)
from openrag.models import Base
from openrag.models.index_generation import IndexGeneration, IndexGenerationState
from openrag.models.task import Task, TaskStatus, TaskType


def test_metrics_are_current_row_gauges_and_cleanup_failure_names_generation():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    generation = IndexGeneration(
        id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        scope="global",
        state=IndexGenerationState.RETIRED.value,
        embedding_provider="provider",
        embedding_model="model",
        embedding_revision="revision",
        embedding_dimension=3,
        embedding_fingerprint="a" * 64,
        embedding_config_ref="embedding/default",
        vector_normalization="none",
        distance_metric="COSINE",
        schema_version=2,
        chunk_policy_revision="chunk-v2",
        hierarchy_policy_revision="hierarchy-v2",
        chunk_collection_name="chunks_a",
        layer_collection_name="layers_a",
        manifest={},
        build_lag_files=3,
    )
    db.add(generation)
    db.flush()
    task = Task(
        task_id="delete-failure",
        workspace_id=1,
        user_id=1,
        task_type=TaskType.PURGE_FILE_FROM_GENERATIONS.value,
        status=TaskStatus.RETRY.value,
        error_code="GENERATION_DELETE_INCOMPLETE",
        result={"failed_generation_ids": [generation.id]},
    )
    db.add(task)
    db.commit()

    assert collect_generation_progress_metrics(db)[0]["build_lag_files"] == 3
    failures = collect_cleanup_failure_metrics(db)
    assert failures[0]["failed_generation_ids"] == [generation.id]
    gauges = collect_task_state_gauges(db)
    assert gauges == [
        {
            "task_type": TaskType.PURGE_FILE_FROM_GENERATIONS.value,
            "status": TaskStatus.RETRY.value,
            "count": 1,
        }
    ]
    db.close()
    Base.metadata.drop_all(engine)
