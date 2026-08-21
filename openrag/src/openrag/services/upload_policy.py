"""Authoritative upload-size policy shared by every ingest path."""

from fastapi import HTTPException, UploadFile, status

from openrag.config import get_config


def get_max_upload_size() -> int:
    """Return the configured maximum file payload size in bytes."""
    return get_config().max_upload_size


def validate_upload_size(file_size: int) -> None:
    """Reject a file payload that exceeds the configured maximum."""
    max_size = get_max_upload_size()
    if file_size > max_size:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=(
                "File size exceeds maximum allowed size of "
                f"{max_size / 1024 / 1024}MB"
            ),
        )


async def read_upload_content(file: UploadFile) -> bytes:
    """Read at most one byte beyond the limit, then enforce the policy."""
    content = await file.read(get_max_upload_size() + 1)
    validate_upload_size(len(content))
    return content
