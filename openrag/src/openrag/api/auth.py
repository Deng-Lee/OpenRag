"""Authentication middleware and utilities for FastAPI"""

from typing import Optional

from fastapi import HTTPException, status

from openrag.security import verify_token


# Custom Exceptions
class CredentialsException(HTTPException):
    """Exception raised when credentials are invalid"""

    def __init__(self):
        super().__init__(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


class InactiveUserException(HTTPException):
    """Exception raised when user account is inactive"""

    def __init__(self):
        super().__init__(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Inactive user account",
        )


# Token Validation Functions
def validate_token_structure(authorization: Optional[str]) -> str:
    """
    Validate token structure and extract token from Authorization header.

    Args:
        authorization: Authorization header value (e.g., "Bearer <token>")

    Returns:
        Extracted JWT token string

    Raises:
        HTTPException: If token structure is invalid
    """
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header format. Expected: Bearer <token>",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return parts[1]


def validate_token_payload(payload: Optional[dict]) -> dict:
    """
    Validate JWT token payload contains required fields.

    Args:
        payload: Decoded JWT payload

    Returns:
        Validated payload

    Raises:
        HTTPException: If payload is invalid or missing required fields
    """
    if not payload:
        raise CredentialsException()

    if "sub" not in payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token: missing subject claim",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return payload


def extract_user_id_from_payload(payload: dict) -> int:
    """
    Extract and validate user ID from token payload.

    Args:
        payload: Decoded JWT payload

    Returns:
        User ID as integer

    Raises:
        HTTPException: If user ID is invalid or missing
    """
    if "sub" not in payload:
        raise CredentialsException()

    try:
        user_id = int(payload["sub"])
        return user_id
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token: user ID must be an integer",
            headers={"WWW-Authenticate": "Bearer"},
        )


def decode_and_validate_token(token: str) -> dict:
    """
    Decode and validate JWT token.

    Args:
        token: JWT token string

    Returns:
        Decoded and validated payload

    Raises:
        HTTPException: If token is invalid, expired, or malformed
    """
    payload, error = verify_token(token)

    if error:
        if error == "expired":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token has expired",
                headers={"WWW-Authenticate": "Bearer"},
            )
        elif error == "invalid_signature":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token signature",
                headers={"WWW-Authenticate": "Bearer"},
            )
        else:  # malformed
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Malformed token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return validate_token_payload(payload)
