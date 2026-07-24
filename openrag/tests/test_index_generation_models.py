"""Index generation registry constraints and lifecycle contracts."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from openrag.models import Base
from openrag.models.index_generation import (
    IndexGeneration,
    IndexGenerationFile,
    IndexGenerationFileState,
    IndexGenerationState,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def generation(generation_id: str, *, state: IndexGenerationState) -> IndexGeneration:
    return IndexGeneration(
        id=generation_id,
        scope="global",
        state=state.value,
        embedding_provider="openai",
        embedding_model="embedding-model",
        embedding_revision="revision-1",
        embedding_dimension=3,
        embedding_fingerprint=(generation_id[0] * 64),
        embedding_config_ref="embedding/default",
        vector_normalization="none",
        distance_metric="COSINE",
        schema_version=1,
        chunk_policy_revision="chunks-v1",
        hierarchy_policy_revision="hierarchy-v1",
        chunk_collection_name=f"chunks_{generation_id}",
        layer_collection_name=f"layers_{generation_id}",
        manifest={"generation_id": generation_id},
    )


def test_same_scope_allows_only_one_active_generation(db):
    db.add_all(
        [
            generation(
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                state=IndexGenerationState.ACTIVE,
            ),
            generation(
                "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                state=IndexGenerationState.ACTIVE,
            ),
        ]
    )

    with pytest.raises(IntegrityError):
        db.commit()


def test_generation_file_is_unique_per_generation(db):
    item = generation(
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", state=IndexGenerationState.DRAFT
    )
    db.add(item)
    db.flush()
    now = datetime.now(timezone.utc)
    db.add_all(
        [
            IndexGenerationFile(
                generation_id=item.id,
                file_id=7,
                workspace_id=3,
                state=IndexGenerationFileState.PENDING.value,
                source_updated_at=now,
            ),
            IndexGenerationFile(
                generation_id=item.id,
                file_id=7,
                workspace_id=3,
                state=IndexGenerationFileState.PENDING.value,
                source_updated_at=now,
            ),
        ]
    )

    with pytest.raises(IntegrityError):
        db.commit()


def test_generation_counts_must_be_non_negative(db):
    item = generation(
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", state=IndexGenerationState.DRAFT
    )
    item.expected_file_count = -1
    db.add(item)

    with pytest.raises(IntegrityError):
        db.commit()
