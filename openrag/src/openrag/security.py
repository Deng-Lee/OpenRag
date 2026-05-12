"""安全工具模块 - 密码哈希和JWT令牌管理"""

from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

import bcrypt
from jose import ExpiredSignatureError, JWTError, jwt

from .config import get_config

# Bcrypt工作因子 - 控制哈希计算复杂度
BCRYPT_ROUNDS = 12

# 密码长度限制（bcrypt最大支持72字节）
MIN_PASSWORD_LENGTH = 1
MAX_PASSWORD_BYTES = 72


def hash_password(password: str) -> str:
    """
    使用bcrypt对密码进行哈希

    Args:
        password: 明文密码

    Returns:
        哈希后的密码字符串

    Raises:
        ValueError: 如果密码为空、None或超过72字节
    """
    if not password:
        raise ValueError("Password cannot be empty or None")

    password_bytes = password.encode('utf-8')
    if len(password_bytes) > MAX_PASSWORD_BYTES:
        raise ValueError(f"Password cannot exceed {MAX_PASSWORD_BYTES} bytes (bcrypt limitation)")

    salt = bcrypt.gensalt(rounds=BCRYPT_ROUNDS)
    hashed = bcrypt.hashpw(password_bytes, salt)
    return hashed.decode('utf-8')


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    验证密码是否匹配

    Args:
        plain_password: 明文密码
        hashed_password: 哈希后的密码

    Returns:
        密码是否匹配

    Raises:
        ValueError: 如果密码为空、None或超过72字节
    """
    if not plain_password:
        raise ValueError("Password cannot be empty or None")

    password_bytes = plain_password.encode('utf-8')
    if len(password_bytes) > MAX_PASSWORD_BYTES:
        raise ValueError(f"Password cannot exceed {MAX_PASSWORD_BYTES} bytes (bcrypt limitation)")

    return bcrypt.checkpw(
        password_bytes,
        hashed_password.encode('utf-8')
    )


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """
    创建JWT访问令牌

    Args:
        data: 要编码到令牌中的数据
        expires_delta: 令牌过期时间，如果为None则使用配置中的默认值

    Returns:
        JWT令牌字符串
    """
    config = get_config()
    to_encode = data.copy()

    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(
            minutes=config.security.access_token_expire_minutes
        )

    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(
        to_encode,
        config.security.secret_key,
        algorithm=config.security.algorithm
    )
    return encoded_jwt


def verify_token(token: str) -> Tuple[Optional[dict], Optional[str]]:
    """
    验证JWT令牌并返回解码后的数据

    Args:
        token: JWT令牌字符串

    Returns:
        元组 (payload, error):
        - payload: 解码后的数据字典，如果令牌无效则为None
        - error: 错误类型字符串，可能的值:
            - None: 验证成功
            - "expired": 令牌已过期
            - "invalid_signature": 签名无效
            - "malformed": 令牌格式错误
    """
    config = get_config()
    try:
        payload = jwt.decode(
            token,
            config.security.secret_key,
            algorithms=[config.security.algorithm]
        )
        return payload, None
    except ExpiredSignatureError:
        return None, "expired"
    except JWTError as e:
        # 区分签名错误和格式错误
        error_msg = str(e).lower()
        if "signature" in error_msg:
            return None, "invalid_signature"
        else:
            return None, "malformed"
