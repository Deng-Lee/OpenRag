"""Issue and validate short-lived grants for federated search."""

from __future__ import annotations

import hashlib
import secrets
import time
import uuid
from typing import Literal, Sequence

from fastapi import HTTPException
from jose import ExpiredSignatureError, JWTError, jwt
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from openrag.config import SearchGrantConfig, get_config
from openrag.services.workspace_file_tree import normalize_scope_paths

SEARCH_GRANT_AUDIENCE = "openrag.federated-search"
SEARCH_GRANT_ACTION = "search"


class SearchGrantError(ValueError):
    """Stable, non-secret grant validation failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class SearchGrantClaims(BaseModel):
    model_config = ConfigDict(extra="forbid")

    iss: str
    aud: str
    instance_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    workspace_id: int = Field(gt=0)
    workspace_name: str = Field(min_length=1, max_length=255)
    allowed_paths: tuple[str, ...] | None
    actions: tuple[Literal["search"], ...]
    scope_ref: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    request_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    query_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    max_top_k: int = Field(gt=0, le=100)
    jti: str = Field(min_length=1, max_length=64)
    iat: int
    exp: int


def hash_search_query(query: str) -> str:
    digest = hashlib.sha256(query.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def path_is_within(uri: str, prefix: str) -> bool:
    normalized_prefix = prefix.rstrip("/") or "/"
    if normalized_prefix == "/":
        return True
    return uri == normalized_prefix or uri.startswith(normalized_prefix + "/")


def normalize_grant_paths(paths: Sequence[str] | None) -> tuple[str, ...] | None:
    """Normalize an explicit grant scope without treating ``[]`` as root access."""
    if paths is None:
        return None
    for raw in paths:
        if "\\" in str(raw):
            raise ValueError("grant paths must use forward slashes")
        if any(segment == ".." for segment in str(raw).replace("\\", "/").split("/")):
            raise ValueError("grant path traversal is not allowed")
    try:
        normalized = normalize_scope_paths(list(paths))
    except HTTPException as exc:
        raise ValueError("invalid grant path") from exc
    if normalized is None:
        return None
    if "/" in normalized:
        raise ValueError("root scope must use allowed_paths=null")

    compact: list[str] = []
    for path in normalized:
        if any(path_is_within(path, parent) for parent in compact):
            continue
        compact = [existing for existing in compact if not path_is_within(existing, path)]
        compact.append(path)
    return tuple(compact)


def _search_grant_config(config: SearchGrantConfig | None) -> SearchGrantConfig:
    resolved = config or get_config().search_grant
    if not resolved.enabled:
        raise SearchGrantError("search_grants_disabled", "search grants are disabled")
    return resolved


def _signing_key(config: SearchGrantConfig) -> str:
    if config.signing_key is None:
        raise SearchGrantError("search_grants_disabled", "search grants are not configured")
    return config.signing_key.get_secret_value()


def _verification_keys(config: SearchGrantConfig) -> tuple[str, ...]:
    keys = [_signing_key(config)]
    if config.previous_signing_key is not None:
        keys.append(config.previous_signing_key.get_secret_value())
    return tuple(keys)


def create_search_grant(
    *,
    workspace_id: int,
    workspace_name: str,
    allowed_paths: Sequence[str] | None,
    scope_ref: str,
    request_id: str,
    query_hash: str,
    max_top_k: int,
    config: SearchGrantConfig | None = None,
    now: int | None = None,
) -> tuple[str, int]:
    resolved = _search_grant_config(config)
    issued_at = int(time.time()) if now is None else now
    expires_at = issued_at + resolved.ttl_seconds
    instance_id = resolved.instance_id or ""
    claims = SearchGrantClaims(
        iss=instance_id,
        aud=SEARCH_GRANT_AUDIENCE,
        instance_id=instance_id,
        workspace_id=workspace_id,
        workspace_name=workspace_name,
        allowed_paths=normalize_grant_paths(allowed_paths),
        actions=(SEARCH_GRANT_ACTION,),
        scope_ref=scope_ref,
        request_id=request_id,
        query_hash=query_hash,
        max_top_k=max_top_k,
        jti=uuid.uuid4().hex,
        iat=issued_at,
        exp=expires_at,
    )
    token = jwt.encode(
        claims.model_dump(mode="json"),
        _signing_key(resolved),
        algorithm=resolved.algorithm,
    )
    return token, expires_at


def decode_search_grant(
    token: str,
    *,
    config: SearchGrantConfig | None = None,
    now: int | None = None,
) -> SearchGrantClaims:
    resolved = _search_grant_config(config)
    payload = None
    decode_error: JWTError | None = None
    for verification_key in _verification_keys(resolved):
        try:
            payload = jwt.decode(
                token,
                verification_key,
                algorithms=[resolved.algorithm],
                audience=SEARCH_GRANT_AUDIENCE,
                options={"verify_exp": False},
            )
            break
        except ExpiredSignatureError as exc:
            raise SearchGrantError("grant_expired", "search grant expired") from exc
        except JWTError as exc:
            decode_error = exc
    if payload is None:
        raise SearchGrantError("grant_invalid", "invalid search grant") from decode_error

    try:
        claims = SearchGrantClaims.model_validate(payload)
    except ValidationError as exc:
        raise SearchGrantError("grant_invalid", "invalid search grant claims") from exc

    instance_id = resolved.instance_id or ""
    if claims.iss != claims.instance_id or claims.instance_id != instance_id:
        raise SearchGrantError(
            "grant_instance_mismatch", "search grant belongs to another instance"
        )
    if claims.actions != (SEARCH_GRANT_ACTION,):
        raise SearchGrantError("grant_invalid", "invalid search grant action")
    effective_now = int(time.time()) if now is None else now
    if claims.exp < effective_now - resolved.clock_skew_seconds:
        raise SearchGrantError("grant_expired", "search grant expired")
    if claims.iat > effective_now + resolved.clock_skew_seconds:
        raise SearchGrantError("grant_invalid", "search grant issued in the future")
    if claims.exp <= claims.iat or claims.exp - claims.iat > resolved.ttl_seconds:
        raise SearchGrantError("grant_invalid", "invalid search grant lifetime")
    try:
        normalized_paths = normalize_grant_paths(claims.allowed_paths)
    except ValueError as exc:
        raise SearchGrantError("grant_invalid", "invalid search grant paths") from exc
    if normalized_paths != claims.allowed_paths:
        raise SearchGrantError("grant_invalid", "non-canonical search grant paths")
    return claims


def validate_search_grant_context(
    claims: SearchGrantClaims,
    *,
    request_id: str,
    query: str,
    top_k: int,
) -> None:
    if not secrets.compare_digest(claims.request_id, request_id):
        raise SearchGrantError(
            "grant_context_mismatch", "search grant request context mismatch"
        )
    if not secrets.compare_digest(claims.query_hash, hash_search_query(query)):
        raise SearchGrantError(
            "grant_context_mismatch", "search grant query context mismatch"
        )
    if top_k > claims.max_top_k:
        raise SearchGrantError(
            "grant_context_mismatch", "search top_k exceeds grant limit"
        )


def unverified_scope_ref(token: str) -> str | None:
    """Read only an error-label candidate; never use this value for authorization."""
    try:
        value = jwt.get_unverified_claims(token).get("scope_ref")
    except JWTError:
        return None
    if not isinstance(value, str) or not value or len(value) > 128:
        return None
    return value
