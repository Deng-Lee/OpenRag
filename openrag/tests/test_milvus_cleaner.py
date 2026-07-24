"""Physical Collection deletion remains behind explicit generation guards."""

from datetime import datetime, timedelta, timezone

import pytest

from openrag.indexing.milvus_cleaner import CleanupGuardError, MilvusCollectionCleaner


class FakeCleanupBackend:
    def __init__(self):
        self.aliases = {}
        self.dropped = []

    def aliases_for(self, name):
        return tuple(self.aliases.get(name, ()))

    def drop_collection(self, name):
        self.dropped.append(name)


def generation(state="retired", delete_after=None):
    return {
        "id": "generation-a",
        "state": state,
        "chunk_collection_name": "chunks_a",
        "layer_collection_name": "layers_a",
        "delete_after": delete_after
        or datetime.now(timezone.utc) - timedelta(minutes=1),
    }


@pytest.mark.parametrize("state", ["active", "ready", "building"])
def test_cleaner_rejects_non_retired_generation(state):
    cleaner = MilvusCollectionCleaner(FakeCleanupBackend())

    with pytest.raises(CleanupGuardError):
        cleaner.assert_deletable(
            generation(state=state), active_id=None, previous_id=None
        )


def test_cleaner_rejects_route_or_alias_targets():
    backend = FakeCleanupBackend()
    backend.aliases["chunks_a"] = ("openrag_chunks_active",)
    cleaner = MilvusCollectionCleaner(backend)

    with pytest.raises(CleanupGuardError):
        cleaner.assert_deletable(generation(), active_id=None, previous_id=None)
    with pytest.raises(CleanupGuardError):
        cleaner.assert_deletable(
            generation(), active_id="generation-a", previous_id=None
        )


def test_cleaner_requires_expired_retention_and_exact_confirmation():
    backend = FakeCleanupBackend()
    cleaner = MilvusCollectionCleaner(backend)
    future = datetime.now(timezone.utc) + timedelta(hours=1)

    with pytest.raises(CleanupGuardError):
        cleaner.drop_generation_collections(
            generation(delete_after=future),
            active_id=None,
            previous_id=None,
            confirmation="generation-a",
        )
    with pytest.raises(CleanupGuardError):
        cleaner.drop_generation_collections(
            generation(), active_id=None, previous_id=None, confirmation="wrong"
        )

    assert backend.dropped == []


def test_cleaner_drops_only_guarded_generation_collections():
    backend = FakeCleanupBackend()
    cleaner = MilvusCollectionCleaner(backend)

    cleaner.drop_generation_collections(
        generation(), active_id=None, previous_id=None, confirmation="generation-a"
    )

    assert backend.dropped == ["chunks_a", "layers_a"]
