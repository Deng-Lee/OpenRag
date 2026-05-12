"""OpenViking 集成测试"""

import pytest


def test_import_openviking() -> None:
    """测试 OpenViking 导入"""
    try:
        import openviking
        # Check for OpenViking class (can be imported via __getattr__)
        from openviking import OpenViking
        assert OpenViking is not None
    except ImportError as e:
        pytest.fail(f"Failed to import OpenViking: {e}")


def test_openviking_version() -> None:
    """测试 OpenViking 版本"""
    import openviking
    assert hasattr(openviking, '__version__')
    assert openviking.__version__ is not None


@pytest.mark.skip(reason="Requires OpenViking installation and configuration")
def test_openviking_initialization() -> None:
    """测试 OpenViking 初始化"""
    from openviking import OpenViking
    from src.openrag.config import get_config

    config = get_config()

    # 初始化 OpenViking
    viking = OpenViking(
        storage_config={
            'type': config.storage.type,
            'config': {
                'base_path': config.storage.base_path
            }
        },
        vector_db_config={
            'type': config.vector_db.type,
            'config': {
                'host': config.vector_db.host,
                'port': config.vector_db.port
            }
        }
    )

    assert viking is not None
    # Test filesystem operations available on OpenViking
    assert hasattr(viking, 'ls')
    assert hasattr(viking, 'read')
    assert hasattr(viking, 'write')


def test_agfs_client_import() -> None:
    """测试 AGFSClient 导入"""
    try:
        from openviking import AGFSClient
        assert AGFSClient is not None
    except ImportError as e:
        pytest.fail(f"Failed to import AGFSClient: {e}")


def test_agfs_client_initialization() -> None:
    """测试 AGFSClient 初始化"""
    from openviking import AGFSClient

    # 初始化客户端（不连接到实际服务器）
    client = AGFSClient(api_base_url="http://localhost:8080", timeout=10)
    assert client is not None


@pytest.mark.skip(reason="Requires OpenViking server running")
def test_agfs_client_connection() -> None:
    """测试 AGFSClient 连接"""
    from openviking import AGFSClient
    from src.openrag.config import get_config

    config = get_config()

    # 初始化客户端
    client = AGFSClient(api_base_url="http://localhost:8080")

    # 测试基本操作（需要服务器运行）
    # 这里只是示例，实际测试需要服务器
    try:
        result = client.ls("/")
        assert result is not None
    except Exception as e:
        pytest.skip(f"OpenViking server not available: {e}")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
