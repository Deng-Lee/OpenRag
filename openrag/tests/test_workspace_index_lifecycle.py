from copy import deepcopy

import pytest

from openrag.search.workspace_index_lifecycle import (
    WorkspaceIndexConflictError,
    WorkspaceIndexLifecycle,
)


class FakeStore:
    def __init__(self):
        self.indices = set()
        self.aliases = {}
        self.deleted = []
        self.fail_aliases = False
        self.conflict = False

    def get_alias_state(self, aliases):
        return {
            alias: deepcopy(self.aliases.get(alias, {}))
            for alias in aliases
        }

    def ensure_versioned_index(self, index_name):
        created = index_name not in self.indices
        self.indices.add(index_name)
        return created

    def ensure_workspace_aliases(
        self, *, read_alias, write_alias, target_index
    ):
        from openrag.search.es_chunk_store import (
            AliasEnsureResult,
            WorkspaceAliasConflictError,
        )

        if self.conflict:
            raise WorkspaceAliasConflictError("unexpected targets")
        if self.fail_aliases:
            raise RuntimeError("alias update failed")
        previous = self.get_alias_state([read_alias, write_alias])
        changed = not previous[read_alias] or not previous[write_alias]
        self.aliases[read_alias] = {target_index: {}}
        self.aliases[write_alias] = {
            target_index: {"is_write_index": True}
        }
        return AliasEnsureResult(
            changed=changed,
            previous_state=previous,
            final_state=self.get_alias_state([read_alias, write_alias]),
        )

    def restore_workspace_aliases(self, state):
        self.aliases = {
            alias: deepcopy(targets) for alias, targets in state.items()
        }

    def delete_index_if_exists(self, index_name):
        if index_name not in self.indices:
            return False
        self.indices.remove(index_name)
        self.deleted.append(index_name)
        for alias in list(self.aliases):
            self.aliases[alias].pop(index_name, None)
            if not self.aliases[alias]:
                del self.aliases[alias]
        return True

    def delete_indices_if_exist(self, index_names):
        deleted = tuple(name for name in index_names if name in self.indices)
        missing = tuple(name for name in index_names if name not in self.indices)
        for name in deleted:
            self.delete_index_if_exists(name)
        return deleted, missing


def test_workspace_index_lifecycle_provisions_idempotently():
    store = FakeStore()
    lifecycle = WorkspaceIndexLifecycle(store)

    first = lifecycle.ensure_ready(workspace_id=7, workspace_slug="demo")
    second = lifecycle.ensure_ready(workspace_id=7, workspace_slug="demo")

    assert first.index_created is True
    assert first.alias_changed is True
    assert second.index_created is False
    assert second.alias_changed is False
    assert store.aliases[first.read_alias] == {first.physical_index: {}}
    assert store.aliases[first.write_alias] == {
        first.physical_index: {"is_write_index": True}
    }


def test_workspace_index_lifecycle_cleans_new_index_on_alias_failure():
    store = FakeStore()
    store.fail_aliases = True
    lifecycle = WorkspaceIndexLifecycle(store)

    with pytest.raises(RuntimeError, match="alias update failed"):
        lifecycle.ensure_ready(workspace_id=7, workspace_slug="demo")

    assert store.indices == set()
    assert store.aliases == {}


def test_workspace_index_lifecycle_does_not_overwrite_conflicts():
    store = FakeStore()
    store.conflict = True

    with pytest.raises(WorkspaceIndexConflictError, match="unexpected"):
        WorkspaceIndexLifecycle(store).ensure_ready(
            workspace_id=7, workspace_slug="demo"
        )

    assert store.aliases == {}


def test_workspace_index_lifecycle_rolls_back_only_its_changes():
    store = FakeStore()
    lifecycle = WorkspaceIndexLifecycle(store)
    receipt = lifecycle.ensure_ready(workspace_id=7, workspace_slug="demo")

    lifecycle.rollback_provision(receipt)

    assert receipt.physical_index not in store.indices
    assert store.aliases == {}


def test_workspace_index_lifecycle_deletes_legacy_and_v2_indices():
    store = FakeStore()
    lifecycle = WorkspaceIndexLifecycle(store)
    receipt = lifecycle.ensure_ready(workspace_id=7, workspace_slug="demo")
    legacy = receipt.physical_index.removesuffix("_v2")
    store.indices.add(legacy)

    result = lifecycle.delete_workspace_indices(
        workspace_id=7, workspace_slug="demo"
    )

    assert set(result.deleted_indices) == {legacy, receipt.physical_index}
    assert store.indices == set()
