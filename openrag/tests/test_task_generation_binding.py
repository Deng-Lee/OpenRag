from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from openrag.broker.task_broker import TaskBroker
from openrag.models.base import Base
from openrag.models.index_generation import IndexGeneration, IndexGenerationRoute
from openrag.models.task import TaskStatus, TaskType
from openrag.models.user import User
from openrag.models.workspace import Workspace
from openrag.services.task_service import TaskService


def _generation(generation_id: str, state: str = "active") -> IndexGeneration:
    return IndexGeneration(
        id=generation_id,
        scope="global",
        state=state,
        embedding_provider="openai-compatible",
        embedding_model="model",
        embedding_revision="revision",
        embedding_dimension=3,
        embedding_fingerprint=(generation_id[-1] * 64),
        embedding_config_ref="env:embedding",
        vector_normalization="none",
        distance_metric="COSINE",
        schema_version=2,
        chunk_policy_revision="chunk-v1",
        hierarchy_policy_revision="hierarchy-v1",
        chunk_collection_name=f"chunks_{generation_id}",
        layer_collection_name=f"layers_{generation_id}",
        manifest={"generation_id": generation_id},
    )


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    user = User(
        username="generation-user",
        email="generation@example.com",
        password_hash="hash",
        full_name="Generation User",
        is_active=True,
    )
    session.add(user)
    session.flush()
    workspace = Workspace(
        name="Generation Workspace",
        slug="generation-workspace",
        owner_id=user.id,
        max_concurrent_tasks=5,
    )
    session.add(workspace)
    active = _generation("generation-a")
    session.add(active)
    session.flush()
    session.add(
        IndexGenerationRoute(
            scope="global",
            active_generation_id=active.id,
            route_version=1,
            activated_at=datetime.now(timezone.utc),
        )
    )
    session.commit()
    yield session, user, workspace
    session.close()
    Base.metadata.drop_all(engine)


def test_online_write_task_binds_active_generation_when_claimed(db):
    session, user, workspace = db
    task = TaskService(session).create_task(
        workspace_id=workspace.id,
        user_id=user.id,
        task_type=TaskType.PROCESS_DOCUMENT.value,
    )

    assigned = TaskBroker(session).get_tasks("worker-a", 1)

    assert [row.id for row in assigned] == [task.id]
    assert assigned[0].index_generation_id == "generation-a"
    assert assigned[0].to_dict()["index_generation_id"] == "generation-a"


def test_explicit_candidate_binding_is_preserved_across_route_change(db):
    session, user, workspace = db
    candidate = _generation("generation-b", state="building")
    session.add(candidate)
    session.commit()
    task = TaskService(session).create_task(
        workspace_id=workspace.id,
        user_id=user.id,
        task_type=TaskType.EMBED_DOCUMENT.value,
        index_generation_id=candidate.id,
    )

    assigned = TaskBroker(session).get_tasks("worker-candidate", 1)

    assert assigned[0].id == task.id
    assert assigned[0].index_generation_id == candidate.id


def test_retry_keeps_original_generation_binding(db):
    session, user, workspace = db
    task = TaskService(session).create_task(
        workspace_id=workspace.id,
        user_id=user.id,
        index_generation_id="generation-a",
    )
    task.status = TaskStatus.FAILURE.value
    session.commit()

    retried = TaskService(session).retry_task(task.id)

    assert retried.index_generation_id == "generation-a"
