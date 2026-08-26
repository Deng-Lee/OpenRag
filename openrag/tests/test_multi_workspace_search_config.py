import pytest
from pydantic import ValidationError

from openrag.config import MultiWorkspaceSearchConfig


def test_multi_workspace_search_config_defaults_match_protocol() -> None:
    config = MultiWorkspaceSearchConfig()

    assert config.max_workspaces == 20
    assert config.recall_concurrency == 10
    assert config.max_global_rerank_candidates == 1000


def test_multi_workspace_search_config_rejects_protocol_limit_above_twenty() -> None:
    with pytest.raises(ValidationError):
        MultiWorkspaceSearchConfig(max_workspaces=21)


def test_multi_workspace_search_config_requires_global_candidate_coverage() -> None:
    with pytest.raises(ValidationError):
        MultiWorkspaceSearchConfig(
            max_workspaces=20,
            max_global_rerank_candidates=19,
        )
