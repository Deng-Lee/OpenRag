"""Preview token signing and verification."""

from datetime import datetime, timezone

from jose import ExpiredSignatureError, JWTError, jwt
from pydantic import BaseModel, ValidationError

from openrag.config import (
    get_config,
    get_preview_token_max_ttl_seconds,
    get_preview_token_ttl_seconds,
)


PREVIEW_TOKEN_SCOPE = "document_preview"


class PreviewTokenClaims(BaseModel):
    scope: str
    workspace_id: int
    file_id: int
    chunk_id: str
    chunk_index: int | None = None
    iat: int
    exp: int


def clamp_preview_ttl(ttl_seconds: int | None) -> int:
    ttl = get_preview_token_ttl_seconds() if ttl_seconds is None else ttl_seconds
    max_ttl = get_preview_token_max_ttl_seconds()
    if ttl > max_ttl:
        return max_ttl
    return ttl


def create_preview_token(
    *,
    workspace_id: int,
    file_id: int,
    chunk_id: str,
    chunk_index: int | None,
    ttl_seconds: int | None,
) -> tuple[str, datetime]:
    config = get_config()
    issued_at = int(datetime.now(timezone.utc).timestamp())
    expires_at_ts = issued_at + clamp_preview_ttl(ttl_seconds)
    expires_at = datetime.fromtimestamp(expires_at_ts, tz=timezone.utc)
    payload = {
        "scope": PREVIEW_TOKEN_SCOPE,
        "workspace_id": workspace_id,
        "file_id": file_id,
        "chunk_id": chunk_id,
        "chunk_index": chunk_index,
        "iat": issued_at,
        "exp": expires_at_ts,
    }
    token = jwt.encode(
        payload,
        config.security.secret_key,
        algorithm=config.security.algorithm,
    )
    return token, expires_at


def decode_preview_token(token: str) -> PreviewTokenClaims:
    config = get_config()
    try:
        payload = jwt.decode(
            token,
            config.security.secret_key,
            algorithms=[config.security.algorithm],
        )
    except ExpiredSignatureError as exc:
        raise ValueError("preview token expired") from exc
    except JWTError as exc:
        raise ValueError("invalid preview token") from exc

    try:
        claims = PreviewTokenClaims.model_validate(payload)
    except ValidationError as exc:
        raise ValueError("invalid preview token claims") from exc

    if claims.scope != PREVIEW_TOKEN_SCOPE:
        raise ValueError("invalid preview token scope")
    return claims
