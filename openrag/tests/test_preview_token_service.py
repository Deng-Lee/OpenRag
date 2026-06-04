from datetime import datetime, timedelta, timezone

import pytest
from jose import jwt

import openrag.config as config_module
from openrag.config import (
    get_config,
    get_preview_public_web_base_url,
    get_preview_token_max_ttl_seconds,
    get_preview_token_ttl_seconds,
)
from openrag.services.preview_token_service import (
    PreviewTokenClaims,
    clamp_preview_ttl,
    create_preview_token,
    decode_preview_token,
)


@pytest.fixture(autouse=True)
def reset_config(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SECRET_KEY", "preview-token-test-secret")
    monkeypatch.delenv("PREVIEW_PUBLIC_WEB_BASE_URL", raising=False)
    monkeypatch.delenv("PREVIEW_TOKEN_TTL_SECONDS", raising=False)
    monkeypatch.delenv("PREVIEW_TOKEN_MAX_TTL_SECONDS", raising=False)
    monkeypatch.setattr(config_module, "_config", None)
    yield
    monkeypatch.setattr(config_module, "_config", None)


def test_preview_config_reads_env_and_trims_base_url(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PREVIEW_PUBLIC_WEB_BASE_URL", "https://openrag.example.com///")
    monkeypatch.setenv("PREVIEW_TOKEN_TTL_SECONDS", "600")
    monkeypatch.setenv("PREVIEW_TOKEN_MAX_TTL_SECONDS", "1200")
    monkeypatch.setattr(config_module, "_config", None)

    assert get_preview_public_web_base_url() == "https://openrag.example.com"
    assert get_preview_token_ttl_seconds() == 600
    assert get_preview_token_max_ttl_seconds() == 1200


def test_preview_config_defaults():
    assert get_preview_public_web_base_url() == ""
    assert get_preview_token_ttl_seconds() == 900
    assert get_preview_token_max_ttl_seconds() == 1800


def test_clamp_preview_ttl_defaults_and_caps_at_max():
    assert clamp_preview_ttl(None) == 900
    assert clamp_preview_ttl(600) == 600
    assert clamp_preview_ttl(9999) == 1800


def test_create_and_decode_preview_token_binds_document_chunk_claims():
    token, expires_at = create_preview_token(
        workspace_id=7,
        file_id=9,
        chunk_id="chunk-42",
        chunk_index=42,
        ttl_seconds=None,
    )

    claims = decode_preview_token(token)

    assert isinstance(claims, PreviewTokenClaims)
    assert claims.scope == "document_preview"
    assert claims.workspace_id == 7
    assert claims.file_id == 9
    assert claims.chunk_id == "chunk-42"
    assert claims.chunk_index == 42
    assert claims.exp - claims.iat == 900
    assert expires_at == datetime.fromtimestamp(claims.exp, tz=timezone.utc)
    assert not hasattr(claims, "external_user_id")
    assert not hasattr(claims, "external_request_id")


def test_create_preview_token_caps_requested_ttl_at_max():
    token, expires_at = create_preview_token(
        workspace_id=1,
        file_id=2,
        chunk_id="chunk-oversized-ttl",
        chunk_index=None,
        ttl_seconds=9999,
    )

    claims = decode_preview_token(token)

    assert claims.chunk_index is None
    assert claims.exp - claims.iat == 1800
    assert expires_at == datetime.fromtimestamp(claims.exp, tz=timezone.utc)


def test_decode_preview_token_rejects_expired_token():
    now = datetime.now(timezone.utc)
    payload = {
        "scope": "document_preview",
        "workspace_id": 7,
        "file_id": 9,
        "chunk_id": "chunk-expired",
        "chunk_index": 1,
        "iat": int((now - timedelta(minutes=20)).timestamp()),
        "exp": int((now - timedelta(minutes=1)).timestamp()),
    }
    config = get_config()
    token = jwt.encode(payload, config.security.secret_key, algorithm=config.security.algorithm)

    with pytest.raises(ValueError, match="expired"):
        decode_preview_token(token)


def test_decode_preview_token_rejects_wrong_scope():
    now = datetime.now(timezone.utc)
    payload = {
        "scope": "search",
        "workspace_id": 7,
        "file_id": 9,
        "chunk_id": "chunk-wrong-scope",
        "chunk_index": 1,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=10)).timestamp()),
    }
    config = get_config()
    token = jwt.encode(payload, config.security.secret_key, algorithm=config.security.algorithm)

    with pytest.raises(ValueError, match="scope"):
        decode_preview_token(token)


def test_decode_preview_token_rejects_tampered_token():
    token, _ = create_preview_token(
        workspace_id=7,
        file_id=9,
        chunk_id="chunk-42",
        chunk_index=42,
        ttl_seconds=None,
    )
    tampered = token[:-1] + ("a" if token[-1] != "a" else "b")

    with pytest.raises(ValueError, match="invalid"):
        decode_preview_token(tampered)
