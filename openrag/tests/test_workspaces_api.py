"""Focused tests for Workspace API error mapping."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, status

from openrag.api import workspaces_api
from openrag.services.workspace_service import (
    WorkspaceNotEmptyError,
    WorkspaceStorageCleanupError,
)


def test_workspace_service_builds_index_lifecycle_for_enabled_legacy(
    monkeypatch
):
    store = object()
    monkeypatch.setattr(
        workspaces_api,
        "get_config",
        lambda: SimpleNamespace(
            elasticsearch=SimpleNamespace(
                enabled=True,
                chunk_index_mode="legacy",
            )
        ),
    )
    monkeypatch.setattr(
        workspaces_api,
        "require_es_chunk_store_from_config",
        lambda: store,
    )

    service = workspaces_api._workspace_service(object())

    assert service.chunk_index_mode == "legacy"
    assert service.index_lifecycle.store is store


def test_delete_workspace_maps_not_empty_to_conflict(monkeypatch):
    class NotEmptyService:
        def delete_workspace(self, workspace_id):
            raise WorkspaceNotEmptyError(
                "Workspace is not empty; delete all files and wait for physical cleanup"
            )

    monkeypatch.setattr(
        workspaces_api,
        "_workspace_service",
        lambda db: NotEmptyService(),
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            workspaces_api.delete_workspace(
                workspace=SimpleNamespace(id=123),
                db=object(),
            )
        )

    assert exc_info.value.status_code == status.HTTP_409_CONFLICT
    assert "not empty" in exc_info.value.detail


def test_delete_workspace_maps_storage_cleanup_failure_to_unavailable(monkeypatch):
    class FailedStorageService:
        def delete_workspace(self, workspace_id):
            raise WorkspaceStorageCleanupError("storage cleanup failed")

    monkeypatch.setattr(
        workspaces_api,
        "_workspace_service",
        lambda db: FailedStorageService(),
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            workspaces_api.delete_workspace(
                workspace=SimpleNamespace(id=123),
                db=object(),
            )
        )

    assert exc_info.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert "storage cleanup failed" in exc_info.value.detail
