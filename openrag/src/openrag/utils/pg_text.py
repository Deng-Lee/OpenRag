"""PostgreSQL 文本列兼容（禁止 NUL 字节）。"""

from typing import Any


def strip_pg_nul_bytes(value: Any) -> Any:
    """去掉 \\x00，避免 psycopg 写入 TEXT/VARCHAR 时报错。"""
    if isinstance(value, str) and "\x00" in value:
        return value.replace("\x00", "")
    return value


def strip_pg_nul_in_json(value: Any) -> Any:
    """递归处理 dict/list 中字符串，避免 JSON/JSONB 序列化后仍含 NUL。"""
    if isinstance(value, str):
        return strip_pg_nul_bytes(value)
    if isinstance(value, dict):
        return {k: strip_pg_nul_in_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [strip_pg_nul_in_json(v) for v in value]
    return value
