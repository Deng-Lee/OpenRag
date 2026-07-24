from datetime import datetime, timezone

from openrag.broker.task_broker import TaskBroker
from openrag.models.index_generation import IndexGenerationRoute
from openrag.models.task import TaskType
from openrag.services.task_service import TaskService

from test_task_generation_binding import db


def test_write_barrier_skips_index_writes_but_assigns_deletes(db):
    session, user, workspace = db
    route = session.get(IndexGenerationRoute, "global")
    route.write_barrier = True
    route.updated_at = datetime.now(timezone.utc)
    session.commit()
    write = TaskService(session).create_task(
        workspace_id=workspace.id,
        user_id=user.id,
        task_type=TaskType.PROCESS_DOCUMENT.value,
        priority=10,
    )
    delete = TaskService(session).create_task(
        workspace_id=workspace.id,
        user_id=user.id,
        task_type=TaskType.DELETE_FILE.value,
        priority=1,
    )

    assigned = TaskBroker(session).get_tasks("worker-delete", 2)

    assert [task.id for task in assigned] == [delete.id]
    assert write.index_generation_id is None
