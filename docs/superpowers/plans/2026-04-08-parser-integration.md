# RAGFlow 解析器集成实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 RAGFlow 的 deepdoc 解析器集成到 OpenRag，支持 9 种文档格式的深度解析

**Architecture:** 复制 RAGFlow 的 deepdoc 模块到项目中，使用适配器模式将 RAGFlow 格式转换为 DocumentBlock，通过懒加载工厂按需初始化解析器

**Tech Stack:** Python 3.10+, RAGFlow deepdoc, PaddleOCR, XGBoost, HuggingFace

---

## 文件结构概览

**新增文件：**
- `src/openrag/parsers/factory.py` - 懒加载工厂
- `src/openrag/parsers/adapters/__init__.py` - 适配器包
- `src/openrag/parsers/adapters/base_adapter.py` - 适配器基类
- `src/openrag/parsers/adapters/pdf_adapter.py` - PDF 适配器
- `src/openrag/parsers/adapters/docx_adapter.py` - Word 适配器
- `src/openrag/parsers/adapters/excel_adapter.py` - Excel 适配器
- `src/openrag/parsers/adapters/ppt_adapter.py` - PowerPoint 适配器
- `src/openrag/parsers/adapters/html_adapter.py` - HTML 适配器
- `src/openrag/parsers/adapters/markdown_adapter.py` - Markdown 适配器
- `src/openrag/parsers/adapters/txt_adapter.py` - 纯文本适配器
- `src/openrag/parsers/adapters/json_adapter.py` - JSON 适配器
- `src/openrag/parsers/adapters/epub_adapter.py` - EPUB 适配器
- `src/openrag/parsers/ragflow/` - RAGFlow deepdoc 模块（复制）
- `tests/test_parser_integration.py` - 集成测试

**修改文件：**
- `src/openrag/parsers/base.py` - 扩展 DocumentBlock
- `src/openrag/parsers/parser_registry.py` - 集成工厂
- `src/openrag/parsers/__init__.py` - 更新导出
- `src/openrag/chunking/chunk_engine.py` - 更新类型
- `src/openrag/processors/document_processor.py` - 更新类型
- `src/openrag/api/files_api.py` - 扩展 MIME 类型
- `tests/test_ragflow_parser.py` - 更新测试
- `tests/test_document_processor.py` - 更新测试
- `tests/test_integration_document_processing.py` - 更新测试
- `requirements.txt` - 添加依赖

---

## Task 1: 准备工作 - 复制 RAGFlow 代码

**Files:**
- Create: `src/openrag/parsers/ragflow/` (directory)
- Create: `src/openrag/parsers/ragflow/__init__.py`
- Create: `src/openrag/parsers/ragflow/parser/` (directory)
- Create: `src/openrag/parsers/ragflow/vision/` (directory)

- [ ] **Step 1: 创建 ragflow 目录结构**

```bash
mkdir -p src/openrag/parsers/ragflow/parser
mkdir -p src/openrag/parsers/ragflow/vision
```

- [ ] **Step 2: 复制 RAGFlow parser 模块**

```bash
cp -r /e/project/openrag/ragflow/deepdoc/parser/*.py src/openrag/parsers/ragflow/parser/
```

Expected: 复制 10 个文件（__init__.py, pdf_parser.py, docx_parser.py, excel_parser.py, ppt_parser.py, html_parser.py, markdown_parser.py, txt_parser.py, json_parser.py, epub_parser.py, utils.py）

- [ ] **Step 3: 复制 RAGFlow vision 模块**

```bash
cp -r /e/project/openrag/ragflow/deepdoc/vision/*.py src/openrag/parsers/ragflow/vision/
```

Expected: 复制视觉识别相关文件

- [ ] **Step 4: 创建 ragflow __init__.py**

```python
# src/openrag/parsers/ragflow/__init__.py
"""
RAGFlow deepdoc 模块

Copyright 2025 The InfiniFlow Authors. All Rights Reserved.
Licensed under the Apache License, Version 2.0
"""

__all__ = []
```

- [ ] **Step 5: 验证文件复制**

```bash
ls -la src/openrag/parsers/ragflow/parser/
ls -la src/openrag/parsers/ragflow/vision/
```

Expected: 看到所有复制的文件

- [ ] **Step 6: Commit**

