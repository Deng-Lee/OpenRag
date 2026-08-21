import asyncio
import io

import pytest
from fastapi import HTTPException, UploadFile
from pydantic import ValidationError

from openrag.config import Config
from openrag.services import upload_policy


def test_max_upload_size_defaults_to_100_mib(monkeypatch):
    monkeypatch.delenv("MAX_UPLOAD_SIZE", raising=False)

    config = Config(_env_file=None)

    assert config.max_upload_size == 100 * 1024 * 1024


def test_max_upload_size_reads_environment(monkeypatch):
    monkeypatch.setenv("MAX_UPLOAD_SIZE", "52428800")

    config = Config(_env_file=None)

    assert config.max_upload_size == 50 * 1024 * 1024


@pytest.mark.parametrize("value", ["0", "-1", "not-a-number"])
def test_max_upload_size_rejects_invalid_values(monkeypatch, value):
    monkeypatch.setenv("MAX_UPLOAD_SIZE", value)

    with pytest.raises(ValidationError):
        Config(_env_file=None)


def test_read_upload_content_reads_only_one_byte_past_limit(monkeypatch):
    monkeypatch.setattr(upload_policy, "get_max_upload_size", lambda: 8)
    upload = UploadFile(filename="large.txt", file=io.BytesIO(b"x" * 100))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(upload_policy.read_upload_content(upload))

    assert exc_info.value.status_code == 413
    assert upload.file.tell() == 9
