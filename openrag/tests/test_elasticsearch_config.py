import pytest
from pydantic import ValidationError

from openrag.config import Config, ElasticsearchConfig


def test_chunk_index_mode_defaults_to_legacy():
    config = ElasticsearchConfig()

    assert config.chunk_index_mode == "legacy"
    assert config.hybrid_recall_mode == "legacy"
    assert config.requires_fulltext_indexing is False


@pytest.mark.parametrize("hybrid_recall_mode", ["legacy", "independent_rrf"])
def test_enabled_elasticsearch_requires_fulltext_indexing(hybrid_recall_mode):
    config = ElasticsearchConfig(
        enabled=True,
        hybrid_recall_mode=hybrid_recall_mode,
        chunk_index_mode="legacy",
    )

    assert config.requires_fulltext_indexing is True


def test_independent_rrf_requires_elasticsearch_enabled():
    with pytest.raises(ValidationError, match="ENABLED=true"):
        ElasticsearchConfig(
            enabled=False,
            hybrid_recall_mode="independent_rrf",
            chunk_index_mode="legacy",
        )


@pytest.mark.parametrize("hybrid_recall_mode", ["legacy", "independent_rrf"])
def test_enabled_elasticsearch_requires_hosts(hybrid_recall_mode):
    with pytest.raises(ValidationError, match="non-empty"):
        ElasticsearchConfig(
            enabled=True,
            hosts=" ",
            hybrid_recall_mode=hybrid_recall_mode,
            chunk_index_mode="legacy",
        )


def test_v2_alias_requires_independent_rrf():
    with pytest.raises(ValidationError, match="independent_rrf"):
        ElasticsearchConfig(
            enabled=True,
            chunk_index_mode="v2_alias",
            hybrid_recall_mode="legacy",
        )

    config = ElasticsearchConfig(
        enabled=True,
        chunk_index_mode="v2_alias",
        hybrid_recall_mode="independent_rrf",
    )
    assert config.chunk_index_mode == "v2_alias"


def test_v2_alias_requires_elasticsearch_enabled_and_hosts():
    with pytest.raises(ValidationError, match="ENABLED=true"):
        ElasticsearchConfig(
            enabled=False,
            chunk_index_mode="v2_alias",
            hybrid_recall_mode="independent_rrf",
        )

    with pytest.raises(ValidationError, match="non-empty"):
        ElasticsearchConfig(
            enabled=True,
            hosts=" ",
            chunk_index_mode="v2_alias",
            hybrid_recall_mode="independent_rrf",
        )


def test_nested_environment_parses_both_elasticsearch_modes(monkeypatch):
    monkeypatch.setenv("ELASTICSEARCH__ENABLED", "true")
    monkeypatch.setenv("ELASTICSEARCH__CHUNK_INDEX_MODE", "v2_alias")
    monkeypatch.setenv("ELASTICSEARCH__HYBRID_RECALL_MODE", "independent_rrf")

    config = Config()

    assert config.elasticsearch.chunk_index_mode == "v2_alias"
    assert config.elasticsearch.hybrid_recall_mode == "independent_rrf"