```bash
git add src/openrag/parsers/ragflow/
git commit -m "chore: copy RAGFlow deepdoc module

- Copy parser module (PDF, DOCX, Excel, PPT, HTML, Markdown, TXT, JSON, EPUB)
- Copy vision module (OCR, layout recognizer, table structure recognizer)
- Preserve Apache 2.0 license headers

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: 扩展 DocumentBlock 数据类

**Files:**
- Modify: `src/openrag/parsers/base.py`
- Modify: `src/openrag/parsers/__init__.py`

- [ ] **Step 1: 备份当前 base.py**

```bash
cp src/openrag/parsers/base.py src/openrag/parsers/base.py.bak
```

- [ ] **Step 2: 扩展 DocumentBlock 类**

```python
# src/openrag/parsers/base.py
"""Base classes for document parsers."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class DocumentBlock:
    """文档内容块，包含文本、图片、表格等多种内容类型
    
    Attributes:
        text: 文本内容
        page: 页码（1-indexed）
        offset: 字符偏移量
        bbox: PDF 坐标 (x1, y1, x2, y2)
        block_type: 块类型（text, table, image, code）
        level: 标题层级（0=正文，1-6=标题）
        image: 图片数据（如果有）
        table_data: 表格结构化数据
        layout_type: 布局类型（title/text/table/figure/list）
        confidence: OCR 置信度（0-1）
        language: 检测到的语言
        metadata: 其他元数据
    """
    text: str
    page: int
    offset: int
    bbox: Optional[tuple[float, float, float, float]] = None
    block_type: str = "text"
    level: int = 0
    
    # RAGFlow 增强字段
    image: Optional[bytes] = None
    table_data: Optional[dict] = None
    layout_type: Optional[str] = None
    confidence: Optional[float] = None
    language: Optional[str] = None
    metadata: Optional[dict] = None


class DocumentParser(ABC):
    """文档解析器基类"""
    
    @abstractmethod
    def parse(self, file_path: str) -> list[DocumentBlock]:
        """解析文档，返回文档块列表"""
        pass
    
    @abstractmethod
    def supports(self, file_path: str) -> bool:
        """检查是否支持该文件类型"""
        pass
```

- [ ] **Step 3: 更新 __init__.py 导出**

```python
# src/openrag/parsers/__init__.py
"""Document parsers for OpenRag."""

from .base import DocumentBlock, DocumentParser
from .parser_registry import ParserRegistry

__all__ = [
    "DocumentBlock",
    "DocumentParser",
    "ParserRegistry",
]
```

- [ ] **Step 4: 运行测试验证基础类**

```bash
python -c "from openrag.parsers import DocumentBlock; print('DocumentBlock imported successfully')"
```

Expected: "DocumentBlock imported successfully"

- [ ] **Step 5: Commit**

```bash
git add src/openrag/parsers/base.py src/openrag/parsers/__init__.py
git commit -m "feat: extend TextBlock to DocumentBlock with RAGFlow fields

- Rename TextBlock to DocumentBlock
- Add image, table_data, layout_type fields
- Add confidence, language, metadata fields
- Update exports

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: 实现 ParserFactory 懒加载工厂

**Files:**
- Create: `src/openrag/parsers/factory.py`
- Test: `tests/test_parser_factory.py`

- [ ] **Step 1: 编写工厂测试**

```python
# tests/test_parser_factory.py
"""Tests for ParserFactory."""

import pytest
from openrag.parsers.factory import ParserFactory
from openrag.parsers.base import DocumentParser


def test_factory_initialization():
    """测试工厂初始化"""
    factory = ParserFactory()
    assert factory._parsers == {}
    assert len(factory._parser_classes) == 15  # 9种格式，15个扩展名


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
```

- [ ] **Step 2: 运行测试验证失败**

```bash
pytest tests/test_parser_factory.py -v
```

Expected: FAIL - ModuleNotFoundError: No module named 'openrag.parsers.factory'

- [ ] **Step 3: 实现 ParserFactory**

```python
# src/openrag/parsers/factory.py
"""Parser factory with lazy loading."""

import os
import importlib
from typing import Dict

from .base import DocumentParser


class ParserFactory:
    """解析器懒加载工厂"""
    
    def __init__(self):
        self._parsers: Dict[str, DocumentParser] = {}
        self._parser_classes = {
            '.pdf': 'openrag.parsers.adapters.pdf_adapter.PDFParserAdapter',
            '.docx': 'openrag.parsers.adapters.docx_adapter.DocxParserAdapter',
            '.doc': 'openrag.parsers.adapters.docx_adapter.DocxParserAdapter',
            '.xlsx': 'openrag.parsers.adapters.excel_adapter.ExcelParserAdapter',
            '.xls': 'openrag.parsers.adapters.excel_adapter.ExcelParserAdapter',
            '.csv': 'openrag.parsers.adapters.excel_adapter.ExcelParserAdapter',
            '.pptx': 'openrag.parsers.adapters.ppt_adapter.PptParserAdapter',
            '.ppt': 'openrag.parsers.adapters.ppt_adapter.PptParserAdapter',
            '.html': 'openrag.parsers.adapters.html_adapter.HtmlParserAdapter',
            '.htm': 'openrag.parsers.adapters.html_adapter.HtmlParserAdapter',
            '.md': 'openrag.parsers.adapters.markdown_adapter.MarkdownParserAdapter',
            '.markdown': 'openrag.parsers.adapters.markdown_adapter.MarkdownParserAdapter',
            '.txt': 'openrag.parsers.adapters.txt_adapter.TxtParserAdapter',
            '.json': 'openrag.parsers.adapters.json_adapter.JsonParserAdapter',
            '.epub': 'openrag.parsers.adapters.epub_adapter.EpubParserAdapter',
        }
    
    def get_parser(self, file_path: str) -> DocumentParser:
        """获取解析器（懒加载）
        
        Args:
            file_path: 文件路径
            
        Returns:
            DocumentParser 实例
            
        Raises:
            ValueError: 不支持的文件格式
        """
        ext = os.path.splitext(file_path)[1].lower()
        
        if ext in self._parsers:
            return self._parsers[ext]
        
        if ext in self._parser_classes:
            parser = self._load_parser(ext)
            self._parsers[ext] = parser
            return parser
        
        raise ValueError(f"不支持的文件格式: {ext}")
    
    def _load_parser(self, ext: str) -> DocumentParser:
        """动态加载解析器类"""
        class_path = self._parser_classes[ext]
        module_path, class_name = class_path.rsplit('.', 1)
        module = importlib.import_module(module_path)
        parser_class = getattr(module, class_name)
        return parser_class()
```

- [ ] **Step 4: 运行测试验证通过**

```bash
pytest tests/test_parser_factory.py::test_factory_initialization -v
pytest tests/test_parser_factory.py::test_get_parser_unsupported_format -v
```

Expected: 2 PASSED

- [ ] **Step 5: Commit**

```bash
git add src/openrag/parsers/factory.py tests/test_parser_factory.py
git commit -m "feat: add ParserFactory with lazy loading

- Implement lazy loading mechanism
- Support 9 document formats with 15 file extensions
- Add parser caching
- Add tests

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: 实现适配器基类

**Files:**
- Create: `src/openrag/parsers/adapters/__init__.py`
- Create: `src/openrag/parsers/adapters/base_adapter.py`
- Test: `tests/test_base_adapter.py`

- [ ] **Step 1: 创建适配器包**

```bash
mkdir -p src/openrag/parsers/adapters
```

- [ ] **Step 2: 创建适配器 __init__.py**

```python
# src/openrag/parsers/adapters/__init__.py
"""RAGFlow parser adapters."""

from .base_adapter import RAGFlowParserAdapter

__all__ = ["RAGFlowParserAdapter"]
```

- [ ] **Step 3: 编写适配器基类测试**

```python
# tests/test_base_adapter.py
"""Tests for RAGFlowParserAdapter base class."""

import pytest
from openrag.parsers.adapters.base_adapter import RAGFlowParserAdapter
from openrag.parsers.base import DocumentBlock


class MockAdapter(RAGFlowParserAdapter):
    """Mock adapter for testing"""
    supported_extensions = ('.mock',)
    
    def __init__(self):
        super().__init__()
        self.ragflow_parser = lambda fp: [["test content", None]]


def test_adapter_supports():
    """测试文件类型支持检查"""
    adapter = MockAdapter()
    assert adapter.supports("test.mock") is True
    assert adapter.supports("test.MOCK") is True
    assert adapter.supports("test.txt") is False


def test_adapter_parse():
    """测试解析功能"""
    adapter = MockAdapter()
    blocks = adapter.parse("test.mock")
    
    assert len(blocks) == 1
    assert isinstance(blocks[0], DocumentBlock)
    assert blocks[0].text == "test content"
    assert blocks[0].page == 1
    assert blocks[0].offset == 0


def test_adapter_with_image():
    """测试带图片的解析"""
    class ImageAdapter(RAGFlowParserAdapter):
        supported_extensions = ('.img',)
        def __init__(self):
            super().__init__()
            self.ragflow_parser = lambda fp: [["content", b"image_data"]]
    
    adapter = ImageAdapter()
    blocks = adapter.parse("test.img")
    
    assert blocks[0].image == b"image_data"
    assert blocks[0].layout_type == "figure"
```

- [ ] **Step 4: 运行测试验证失败**

```bash
pytest tests/test_base_adapter.py -v
```

Expected: FAIL - ModuleNotFoundError

- [ ] **Step 5: 实现适配器基类**

```python
# src/openrag/parsers/adapters/base_adapter.py
"""Base adapter for RAGFlow parsers."""

from abc import ABC
from typing import Tuple

from openrag.parsers.base import DocumentBlock, DocumentParser


class RAGFlowParserAdapter(DocumentParser, ABC):
    """RAGFlow 解析器适配器基类"""
    
    supported_extensions: Tuple[str, ...] = ()
    
    def __init__(self):
        self.ragflow_parser = None
    
    def parse(self, file_path: str) -> list[DocumentBlock]:
        """解析文档并转换为 DocumentBlock"""
        raw_result = self.ragflow_parser(file_path)
        
        blocks = []
        for idx, item in enumerate(raw_result):
            content = item[0] if isinstance(item, (list, tuple)) else item
            image = item[1] if isinstance(item, (list, tuple)) and len(item) > 1 else None
            
            block = self._convert_to_document_block(content, image, idx)
            blocks.append(block)
        
        return blocks
    
    def supports(self, file_path: str) -> bool:
        """检查文件扩展名"""
        return file_path.lower().endswith(self.supported_extensions)
    
    def _convert_to_document_block(
        self, 
        content: str, 
        image: bytes, 
        index: int
    ) -> DocumentBlock:
        """转换为 DocumentBlock（子类可覆盖）"""
        return DocumentBlock(
            text=content,
            page=self._extract_page(content, index),
            offset=index,
            image=image,
            block_type=self._detect_block_type(content),
            level=self._detect_heading_level(content),
            layout_type=self._detect_layout_type(content, image)
        )
    
    def _extract_page(self, content: str, index: int) -> int:
        """提取页码（子类可覆盖）"""
        return 1
    
    def _detect_block_type(self, content: str) -> str:
        """检测块类型（子类可覆盖）"""
        return "text"
    
    def _detect_heading_level(self, content: str) -> int:
        """检测标题层级（子类可覆盖）"""
        return 0
    
    def _detect_layout_type(self, content: str, image: bytes) -> str:
        """检测布局类型（子类可覆盖）"""
        if image:
            return "figure"
        return "text"
```

- [ ] **Step 6: 运行测试验证通过**

```bash
pytest tests/test_base_adapter.py -v
```

Expected: 3 PASSED

- [ ] **Step 7: Commit**

```bash
git add src/openrag/parsers/adapters/ tests/test_base_adapter.py
git commit -m "feat: add RAGFlowParserAdapter base class

- Implement adapter pattern for format conversion
- Support RAGFlow [[content, image], ...] format
- Add extensible conversion methods
- Add tests

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: 实现具体适配器（PDF, Markdown, TXT）

**Files:**
- Create: `src/openrag/parsers/adapters/pdf_adapter.py`
- Create: `src/openrag/parsers/adapters/markdown_adapter.py`
- Create: `src/openrag/parsers/adapters/txt_adapter.py`
- Test: `tests/test_adapters.py`

- [ ] **Step 1: 实现 PDF 适配器**

```python
# src/openrag/parsers/adapters/pdf_adapter.py
"""PDF parser adapter."""

from openrag.parsers.adapters.base_adapter import RAGFlowParserAdapter


class PDFParserAdapter(RAGFlowParserAdapter):
    """PDF 解析器适配器"""
    
    supported_extensions = ('.pdf',)
    
    def __init__(self):
        super().__init__()
        from openrag.parsers.ragflow.parser.pdf_parser import RAGFlowPdfParser
        self.ragflow_parser = RAGFlowPdfParser()
```

- [ ] **Step 2: 实现 Markdown 适配器**

```python
# src/openrag/parsers/adapters/markdown_adapter.py
"""Markdown parser adapter."""

from openrag.parsers.adapters.base_adapter import RAGFlowParserAdapter


class MarkdownParserAdapter(RAGFlowParserAdapter):
    """Markdown 解析器适配器"""
    
    supported_extensions = ('.md', '.markdown')
    
    def __init__(self):
        super().__init__()
        from openrag.parsers.ragflow.parser.markdown_parser import RAGFlowMarkdownParser
        self.ragflow_parser = RAGFlowMarkdownParser()
    
    def _detect_heading_level(self, content: str) -> int:
        """检测 Markdown 标题层级"""
        if content.startswith('#'):
            return len(content) - len(content.lstrip('#'))
        return 0
```

- [ ] **Step 3: 实现 TXT 适配器**

```python
# src/openrag/parsers/adapters/txt_adapter.py
"""Text parser adapter."""

from openrag.parsers.adapters.base_adapter import RAGFlowParserAdapter


class TxtParserAdapter(RAGFlowParserAdapter):
    """纯文本解析器适配器"""
    
    supported_extensions = ('.txt',)
    
    def __init__(self):
        super().__init__()
        from openrag.parsers.ragflow.parser.txt_parser import RAGFlowTxtParser
        self.ragflow_parser = RAGFlowTxtParser()
```

- [ ] **Step 4: 实现 DOCX 适配器**

```python
# src/openrag/parsers/adapters/docx_adapter.py
"""DOCX parser adapter."""

from openrag.parsers.adapters.base_adapter import RAGFlowParserAdapter


class DocxParserAdapter(RAGFlowParserAdapter):
    """Word 文档解析器适配器"""
    
    supported_extensions = ('.docx', '.doc')
    
    def __init__(self):
        super().__init__()
        from openrag.parsers.ragflow.parser.docx_parser import RAGFlowDocxParser
        self.ragflow_parser = RAGFlowDocxParser()
```

- [ ] **Step 5: 实现 Excel 适配器**

```python
# src/openrag/parsers/adapters/excel_adapter.py
"""Excel parser adapter."""

from openrag.parsers.adapters.base_adapter import RAGFlowParserAdapter


class ExcelParserAdapter(RAGFlowParserAdapter):
    """Excel 解析器适配器"""
    
    supported_extensions = ('.xlsx', '.xls', '.csv')
    
    def __init__(self):
        super().__init__()
        from openrag.parsers.ragflow.parser.excel_parser import RAGFlowExcelParser
        self.ragflow_parser = RAGFlowExcelParser()
```

- [ ] **Step 6: 实现 PPT 适配器**

```python
# src/openrag/parsers/adapters/ppt_adapter.py
"""PowerPoint parser adapter."""

from openrag.parsers.adapters.base_adapter import RAGFlowParserAdapter


class PptParserAdapter(RAGFlowParserAdapter):
    """PowerPoint 解析器适配器"""
    
    supported_extensions = ('.pptx', '.ppt')
    
    def __init__(self):
        super().__init__()
        from openrag.parsers.ragflow.parser.ppt_parser import RAGFlowPptParser
        self.ragflow_parser = RAGFlowPptParser()
```

- [ ] **Step 7: 实现 HTML 适配器**

```python
# src/openrag/parsers/adapters/html_adapter.py
"""HTML parser adapter."""

from openrag.parsers.adapters.base_adapter import RAGFlowParserAdapter


class HtmlParserAdapter(RAGFlowParserAdapter):
    """HTML 解析器适配器"""
    
    supported_extensions = ('.html', '.htm')
    
    def __init__(self):
        super().__init__()
        from openrag.parsers.ragflow.parser.html_parser import RAGFlowHtmlParser
        self.ragflow_parser = RAGFlowHtmlParser()
```

- [ ] **Step 8: 实现 JSON 适配器**

```python
# src/openrag/parsers/adapters/json_adapter.py
"""JSON parser adapter."""

from openrag.parsers.adapters.base_adapter import RAGFlowParserAdapter


class JsonParserAdapter(RAGFlowParserAdapter):
    """JSON 解析器适配器"""
    
    supported_extensions = ('.json',)
    
    def __init__(self):
        super().__init__()
        from openrag.parsers.ragflow.parser.json_parser import RAGFlowJsonParser
        self.ragflow_parser = RAGFlowJsonParser()
```

- [ ] **Step 9: 实现 EPUB 适配器**

```python
# src/openrag/parsers/adapters/epub_adapter.py
"""EPUB parser adapter."""

from openrag.parsers.adapters.base_adapter import RAGFlowParserAdapter


class EpubParserAdapter(RAGFlowParserAdapter):
    """EPUB 解析器适配器"""
    
    supported_extensions = ('.epub',)
    
    def __init__(self):
        super().__init__()
        from openrag.parsers.ragflow.parser.epub_parser import RAGFlowEpubParser
        self.ragflow_parser = RAGFlowEpubParser()
```

- [ ] **Step 10: 更新适配器包导出**

```python
# src/openrag/parsers/adapters/__init__.py
"""RAGFlow parser adapters."""

from .base_adapter import RAGFlowParserAdapter
from .pdf_adapter import PDFParserAdapter
from .docx_adapter import DocxParserAdapter
from .excel_adapter import ExcelParserAdapter
from .ppt_adapter import PptParserAdapter
from .html_adapter import HtmlParserAdapter
from .markdown_adapter import MarkdownParserAdapter
from .txt_adapter import TxtParserAdapter
from .json_adapter import JsonParserAdapter
from .epub_adapter import EpubParserAdapter

__all__ = [
    "RAGFlowParserAdapter",
    "PDFParserAdapter",
    "DocxParserAdapter",
    "ExcelParserAdapter",
    "PptParserAdapter",
    "HtmlParserAdapter",
    "MarkdownParserAdapter",
    "TxtParserAdapter",
    "JsonParserAdapter",
    "EpubParserAdapter",
]
```

- [ ] **Step 11: Commit**

```bash
git add src/openrag/parsers/adapters/*.py
git commit -m "feat: add all 9 document format adapters

- Add PDF, DOCX, Excel, PPT adapters
- Add HTML, Markdown, TXT adapters
- Add JSON, EPUB adapters
- All adapters support lazy loading

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: 更新 ParserRegistry 集成工厂

**Files:**
- Modify: `src/openrag/parsers/parser_registry.py`
- Test: `tests/test_parser_registry.py`

- [ ] **Step 1: 更新 ParserRegistry**

```python
# src/openrag/parsers/parser_registry.py
"""Parser registry for managing multiple document parsers."""

from .base import DocumentBlock, DocumentParser
from .factory import ParserFactory


class ParserRegistry:
    """解析器注册表
    
    使用 ParserFactory 实现懒加载机制
    """

    def __init__(self) -> None:
        """初始化解析器注册表"""
        self.factory = ParserFactory()

    def get_parser(self, file_path: str) -> DocumentParser:
        """获取适合的解析器（懒加载）
        
        Args:
            file_path: 文件路径
            
        Returns:
            DocumentParser 实例
            
        Raises:
            ValueError: 不支持的文件格式
        """
        return self.factory.get_parser(file_path)

    def parse(self, file_path: str) -> list[DocumentBlock]:
        """解析文件
        
        Args:
            file_path: 文件路径
            
        Returns:
            DocumentBlock 列表
            
        Raises:
            ValueError: 不支持的文件格式
        """
        parser = self.get_parser(file_path)
        return parser.parse(file_path)
```

- [ ] **Step 2: 更新 ParserRegistry 测试**

```python
# tests/test_parser_registry.py (更新现有测试)
"""Tests for ParserRegistry."""

import pytest
from openrag.parsers.parser_registry import ParserRegistry
from openrag.parsers.base import DocumentBlock


def test_registry_initialization():
    """测试注册表初始化"""
    registry = ParserRegistry()
    assert registry.factory is not None


def test_get_parser_unsupported():
    """测试不支持的格式"""
    registry = ParserRegistry()
    with pytest.raises(ValueError, match="不支持的文件格式"):
        registry.get_parser("test.xyz")


def test_parse_unsupported():
    """测试解析不支持的格式"""
    registry = ParserRegistry()
    with pytest.raises(ValueError, match="不支持的文件格式"):
        registry.parse("test.xyz")
```

- [ ] **Step 3: 运行测试**

```bash
pytest tests/test_parser_registry.py -v
```

Expected: 3 PASSED

- [ ] **Step 4: Commit**

```bash
git add src/openrag/parsers/parser_registry.py tests/test_parser_registry.py
git commit -m "refactor: integrate ParserFactory into ParserRegistry

- Replace manual registration with factory pattern
- Support lazy loading for all parsers
- Update tests

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: 全局替换 TextBlock → DocumentBlock

**Files:**
- Modify: `src/openrag/chunking/chunk_engine.py`
- Modify: `src/openrag/processors/document_processor.py`
- Modify: `tests/test_ragflow_parser.py`
- Modify: `tests/test_document_processor.py`
- Modify: `tests/test_integration_document_processing.py`

- [ ] **Step 1: 更新 chunk_engine.py**

```bash
sed -i 's/TextBlock/DocumentBlock/g' src/openrag/chunking/chunk_engine.py
sed -i 's/from openrag.parsers.base import DocumentBlock/from openrag.parsers.base import DocumentBlock/g' src/openrag/chunking/chunk_engine.py
```

- [ ] **Step 2: 更新 document_processor.py**

```bash
sed -i 's/TextBlock/DocumentBlock/g' src/openrag/processors/document_processor.py
sed -i 's/from openrag.parsers.base import DocumentBlock/from openrag.parsers.base import DocumentBlock/g' src/openrag/processors/document_processor.py
```

- [ ] **Step 3: 更新测试文件**

```bash
sed -i 's/TextBlock/DocumentBlock/g' tests/test_ragflow_parser.py
sed -i 's/TextBlock/DocumentBlock/g' tests/test_document_processor.py
sed -i 's/TextBlock/DocumentBlock/g' tests/test_integration_document_processing.py
```

- [ ] **Step 4: 验证所有导入**

```bash
grep -r "from.*TextBlock" src/ tests/
```

Expected: 无输出（所有 TextBlock 已替换）

- [ ] **Step 5: 运行所有测试**

```bash
pytest tests/ -v --tb=short
```

Expected: 所有测试通过

- [ ] **Step 6: Commit**

```bash
git add src/openrag/chunking/ src/openrag/processors/ tests/
git commit -m "refactor: rename TextBlock to DocumentBlock globally

- Update chunk_engine.py
- Update document_processor.py
- Update all test files
- Ensure consistency across codebase

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: 扩展 ALLOWED_MIME_TYPES

**Files:**
- Modify: `src/openrag/api/files_api.py`

- [ ] **Step 1: 更新 ALLOWED_MIME_TYPES**

```python
# src/openrag/api/files_api.py (更新常量部分)

ALLOWED_MIME_TYPES = [
    # 文本类
    "text/plain",                                                              # .txt
    "text/markdown",                                                           # .md
    "text/html",                                                               # .html
    "text/csv",                                                                # .csv
    
    # PDF
    "application/pdf",                                                         # .pdf
    
    # Word
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document", # .docx
    "application/msword",                                                      # .doc
    
    # Excel
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",      # .xlsx
    "application/vnd.ms-excel",                                                # .xls
    
    # PowerPoint
    "application/vnd.openxmlformats-officedocument.presentationml.presentation", # .pptx
    "application/vnd.ms-powerpoint",                                           # .ppt
    
    # 其他
    "application/json",                                                        # .json
    "application/epub+zip",                                                    # .epub
]
```

- [ ] **Step 2: 验证 MIME 类型数量**

```bash
python -c "from openrag.api.files_api import ALLOWED_MIME_TYPES; print(f'Total MIME types: {len(ALLOWED_MIME_TYPES)}')"
```

Expected: Total MIME types: 14

- [ ] **Step 3: Commit**

```bash
git add src/openrag/api/files_api.py
git commit -m "feat: extend ALLOWED_MIME_TYPES for 9 document formats

- Add HTML, CSV MIME types
- Add Excel, PowerPoint MIME types
- Add JSON, EPUB MIME types
- Total 14 MIME types supported

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: 添加依赖到 requirements.txt

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: 添加 RAGFlow 解析器依赖**

```txt
# requirements.txt (在文件末尾添加)

# RAGFlow 解析器依赖
pdfplumber>=0.10.0
pypdf>=3.0.0
python-docx>=1.0.0
openpyxl>=3.1.0
python-pptx>=0.6.0
beautifulsoup4>=4.12.0
html5lib>=1.1
markdown>=3.5.0
ebooklib>=0.18

# OCR 和视觉识别
paddleocr>=2.7.0
paddlepaddle>=2.5.0
opencv-python>=4.8.0
Pillow>=10.0.0

# 机器学习
xgboost>=2.0.0
scikit-learn>=1.3.0
numpy>=1.24.0

# HuggingFace
huggingface-hub>=0.19.0
```

- [ ] **Step 2: 安装新依赖**

```bash
pip install -r requirements.txt
```

Expected: 所有依赖安装成功

- [ ] **Step 3: 验证关键依赖**

```bash
python -c "import pdfplumber; import paddleocr; import xgboost; print('Key dependencies installed')"
```

Expected: "Key dependencies installed"

- [ ] **Step 4: Commit**

```bash
git add requirements.txt
git commit -m "deps: add RAGFlow parser dependencies

- Add document parsing libraries (pdfplumber, python-docx, etc.)
- Add OCR dependencies (paddleocr, paddlepaddle)
- Add ML dependencies (xgboost, scikit-learn)
- Add HuggingFace hub for model download

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: 集成测试和验证

**Files:**
- Create: `tests/test_parser_integration.py`
- Create: `tests/fixtures/` (测试文件目录)

- [ ] **Step 1: 创建测试文件目录**

```bash
mkdir -p tests/fixtures
```

- [ ] **Step 2: 创建简单测试文件**

```bash
# 创建测试 Markdown 文件
echo "# Test Document

This is a test paragraph.

## Section 1

Content here." > tests/fixtures/test.md

# 创建测试 TXT 文件
echo "This is a plain text test file.
It has multiple lines.
For testing purposes." > tests/fixtures/test.txt

# 创建测试 JSON 文件
echo '{"test": "data", "items": [1, 2, 3]}' > tests/fixtures/test.json
```

- [ ] **Step 3: 编写集成测试**

```python
# tests/test_parser_integration.py
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
    parser1 = factory.get_parser("test.md")
    assert len(factory._parsers) == 1
    assert '.md' in factory._parsers
    
    # 第二次获取：使用缓存
    parser2 = factory.get_parser("another.md")
    assert parser1 is parser2
    assert len(factory._parsers) == 1


def test_markdown_parser(registry, fixtures_dir):
    """测试 Markdown 解析"""
    md_file = fixtures_dir / "test.md"
    if not md_file.exists():
        pytest.skip("Test file not found")
    
    blocks = registry.parse(str(md_file))
    
    assert len(blocks) > 0
    assert all(isinstance(b, DocumentBlock) for b in blocks)
    # 第一个块应该是标题
    assert blocks[0].level > 0 or "Test Document" in blocks[0].text


def test_txt_parser(registry, fixtures_dir):
    """测试纯文本解析"""
    txt_file = fixtures_dir / "test.txt"
    if not txt_file.exists():
        pytest.skip("Test file not found")
    
    blocks = registry.parse(str(txt_file))
    
    assert len(blocks) > 0
    assert all(isinstance(b, DocumentBlock) for b in blocks)
    assert any("plain text" in b.text.lower() for b in blocks)


def test_json_parser(registry, fixtures_dir):
    """测试 JSON 解析"""
    json_file = fixtures_dir / "test.json"
    if not json_file.exists():
        pytest.skip("Test file not found")
    
    blocks = registry.parse(str(json_file))
    
    assert len(blocks) > 0
    assert all(isinstance(b, DocumentBlock) for b in blocks)


def test_unsupported_format(registry):
    """测试不支持的格式"""
    with pytest.raises(ValueError, match="不支持的文件格式"):
        registry.parse("test.xyz")


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
```

- [ ] **Step 4: 运行集成测试**

```bash
pytest tests/test_parser_integration.py -v
```

Expected: 至少 5 PASSED（部分测试可能 SKIP 如果没有测试文件）

- [ ] **Step 5: 运行所有测试**

```bash
pytest tests/ -v --tb=short
```

Expected: 所有测试通过

- [ ] **Step 6: Commit**

```bash
git add tests/test_parser_integration.py tests/fixtures/
git commit -m "test: add comprehensive integration tests

- Test lazy loading mechanism
- Test Markdown, TXT, JSON parsers
- Test DocumentBlock all fields
- Add test fixtures

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 11: 文档和最终验证

**Files:**
- Create: `docs/parsers.md`
- Modify: `README.md`

- [ ] **Step 1: 创建解析器文档**

```markdown
# docs/parsers.md

# 文档解析器

OpenRag 集成了 RAGFlow 的 deepdoc 解析器，支持 9 种文档格式的深度解析。

## 支持的格式

| 格式 | 扩展名 | 特性 |
|------|--------|------|
| PDF | `.pdf` | OCR + 布局识别 + 表格识别 |
| Word | `.docx`, `.doc` | 文本 + 表格 + 图片 |
| Excel | `.xlsx`, `.xls`, `.csv` | 表格数据 + 多 sheet |
| PowerPoint | `.pptx`, `.ppt` | 幻灯片文本 + 表格 |
| HTML | `.html`, `.htm` | 结构化提取 |
| Markdown | `.md`, `.markdown` | 标题层级 + 表格 |
| 纯文本 | `.txt` | 智能分段 |
| JSON | `.json` | 结构化数据 |
| EPUB | `.epub` | 电子书章节 |

## 使用方法

```python
from openrag.parsers import ParserRegistry

registry = ParserRegistry()
blocks = registry.parse("document.pdf")

for block in blocks:
    print(f"Page {block.page}: {block.text}")
    if block.image:
        print(f"  Has image: {len(block.image)} bytes")
```

## 懒加载机制

解析器采用懒加载机制，只在首次使用某格式时才初始化对应的解析器。

## DocumentBlock 字段

- `text`: 文本内容
- `page`: 页码
- `offset`: 字符偏移量
- `bbox`: PDF 坐标
- `block_type`: 块类型
- `level`: 标题层级
- `image`: 图片数据
- `table_data`: 表格数据
- `layout_type`: 布局类型
- `confidence`: OCR 置信度
- `language`: 语言
- `metadata`: 元数据
```

- [ ] **Step 2: 更新 README.md**

在 README.md 的特性部分添加：

```markdown
- ✅ 支持 9 种文档格式解析（PDF, Word, Excel, PPT, HTML, Markdown, TXT, JSON, EPUB）
- ✅ RAGFlow 深度解析（OCR + 布局识别 + 表格识别）
- ✅ 懒加载机制优化启动性能
```

- [ ] **Step 3: 运行最终验证**

```bash
# 验证导入
python -c "from openrag.parsers import ParserRegistry, DocumentBlock; print('✓ Imports OK')"

# 验证工厂
python -c "from openrag.parsers.factory import ParserFactory; f = ParserFactory(); print(f'✓ Factory supports {len(f._parser_classes)} extensions')"

# 运行所有测试
pytest tests/ -v --tb=short

# 检查代码风格
flake8 src/openrag/parsers/ --max-line-length=120
```

Expected: 所有检查通过

- [ ] **Step 4: 最终 Commit**

```bash
git add docs/parsers.md README.md
git commit -m "docs: add parser documentation and update README

- Document 9 supported formats
- Add usage examples
- Explain lazy loading mechanism
- Document DocumentBlock fields

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## 完成检查清单

- [ ] RAGFlow 代码已复制到项目中
- [ ] DocumentBlock 类已扩展
- [ ] ParserFactory 已实现
- [ ] 9 个适配器已实现
- [ ] ParserRegistry 已更新
- [ ] TextBlock 已全局替换为 DocumentBlock
- [ ] ALLOWED_MIME_TYPES 已扩展
- [ ] 依赖已添加到 requirements.txt
- [ ] 集成测试已通过
- [ ] 文档已更新

---

## 预期成果

1. **功能完整**：支持 9 种文档格式的深度解析
2. **性能优化**：懒加载机制避免启动时加载所有模型
3. **架构清晰**：适配器模式实现格式转换
4. **测试完善**：单元测试 + 集成测试覆盖
5. **文档齐全**：使用文档 + API 文档

## 后续工作

1. 添加更多测试文件（PDF, DOCX 等）
2. 优化大文件解析性能
3. 添加解析进度反馈
4. 实现解析结果缓存
5. 添加错误处理和日志

