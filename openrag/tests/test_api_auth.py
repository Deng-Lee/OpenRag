"""Tests for API authentication middleware"""

import pytest
from datetime import timedelta
from fastapi import HTTPException

from openrag.security import create_access_token


class TestTokenValidation:
    """Test token validation functions"""

    def test_validate_token_structure_valid(self):
        """Test validate_token_structure accepts valid Bearer token"""
        from openrag.api.auth import validate_token_structure

        token = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        result = validate_token_structure(token)
        assert result == "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"

    def test_validate_token_structure_missing_bearer(self):
        """Test validate_token_structure rejects token without Bearer prefix"""
        from openrag.api.auth import validate_token_structure

        with pytest.raises(HTTPException) as exc_info:
            validate_token_structure("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9")

        assert exc_info.value.status_code == 401
        assert "bearer" in exc_info.value.detail.lower()

    def test_validate_token_structure_empty(self):
        """Test validate_token_structure rejects empty token"""
        from openrag.api.auth import validate_token_structure

        with pytest.raises(HTTPException) as exc_info:
            validate_token_structure("")

        assert exc_info.value.status_code == 401

    def test_validate_token_structure_none(self):
        """Test validate_token_structure rejects None token"""
        from openrag.api.auth import validate_token_structure

        with pytest.raises(HTTPException) as exc_info:
            validate_token_structure(None)

        assert exc_info.value.status_code == 401


class TestTokenPayloadValidation:
    """Test token payload validation"""

    def test_validate_token_payload_valid(self):
        """Test validate_token_payload accepts valid payload"""
        from openrag.api.auth import validate_token_payload

        payload = {"sub": "123", "username": "testuser"}
        result = validate_token_payload(payload)
        assert result == payload

    def test_validate_token_payload_missing_sub(self):
        """Test validate_token_payload rejects payload without sub"""
        from openrag.api.auth import validate_token_payload

        with pytest.raises(HTTPException) as exc_info:
            validate_token_payload({"username": "testuser"})

        assert exc_info.value.status_code == 401
        assert "sub" in exc_info.value.detail.lower() or "invalid" in exc_info.value.detail.lower()

    def test_validate_token_payload_empty(self):
        """Test validate_token_payload rejects empty payload"""
        from openrag.api.auth import validate_token_payload

        with pytest.raises(HTTPException) as exc_info:
            validate_token_payload({})

        assert exc_info.value.status_code == 401

    def test_validate_token_payload_none(self):
        """Test validate_token_payload rejects None payload"""
        from openrag.api.auth import validate_token_payload

        with pytest.raises(HTTPException) as exc_info:
            validate_token_payload(None)

        assert exc_info.value.status_code == 401


class TestAuthenticationExceptions:
    """Test authentication exception classes"""

    def test_credentials_exception(self):
        """Test CredentialsException is properly defined"""
        from openrag.api.auth import CredentialsException

        exc = CredentialsException()
        assert exc.status_code == 401
        assert "authenticate" in exc.detail.lower() or "credentials" in exc.detail.lower()
        assert exc.headers == {"WWW-Authenticate": "Bearer"}

    def test_inactive_user_exception(self):
        """Test InactiveUserException is properly defined"""
        from openrag.api.auth import InactiveUserException

        exc = InactiveUserException()
        assert exc.status_code == 403
        assert "inactive" in exc.detail.lower()


class TestAuthenticationHelpers:
    """Test authentication helper functions"""

    def test_extract_user_id_from_payload(self):
        """Test extract_user_id_from_payload extracts integer user ID"""
        from openrag.api.auth import extract_user_id_from_payload

        payload = {"sub": "123", "username": "testuser"}
        user_id = extract_user_id_from_payload(payload)
        assert user_id == 123
        assert isinstance(user_id, int)

    def test_extract_user_id_from_payload_invalid_format(self):
        """Test extract_user_id_from_payload handles invalid format"""
        from openrag.api.auth import extract_user_id_from_payload

        with pytest.raises(HTTPException) as exc_info:
            extract_user_id_from_payload({"sub": "not_a_number"})

        assert exc_info.value.status_code == 401

    def test_extract_user_id_from_payload_missing_sub(self):
        """Test extract_user_id_from_payload handles missing sub"""
        from openrag.api.auth import extract_user_id_from_payload

        with pytest.raises(HTTPException) as exc_info:
            extract_user_id_from_payload({"username": "testuser"})

        assert exc_info.value.status_code == 401
