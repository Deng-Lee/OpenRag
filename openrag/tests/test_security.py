"""安全模块测试"""

import time
from datetime import timedelta

import pytest

from openrag.security import (
    create_access_token,
    hash_password,
    verify_password,
    verify_token,
)


class TestPasswordHashing:
    """密码哈希测试"""

    def test_hash_password(self):
        """测试密码哈希"""
        password = "test_password_123"
        hashed = hash_password(password)

        # 验证哈希后的密码不等于原密码
        assert hashed != password
        # 验证哈希后的密码是字符串
        assert isinstance(hashed, str)
        # 验证哈希后的密码长度合理（bcrypt哈希通常是60字符）
        assert len(hashed) == 60

    def test_verify_password_success(self):
        """测试密码验证成功"""
        password = "correct_password"
        hashed = hash_password(password)

        # 验证正确的密码
        assert verify_password(password, hashed) is True

    def test_verify_password_failure(self):
        """测试密码验证失败"""
        password = "correct_password"
        wrong_password = "wrong_password"
        hashed = hash_password(password)

        # 验证错误的密码
        assert verify_password(wrong_password, hashed) is False

    def test_hash_password_empty_string(self):
        """测试空字符串密码"""
        with pytest.raises(ValueError, match="Password cannot be empty or None"):
            hash_password("")

    def test_hash_password_none(self):
        """测试None密码"""
        with pytest.raises(ValueError, match="Password cannot be empty or None"):
            hash_password(None)

    def test_hash_password_too_long(self):
        """测试超长密码（超过72字节）"""
        # 创建一个超过72字节的密码
        long_password = "a" * 73
        with pytest.raises(ValueError, match="Password cannot exceed 72 bytes"):
            hash_password(long_password)

    def test_hash_password_max_length(self):
        """测试最大长度密码（72字节）"""
        # 72字节的密码应该可以正常哈希
        max_password = "a" * 72
        hashed = hash_password(max_password)
        assert isinstance(hashed, str)
        assert verify_password(max_password, hashed) is True

    def test_verify_password_empty_string(self):
        """测试验证空字符串密码"""
        hashed = hash_password("valid_password")
        with pytest.raises(ValueError, match="Password cannot be empty or None"):
            verify_password("", hashed)

    def test_verify_password_none(self):
        """测试验证None密码"""
        hashed = hash_password("valid_password")
        with pytest.raises(ValueError, match="Password cannot be empty or None"):
            verify_password(None, hashed)

    def test_hash_password_different_each_time(self):
        """测试相同密码每次哈希结果不同（因为salt不同）"""
        password = "same_password"
        hash1 = hash_password(password)
        hash2 = hash_password(password)

        # 两次哈希结果应该不同
        assert hash1 != hash2
        # 但都能验证成功
        assert verify_password(password, hash1) is True
        assert verify_password(password, hash2) is True


class TestJWTToken:
    """JWT令牌测试"""

    def test_create_access_token(self):
        """测试创建访问令牌"""
        data = {"sub": "user123", "role": "admin"}
        token = create_access_token(data)

        # 验证令牌是字符串
        assert isinstance(token, str)
        # 验证令牌不为空
        assert len(token) > 0

    def test_verify_token_success(self):
        """测试验证有效令牌"""
        data = {"sub": "user123", "role": "admin"}
        token = create_access_token(data)

        # 验证令牌
        payload, error = verify_token(token)
        assert payload is not None
        assert error is None
        assert payload["sub"] == "user123"
        assert payload["role"] == "admin"
        # 验证包含过期时间
        assert "exp" in payload

    def test_verify_token_with_custom_expiry(self):
        """测试使用自定义过期时间创建令牌"""
        data = {"sub": "user456"}
        expires_delta = timedelta(minutes=60)
        token = create_access_token(data, expires_delta)

        # 验证令牌
        payload, error = verify_token(token)
        assert payload is not None
        assert error is None
        assert payload["sub"] == "user456"

    def test_verify_token_expired(self):
        """测试验证过期令牌"""
        data = {"sub": "user789"}
        # 创建一个立即过期的令牌
        expires_delta = timedelta(seconds=-1)
        token = create_access_token(data, expires_delta)

        # 验证过期的令牌应该返回None和"expired"错误
        payload, error = verify_token(token)
        assert payload is None
        assert error == "expired"

    def test_verify_token_invalid(self):
        """测试验证无效令牌（签名错误）"""
        invalid_token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ1c2VyMTIzIn0.invalid_signature"

        # 验证无效令牌应该返回None和错误类型
        payload, error = verify_token(invalid_token)
        assert payload is None
        assert error in ["invalid_signature", "malformed"]

    def test_verify_token_malformed(self):
        """测试验证格式错误的令牌"""
        malformed_token = "not-a-jwt-token"

        # 验证格式错误的令牌应该返回None和"malformed"错误
        payload, error = verify_token(malformed_token)
        assert payload is None
        assert error == "malformed"

    def test_token_contains_all_data(self):
        """测试令牌包含所有编码的数据"""
        data = {
            "sub": "user123",
            "username": "testuser",
            "email": "test@example.com",
            "role": "user"
        }
        token = create_access_token(data)

        # 验证令牌
        payload, error = verify_token(token)
        assert payload is not None
        assert error is None
        assert payload["sub"] == "user123"
        assert payload["username"] == "testuser"
        assert payload["email"] == "test@example.com"
        assert payload["role"] == "user"
