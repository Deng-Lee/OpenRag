"""Workspace Elasticsearch v2 index lifecycle."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from openrag.search.es_chunk_contract import SCHEMA_VERSION, compute_mapping_hash
from openrag.search.es_chunk_store import (
    EsChunkStore,
    WorkspaceAliasConflictError,
)
from openrag.search.workspace_es_slug import (
    build_workspace_chunks_index_name,
    build_workspace_chunks_physical_index_name,
    build_workspace_chunks_read_alias,
    build_workspace_chunks_write_alias,
)

logger = logging.getLogger(__name__)


class WorkspaceIndexProvisionError(RuntimeError):
    """Raised when a workspace v2 index cannot be provisioned safely."""


class WorkspaceIndexConflictError(WorkspaceIndexProvisionError):
    """Raised when an existing mapping or alias conflicts with the contract."""


class WorkspaceIndexCleanupError(RuntimeError):
    """Raised when workspace search indices cannot be removed."""


@dataclass(frozen=True)
class WorkspaceIndexProvisionReceipt:
    workspace_id: int
    workspace_slug: str
    physical_index: str
    read_alias: str
    write_alias: str
    index_created: bool
    alias_changed: bool
    previous_alias_state: dict
    schema_version: str
    mapping_hash: str


@dataclass(frozen=True)
class WorkspaceIndexDeleteResult:
    deleted_indices: tuple[str, ...]
    missing_indices: tuple[str, ...]


class WorkspaceIndexLifecycle:
    """Provision, compensate, and delete one workspace's search indices."""

    def __init__(self, store: EsChunkStore):
        self.store = store

    def ensure_ready(
        self, *, workspace_id: int, workspace_slug: str
    ) -> WorkspaceIndexProvisionReceipt:
        physical_index = build_workspace_chunks_physical_index_name(
            workspace_slug, workspace_id
        )
        read_alias = build_workspace_chunks_read_alias(
            workspace_slug, workspace_id
        )
        write_alias = build_workspace_chunks_write_alias(
            workspace_slug, workspace_id
        )
        previous_alias_state = self.store.get_alias_state(
            [read_alias, write_alias]
        )
        index_created = False
        alias_attempted = False
        try:
            index_created = self.store.ensure_versioned_index(physical_index)
            alias_attempted = True
            alias_result = self.store.ensure_workspace_aliases(
                read_alias=read_alias,
                write_alias=write_alias,
                target_index=physical_index,
            )
            return WorkspaceIndexProvisionReceipt(
                workspace_id=workspace_id,
                workspace_slug=workspace_slug,
                physical_index=physical_index,
                read_alias=read_alias,
                write_alias=write_alias,
                index_created=index_created,
                alias_changed=alias_result.changed,
                previous_alias_state=previous_alias_state,
                schema_version=SCHEMA_VERSION,
                mapping_hash=compute_mapping_hash(),
            )
        except WorkspaceAliasConflictError as exc:
            if index_created:
                try:
                    self.store.delete_index_if_exists(physical_index)
                except Exception:
                    logger.exception(
                        "Failed to remove index after workspace alias conflict"
                    )
            raise WorkspaceIndexConflictError(str(exc)) from exc
        except Exception as exc:
            if alias_attempted:
                try:
                    self.store.restore_workspace_aliases(previous_alias_state)
                except Exception:
                    logger.exception(
                        "Failed to restore aliases after workspace index provisioning"
                    )
            if index_created:
                try:
                    self.store.delete_index_if_exists(physical_index)
                except Exception:
                    logger.exception(
                        "Failed to remove index after workspace index provisioning"
                    )
            raise WorkspaceIndexProvisionError(str(exc)) from exc

    def rollback_provision(
        self, receipt: WorkspaceIndexProvisionReceipt
    ) -> None:
        if receipt.alias_changed:
            self.store.restore_workspace_aliases(receipt.previous_alias_state)
        if receipt.index_created:
            self.store.delete_index_if_exists(receipt.physical_index)

    def delete_workspace_indices(
        self, *, workspace_id: int, workspace_slug: str
    ) -> WorkspaceIndexDeleteResult:
        names = (
            build_workspace_chunks_index_name(workspace_slug, workspace_id),
            build_workspace_chunks_physical_index_name(
                workspace_slug, workspace_id
            ),
        )
        aliases = (
            build_workspace_chunks_read_alias(workspace_slug, workspace_id),
            build_workspace_chunks_write_alias(workspace_slug, workspace_id),
        )
        try:
            deleted, missing = self.store.delete_indices_if_exist(names)
            self.store.restore_workspace_aliases(
                {alias: {} for alias in aliases}
            )
            alias_state = self.store.get_alias_state(list(aliases))
            if any(alias_state.values()):
                raise RuntimeError("Workspace aliases still exist after cleanup")
        except Exception as exc:
            raise WorkspaceIndexCleanupError(str(exc)) from exc
        return WorkspaceIndexDeleteResult(deleted, missing)
