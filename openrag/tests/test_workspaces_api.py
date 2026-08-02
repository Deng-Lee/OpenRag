"""Focused tests for Workspace API error mapping."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, status

from openrag.api import workspaces_api
from openrag.services.workspace_service import WorkspaceNotEmptyError


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
