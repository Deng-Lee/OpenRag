from __future__ import annotations

import pytest
from jose import jwt
from pydantic import SecretStr

from openrag.config import SearchGrantConfig
from openrag.services.search_grant_service import (
    SEARCH_GRANT_AUDIENCE,
    SearchGrantError,
    create_search_grant,
    decode_search_grant,
    hash_search_query,
    normalize_grant_paths,
    validate_search_grant_context,
)


def _config(
    *,
    instance_id: str = "openrag-a",
    signing_key: str = "grant-secret-a-32-characters-long",
):
    return SearchGrantConfig(
        enabled=True,
        instance_id=instance_id,
        signing_key=SecretStr(signing_key),
        ttl_seconds=60,
        clock_skew_seconds=0,
    )


def _grant(config: SearchGrantConfig | None = None, *, now: int = 1000):
    return create_search_grant(
        workspace_id=7,
        workspace_name="Personal",
        allowed_paths=["/users/u/kb-a"],
        scope_ref="kb-a",
        request_id="request-1",
        query_hash=hash_search_query("policy"),
        max_top_k=10,
        config=config or _config(),
        now=now,
    )


def test_create_decode_and_validate_search_grant_context():
    token, expires_at = _grant()

    claims = decode_search_grant(token, config=_config(), now=1000)
    validate_search_grant_context(
        claims,
        request_id="request-1",
        query="policy",
        top_k=10,
    )

    assert claims.scope_ref == "kb-a"
    assert claims.allowed_paths == ("/users/u/kb-a",)
    assert claims.aud == SEARCH_GRANT_AUDIENCE
    assert expires_at == 1060


@pytest.mark.parametrize(
    ("request_id", "query", "top_k"),
    [
        ("request-2", "policy", 10),
        ("request-1", "different", 10),
        ("request-1", "policy", 11),
    ],
)
def test_validate_search_grant_context_rejects_mismatch(request_id, query, top_k):
    token, _ = _grant()
    claims = decode_search_grant(token, config=_config(), now=1000)

    with pytest.raises(SearchGrantError) as exc_info:
        validate_search_grant_context(
            claims,
            request_id=request_id,
            query=query,
            top_k=top_k,
        )

    assert exc_info.value.code == "grant_context_mismatch"


def test_decode_rejects_expired_and_tampered_grants():
    token, _ = _grant()

    with pytest.raises(SearchGrantError) as expired:
        decode_search_grant(token, config=_config(), now=1061)
    assert expired.value.code == "grant_expired"

    header, payload, signature = token.split(".")
    tampered_signature = ("a" if signature[0] != "a" else "b") + signature[1:]
    tampered = ".".join((header, payload, tampered_signature))
    with pytest.raises(SearchGrantError) as invalid:
        decode_search_grant(tampered, config=_config(), now=1000)
    assert invalid.value.code == "grant_invalid"


def test_decode_rejects_grant_from_another_instance():
    shared_key = "shared-search-grant-key-32-chars"
    token, _ = _grant(_config(instance_id="openrag-a", signing_key=shared_key))

    with pytest.raises(SearchGrantError) as exc_info:
        decode_search_grant(
            token,
            config=_config(instance_id="openrag-b", signing_key=shared_key),
            now=1000,
        )

    assert exc_info.value.code == "grant_instance_mismatch"


def test_decode_accepts_previous_key_only_during_rotation_window():
    old_key = "old-search-grant-key-32-characters"
    new_key = "new-search-grant-key-32-characters"
    token, _ = _grant(_config(signing_key=old_key))
    rotating_config = SearchGrantConfig(
        enabled=True,
        instance_id="openrag-a",
        signing_key=SecretStr(new_key),
        previous_signing_key=SecretStr(old_key),
        ttl_seconds=60,
        clock_skew_seconds=0,
    )

    assert decode_search_grant(token, config=rotating_config, now=1000).scope_ref == "kb-a"

    with pytest.raises(SearchGrantError) as exc_info:
        decode_search_grant(
            token,
            config=_config(signing_key=new_key),
            now=1000,
        )
    assert exc_info.value.code == "grant_invalid"


def test_decode_rejects_wrong_audience_and_action():
    config = _config()
    payload = {
        "iss": "openrag-a",
        "aud": "wrong-audience",
        "instance_id": "openrag-a",
        "workspace_id": 7,
        "workspace_name": "Personal",
        "allowed_paths": None,
        "actions": ["write"],
        "scope_ref": "kb-a",
        "request_id": "request-1",
        "query_hash": hash_search_query("policy"),
        "max_top_k": 10,
        "jti": "jti-1",
        "iat": 1000,
        "exp": 1060,
    }
    token = jwt.encode(
        payload,
        config.signing_key.get_secret_value(),
        algorithm=config.algorithm,
    )

    with pytest.raises(SearchGrantError) as exc_info:
        decode_search_grant(token, config=config, now=1000)

    assert exc_info.value.code == "grant_invalid"


def test_normalize_grant_paths_preserves_null_and_empty_semantics():
    assert normalize_grant_paths(None) is None
    assert normalize_grant_paths([]) == ()
    assert normalize_grant_paths(["/a", "/a/b", "/c/"]) == ("/a", "/c")


@pytest.mark.parametrize("paths", [["/"], ["/a/../b"], [" "], [r"\users\u"]])
def test_normalize_grant_paths_rejects_invalid_explicit_root_or_path(paths):
    with pytest.raises(ValueError):
        normalize_grant_paths(paths)
