"""Tests for ParserFactory."""

import pytest
from openrag.parsers.factory import ParserFactory
from openrag.parsers.base import DocumentParser


def test_factory_initialization():
    """测试工厂初始化"""
    factory = ParserFactory()
    assert factory._parsers == {}
    assert len(factory._parser_classes) == 20  # 11种格式，20个扩展名


def test_get_parser_unsupported_format():
    """测试不支持的格式"""
    factory = ParserFactory()
    with pytest.raises(ValueError, match="不支持的文件格式"):
        factory.get_parser("test.xyz")


def test_parser_caching():
    """测试解析器缓存"""
    factory = ParserFactory()
    # 注意：这个测试需要在实现适配器后才能真正运行
    # 现在只测试缓存机制的结构
    assert hasattr(factory, '_parsers')
    assert isinstance(factory._parsers, dict)
