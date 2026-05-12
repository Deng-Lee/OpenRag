"""Integration tests for parser system."""

import pytest
from pathlib import Path

from openrag.parsers import ParserRegistry, DocumentBlock
from openrag.parsers.factory import ParserFactory


@pytest.fixture
def registry():
    """创建解析器注册表"""
    return ParserRegistry()


@pytest.fixture
def fixtures_dir():
    """测试文件目录"""
    return Path(__file__).parent / "fixtures"


def test_factory_lazy_loading():
    """测试懒加载机制"""
    factory = ParserFactory()

    # 初始状态：无缓存
    assert len(factory._parsers) == 0

    # 首次获取：触发加载
    # 注意：适配器尚未完全实现，这里只测试工厂结构
    assert hasattr(factory, '_parsers')
    assert isinstance(factory._parsers, dict)
    assert len(factory._parser_classes) == 15  # 15个扩展名


def test_document_block_fields():
    """测试 DocumentBlock 所有字段"""
    block = DocumentBlock(
        text="test content",
        page=1,
        offset=0,
        bbox=(0.0, 0.0, 100.0, 100.0),
        block_type="text",
        level=0,
        image=b"image_data",
        table_data={"rows": 2, "cols": 3},
        layout_type="text",
        confidence=0.95,
        language="en",
        metadata={"source": "test"}
    )

    assert block.text == "test content"
    assert block.page == 1
    assert block.image == b"image_data"
    assert block.table_data["rows"] == 2
    assert block.confidence == 0.95
    assert block.metadata["source"] == "test"


def test_unsupported_format(registry):
    """测试不支持的格式"""
    with pytest.raises(ValueError, match="不支持的文件格式"):
        registry.parse("test.xyz")


def test_adapter_imports():
    """测试适配器导入"""
    from openrag.parsers.adapters import (
        RAGFlowParserAdapter,
        PDFParserAdapter,
        DocxParserAdapter,
        ExcelParserAdapter,
        PptParserAdapter,
        HtmlParserAdapter,
        MarkdownParserAdapter,
        TxtParserAdapter,
        JsonParserAdapter,
        EpubParserAdapter,
    )

    # 验证所有适配器类都存在
    assert RAGFlowParserAdapter is not None
    assert PDFParserAdapter is not None
    assert DocxParserAdapter is not None
    assert ExcelParserAdapter is not None
    assert PptParserAdapter is not None
    assert HtmlParserAdapter is not None
    assert MarkdownParserAdapter is not None
    assert TxtParserAdapter is not None
    assert JsonParserAdapter is not None
    assert EpubParserAdapter is not None
