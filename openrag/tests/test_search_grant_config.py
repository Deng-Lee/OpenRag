import pytest
from pydantic import SecretStr, ValidationError

import openrag.config as config_module
from openrag.config import Config, SearchGrantConfig


def test_search_grant_config_defaults_disabled():
    config = SearchGrantConfig()

    assert config.enabled is False
    assert config.ttl_seconds == 60
    assert config.clock_skew_seconds == 5
    assert config.max_scopes_per_issue == 20
    assert config.max_grants_per_search == 20


@pytest.mark.parametrize(
    "kwargs",
    [
        {"enabled": True, "signing_key": SecretStr("x" * 32)},
        {"enabled": True, "instance_id": "openrag-a"},
        {
            "enabled": True,
            "instance_id": "openrag-a",
            "signing_key": SecretStr("too-short"),
        },
        {
            "enabled": True,
            "instance_id": "openrag-a",
            "signing_key": SecretStr("x" * 32),
            "previous_signing_key": SecretStr("too-short"),
        },
        {
            "enabled": True,
            "instance_id": "openrag-a",
            "signing_key": SecretStr("x" * 32),
            "previous_signing_key": SecretStr("x" * 32),
        },
    ],
)
def test_search_grant_config_requires_identity_and_key_when_enabled(kwargs):
    with pytest.raises(ValidationError):
        SearchGrantConfig(**kwargs)


def test_search_grant_config_rejects_non_hs256_algorithm():
    with pytest.raises(ValidationError):
        SearchGrantConfig(algorithm="HS512")


def test_root_config_reads_flat_search_grant_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SEARCH_GRANT_ENABLED", "true")
    monkeypatch.setenv("OPENRAG_INSTANCE_ID", "openrag-env")
    monkeypatch.setenv("SEARCH_GRANT_SIGNING_KEY", "e" * 32)
    monkeypatch.setattr(config_module, "_config", None)

    config = Config()

    assert config.search_grant.enabled is True
    assert config.search_grant.instance_id == "openrag-env"
    assert config.search_grant.signing_key.get_secret_value() == "e" * 32
