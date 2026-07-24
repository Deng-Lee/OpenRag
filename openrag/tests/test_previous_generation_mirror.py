from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from openrag.indexing.mirror_service import PreviousGenerationMirrorService
from openrag.indexing.source_reader import GenerationSourceSnapshot
from openrag.models import Base
from openrag.models.file import File, ProcessingStatus
from openrag.models.index_generation import (
    IndexGeneration,
    IndexGenerationFile,
    IndexGenerationFileState,
    IndexGenerationRoute,
    IndexGenerationState,
)
from openrag.models.task import TaskType
from openrag.models.user import User
from openrag.models.workspace import Workspace


def _generation(generation_id, state):
    return IndexGeneration(
        id=generation_id,
        scope="global",
        state=state.value,
        embedding_provider="provider",
        embedding_model="model",
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
        manifest={},
    )


def test_new_update_delete_are_reflected_in_previous_mirror_lag():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    user = User(
        username="mirror",
        email="mirror@example.com",
        password_hash="hash",
        full_name="Mirror",
        is_active=True,
    )
    db.add(user)
    db.flush()
    workspace = Workspace(name="Mirror", slug="mirror", owner_id=user.id)
    db.add(workspace)
    db.flush()
    file = File(
        uri="docs/a.txt",
        name="a.txt",
        owner_id=user.id,
        workspace_id=workspace.id,
        is_directory=False,
        size=1,
        processing_status=ProcessingStatus.completed,
    )
    db.add(file)
    db.flush()
    current = _generation(
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", IndexGenerationState.ACTIVE
    )
    previous = _generation(
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", IndexGenerationState.RETIRED
    )
    db.add_all([current, previous])
    db.flush()
    db.add(
        IndexGenerationRoute(
            scope="global",
            active_generation_id=current.id,
            previous_generation_id=previous.id,
            route_version=1,
            rollback_deadline=datetime.now(timezone.utc) + timedelta(hours=1),
        )
    )
    row = IndexGenerationFile(
        generation_id=previous.id,
        file_id=file.id,
        workspace_id=workspace.id,
        state=IndexGenerationFileState.SUCCESS.value,
        source_content_hash="a" * 64,
        source_updated_at=datetime.now(timezone.utc),
        expected_chunk_count=1,
        written_chunk_count=1,
        expected_layer_count=1,
        written_layer_count=1,
    )
    db.add(row)
    db.commit()
    revisions = {file.id: "c" * 64}

    class Reader:
        def list_active_source_files(self):
            return (
                db.query(File)
                .filter(File.deleted_at.is_(None), File.is_directory.is_(False))
                .all()
            )

        def get_source_revision(self, file_id):
            item = db.get(File, file_id)
            return GenerationSourceSnapshot(
                file_id=file_id,
                workspace_id=item.workspace_id,
                source_updated_at=item.updated_at,
                source_content_hash=revisions[file_id],
                chunks=(SimpleNamespace(),),
                layers=(("l0", "summary"),),
            )

    service = PreviousGenerationMirrorService(db, Reader())
    task = service.enqueue_file_mirror(file.id, user_id=user.id)
    assert task.task_type == TaskType.MIRROR_PREVIOUS_GENERATION_FILE.value
    assert task.index_generation_id == previous.id
    assert row.state == IndexGenerationFileState.STALE.value
    assert service.calculate_previous_lag() == 1

    row.state = IndexGenerationFileState.SUCCESS.value
    row.source_content_hash = revisions[file.id]
    db.commit()
    assert service.calculate_previous_lag() == 0

    added = File(
        uri="docs/new.txt",
        name="new.txt",
        owner_id=user.id,
        workspace_id=workspace.id,
        is_directory=False,
        size=1,
        processing_status=ProcessingStatus.completed,
    )
    db.add(added)
    db.flush()
    revisions[added.id] = "d" * 64
    db.commit()
    added_task = service.enqueue_file_mirror(added.id, user_id=user.id)
    assert added_task.file_id == added.id
    assert service.calculate_previous_lag() == 1

    added_row = db.get(IndexGenerationFile, (previous.id, added.id))
    added_row.state = IndexGenerationFileState.SUCCESS.value
    db.commit()
    added.deleted_at = datetime.now(timezone.utc)
    db.commit()
    assert service.calculate_previous_lag() == 1
    service.propagate_delete(added.id)
    assert service.calculate_previous_lag() == 0

    db.close()
    Base.metadata.drop_all(engine)
