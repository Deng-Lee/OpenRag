"""环境验证测试"""

import pytest
import socket
import sys
import os

# Add src to path for config import
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from openrag.config import get_config


def is_service_available(host: str, port: int, timeout: int = 2) -> bool:
    """检查服务是否可用"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception:
        return False


# Load config once
config = get_config()

# Skip decorators for when services are unavailable
skip_if_postgres_unavailable = pytest.mark.skipif(
    not is_service_available(config.postgres.host, config.postgres.port),
    reason="PostgreSQL service is not available"
)

skip_if_milvus_unavailable = pytest.mark.skipif(
    not is_service_available(config.vector_db.host, config.vector_db.port),
    reason="Milvus service is not available"
)


@skip_if_postgres_unavailable
def test_postgres_connection():
    """测试 PostgreSQL 连接"""
    import psycopg

    conn = None
    try:
        conn = psycopg.connect(
            host=config.postgres.host,
            port=config.postgres.port,
            user=config.postgres.user,
            password=config.postgres.password,
            dbname=config.postgres.database,
        )
        cur = conn.cursor()
        cur.execute("SELECT 1")
        result = cur.fetchone()
        assert result == (1,)
        cur.close()
    finally:
        if conn:
            conn.close()


@skip_if_milvus_unavailable
def test_milvus_connection():
    """测试 Milvus 连接"""
    from pymilvus import connections

    try:
        connections.connect(
            host=config.vector_db.host,
            port=config.vector_db.port
        )
        assert connections.has_connection('default')
    finally:
        if connections.has_connection('default'):
            connections.disconnect('default')


def test_all_services_running():
    """测试所有服务是否运行"""
    services = [
        ('PostgreSQL', config.postgres.host, config.postgres.port),
        ('Milvus', config.vector_db.host, config.vector_db.port),
    ]

    results = []
    for name, host, port in services:
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            result = sock.connect_ex((host, port))
            if result != 0:
                results.append(f"{name} is not running on {host}:{port}")
        finally:
            if sock:
                sock.close()

    if results:
        pytest.skip(f"Services unavailable: {', '.join(results)}")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
