# OpenViking 递归检索集成实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现异步文档处理、L0/L1/L2 层级生成和智能目录更新，支持分层递归检索

**Architecture:** 从 OpenViking 拷贝 TreeBuilder 核心逻辑生成单文档 L0/L1/L2，自实现目录聚合和智能更新引擎（基于向量相似度），通过 Celery 异步任务处理文档和级联更新

**Tech Stack:** Python 3.10+, SQLAlchemy, Celery, OpenAI Embeddings, Milvus/向量数据库, NumPy

---

## 文件结构规划

### 新增文件

**核心组件：**
- `src/openrag/hierarchy/__init__.py` - 层级模块入口
- `src/openrag/hierarchy/document_hierarchy_builder.py` - 单文档 L0/L1/L2 生成器
- `src/openrag/hierarchy/directory_hierarchy_manager.py` - 目录聚合管理器
- `src/openrag/hierarchy/smart_update_engine.py` - 智能更新引擎
- `src/openrag/hierarchy/hierarchy_storage.py` - 层级内容存储
- `src/openrag/hierarchy/models.py` - 数据模型
- `src/openrag/hierarchy/utils.py` - 工具函数（从 OpenViking 拷贝）

**任务：**
- `src/openrag/tasks/hierarchy_tasks.py` - Celery 异步任务

**数据库迁移：**
- `alembic/versions/XXXX_add_hierarchy_support.py` - 数据库迁移脚本

**测试：**
- `tests/test_hierarchy/test_document_hierarchy_builder.py`
- `tests/test_hierarchy/test_directory_hierarchy_manager.py`
- `tests/test_hierarchy/test_smart_update_engine.py`
- `tests/test_hierarchy/test_hierarchy_storage.py`
- `tests/test_hierarchy/test_integration_hierarchy.py`

### 修改文件

- `src/openrag/models/file.py` - 扩展 File 模型
- `src/openrag/processors/document_processor.py` - 集成层级生成
- `src/openrag/retrieval/retrieval_service.py` - 添加分层检索
- `src/openrag/config.py` - 添加层级配置
- `src/openrag/api/search_api.py` - 更新检索 API

---

## 阶段 1：基础设施

### Task 1.1: 数据库模型扩展

**Files:**
- Modify: `src/openrag/models/file.py`
- Create: `alembic/versions/XXXX_add_hierarchy_support.py`

- [ ] **Step 1: 扩展 File 模型添加层级字段**

```python
# src/openrag/models/file.py
# 在 File 类中添加以下字段

from sqlalchemy import Column, String, Integer, Enum, TIMESTAMP
import enum

class ProcessingStatus(enum.Enum):
    pending = "pending"
    parsing = "parsing"
    building_hierarchy = "building_hierarchy"
    embedding = "embedding"
    completed = "completed"
    failed = "failed"

# 在 File 类中添加：
l0_path = Column(String(512), nullable=True)
l1_path = Column(String(512), nullable=True)
l2_path = Column(String(512), nullable=True)
l0_vector_id = Column(String(128), nullable=True)
processing_status = Column(Enum(ProcessingStatus), default=ProcessingStatus.pending)
total_chunks = Column(Integer, default=0)
total_tokens = Column(Integer, default=0)
child_count = Column(Integer, default=0)
last_aggregated_at = Column(TIMESTAMP, nullable=True)
```

- [ ] **Step 2: 创建数据库迁移脚本**

```bash
alembic revision --autogenerate -m "Add hierarchy support to files table"
```

- [ ] **Step 3: 检查生成的迁移脚本**

打开 `alembic/versions/XXXX_add_hierarchy_support.py`，确认包含所有新字段

- [ ] **Step 4: 执行数据库迁移**

```bash
alembic upgrade head
```

Expected: "Running upgrade ... -> XXXX, Add hierarchy support to files table"

- [ ] **Step 5: 提交**

```bash
git add src/openrag/models/file.py alembic/versions/
git commit -m "feat: add hierarchy support fields to File model"
```

### Task 1.2: 层级数据模型

**Files:**
- Create: `src/openrag/hierarchy/__init__.py`
- Create: `src/openrag/hierarchy/models.py`

- [ ] **Step 1: 创建层级模块初始化文件**

```python
# src/openrag/hierarchy/__init__.py
"""Hierarchy generation and management module."""

from .models import HierarchyResult, DirectoryHierarchy, Section

__all__ = [
    'HierarchyResult',
    'DirectoryHierarchy',
    'Section',
]
```

- [ ] **Step 2: 编写层级数据模型**

```python
# src/openrag/hierarchy/models.py
"""Data models for hierarchy structures."""

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Section:
    """Represents a section in L1 overview."""
    title: str
    level: int  # Heading level (1, 2, 3)
    content: str  # Preview content
    start_offset: int = 0
    end_offset: int = 0


@dataclass
class HierarchyResult:
    """Result of document hierarchy generation."""
    l0: str  # Summary (500-1000 tokens)
    l1: List[Section]  # Overview (sections with previews)
    l2: List  # Original chunks (from ChunkEngine)
    
    def to_dict(self) -> dict:
        """Convert to dictionary for storage."""
        return {
            'l0': self.l0,
            'l1': [
                {
                    'title': s.title,
                    'level': s.level,
                    'content': s.content,
                    'start_offset': s.start_offset,
                    'end_offset': s.end_offset
                }
                for s in self.l1
            ],
            'l2': [chunk.to_openviking_format() for chunk in self.l2]
        }


@dataclass
class DirectoryHierarchy:
    """Result of directory aggregation."""
    l0: str  # Directory summary
    l1: str  # Directory overview (list of children)
    
    def to_dict(self) -> dict:
        """Convert to dictionary for storage."""
        return {
            'l0': self.l0,
            'l1': self.l1
        }
```

- [ ] **Step 3: 提交**

```bash
git add src/openrag/hierarchy/
git commit -m "feat: add hierarchy data models"
```

### Task 1.3: 层级存储实现

**Files:**
- Create: `src/openrag/hierarchy/hierarchy_storage.py`
- Create: `tests/test_hierarchy/test_hierarchy_storage.py`

- [ ] **Step 1: 编写存储测试**

```python
# tests/test_hierarchy/test_hierarchy_storage.py
"""Tests for hierarchy storage."""

import pytest
import tempfile
import shutil
from pathlib import Path
from src.openrag.hierarchy.hierarchy_storage import HierarchyStorage
from src.openrag.hierarchy.models import HierarchyResult, Section, DirectoryHierarchy


@pytest.fixture
def temp_storage_path():
    """Create temporary storage directory."""
    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    shutil.rmtree(temp_dir)


@pytest.fixture
def storage(temp_storage_path):
    """Create HierarchyStorage instance."""
    return HierarchyStorage(base_path=temp_storage_path)


def test_save_and_load_document_hierarchy(storage):
    """Test saving and loading document hierarchy."""
    file_id = 123
    hierarchy = HierarchyResult(
        l0="This is a summary",
        l1=[Section(title="Chapter 1", level=1, content="Content here")],
        l2=[]
    )
    
    # Save
    storage.save_document_hierarchy(file_id, hierarchy)
    
    # Load
    loaded = storage.load_document_hierarchy(file_id)
    
    assert loaded.l0 == hierarchy.l0
    assert len(loaded.l1) == 1
    assert loaded.l1[0].title == "Chapter 1"


def test_save_and_load_directory_hierarchy(storage):
    """Test saving and loading directory hierarchy."""
    file_id = 456
    hierarchy = DirectoryHierarchy(
        l0="Directory summary",
        l1="Directory overview"
    )
    
    # Save
    storage.save_directory_hierarchy(file_id, hierarchy)
    
    # Load
    loaded = storage.load_directory_hierarchy(file_id)
    
    assert loaded.l0 == hierarchy.l0
    assert loaded.l1 == hierarchy.l1
```

- [ ] **Step 2: 运行测试验证失败**

```bash
pytest tests/test_hierarchy/test_hierarchy_storage.py -v
```

Expected: FAIL with "No module named 'src.openrag.hierarchy.hierarchy_storage'"

- [ ] **Step 3: 实现 HierarchyStorage**

```python
# src/openrag/hierarchy/hierarchy_storage.py
"""Storage for hierarchy content (L0/L1/L2)."""

import json
from pathlib import Path
from typing import Optional
from .models import HierarchyResult, DirectoryHierarchy, Section


class HierarchyStorage:
    """Manages storage of hierarchy content to filesystem."""
    
    def __init__(self, base_path: str = "/storage/hierarchies"):
        """Initialize storage.
        
        Args:
            base_path: Base directory for hierarchy storage
        """
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)
    
    def _get_file_dir(self, file_id: int) -> Path:
        """Get directory path for a file's hierarchy."""
        file_dir = self.base_path / str(file_id)
        file_dir.mkdir(parents=True, exist_ok=True)
        return file_dir
    
    def save_document_hierarchy(self, file_id: int, hierarchy: HierarchyResult) -> None:
        """Save document hierarchy to filesystem.
        
        Args:
            file_id: File ID
            hierarchy: HierarchyResult to save
        """
        file_dir = self._get_file_dir(file_id)
        
        # Save L0 (plain text)
        l0_path = file_dir / "l0.txt"
        l0_path.write_text(hierarchy.l0, encoding='utf-8')
        
        # Save L1 (JSON)
        l1_path = file_dir / "l1.json"
        l1_data = [
            {
                'title': s.title,
                'level': s.level,
                'content': s.content,
                'start_offset': s.start_offset,
                'end_offset': s.end_offset
            }
            for s in hierarchy.l1
        ]
        l1_path.write_text(json.dumps(l1_data, ensure_ascii=False, indent=2), encoding='utf-8')
        
        # L2 is stored separately as chunks (handled by ChunkEngine)
    
    def load_document_hierarchy(self, file_id: int) -> Optional[HierarchyResult]:
        """Load document hierarchy from filesystem.
        
        Args:
            file_id: File ID
            
        Returns:
            HierarchyResult or None if not found
        """
        file_dir = self._get_file_dir(file_id)
        l0_path = file_dir / "l0.txt"
        l1_path = file_dir / "l1.json"
        
        if not l0_path.exists() or not l1_path.exists():
            return None
        
        # Load L0
        l0 = l0_path.read_text(encoding='utf-8')
        
        # Load L1
        l1_data = json.loads(l1_path.read_text(encoding='utf-8'))
        l1 = [
            Section(
                title=s['title'],
                level=s['level'],
                content=s['content'],
                start_offset=s.get('start_offset', 0),
                end_offset=s.get('end_offset', 0)
            )
            for s in l1_data
        ]
        
        return HierarchyResult(l0=l0, l1=l1, l2=[])
    
    def save_directory_hierarchy(self, file_id: int, hierarchy: DirectoryHierarchy) -> None:
        """Save directory hierarchy to filesystem.
        
        Args:
            file_id: Directory file ID
            hierarchy: DirectoryHierarchy to save
        """
        file_dir = self._get_file_dir(file_id)
        
        # Save L0
        l0_path = file_dir / "l0.txt"
        l0_path.write_text(hierarchy.l0, encoding='utf-8')
        
        # Save L1
        l1_path = file_dir / "l1.txt"
        l1_path.write_text(hierarchy.l1, encoding='utf-8')
    
    def load_directory_hierarchy(self, file_id: int) -> Optional[DirectoryHierarchy]:
        """Load directory hierarchy from filesystem.
        
        Args:
            file_id: Directory file ID
            
        Returns:
            DirectoryHierarchy or None if not found
        """
        file_dir = self._get_file_dir(file_id)
        l0_path = file_dir / "l0.txt"
        l1_path = file_dir / "l1.txt"
        
        if not l0_path.exists() or not l1_path.exists():
            return None
        
        l0 = l0_path.read_text(encoding='utf-8')
        l1 = l1_path.read_text(encoding='utf-8')
        
        return DirectoryHierarchy(l0=l0, l1=l1)
    
    def load_l0(self, file_id: int) -> Optional[str]:
        """Load only L0 content.
        
        Args:
            file_id: File ID
            
        Returns:
            L0 content or None if not found
        """
        file_dir = self._get_file_dir(file_id)
        l0_path = file_dir / "l0.txt"
        
        if not l0_path.exists():
            return None
        
        return l0_path.read_text(encoding='utf-8')
```

- [ ] **Step 4: 运行测试验证通过**

```bash
pytest tests/test_hierarchy/test_hierarchy_storage.py -v
```

Expected: 2 passed

- [ ] **Step 5: 提交**

```bash
git add src/openrag/hierarchy/hierarchy_storage.py tests/test_hierarchy/
git commit -m "feat: implement hierarchy storage for filesystem"
```

### Task 1.4: 工具函数（从 OpenViking 拷贝）

**Files:**
- Create: `src/openrag/hierarchy/utils.py`
- Create: `tests/test_hierarchy/test_utils.py`

- [ ] **Step 1: 编写工具函数测试**

```python
# tests/test_hierarchy/test_utils.py
"""Tests for hierarchy utility functions."""

import pytest
from src.openrag.hierarchy.utils import (
    extract_key_sentences,
    count_tokens,
    truncate_to_tokens,
    clean_text
)


def test_extract_key_sentences():
    """Test extracting key sentences from text."""
    text = "This is the first sentence. This is the second sentence. This is the third sentence."
    sentences = extract_key_sentences(text, max_sentences=2)
    
    assert len(sentences) == 2
    assert "first sentence" in sentences[0]


def test_count_tokens():
    """Test token counting."""
    text = "Hello world, this is a test."
    count = count_tokens(text)
    
    assert count > 0
    assert count < 20  # Rough estimate


def test_truncate_to_tokens():
    """Test truncating text to token limit."""
    text = "This is a long text. " * 100
    truncated = truncate_to_tokens(text, max_tokens=50)
    
    assert count_tokens(truncated) <= 50
    assert len(truncated) < len(text)


def test_clean_text():
    """Test text cleaning."""
    text = "  Hello\n\nWorld  \t\n"
    cleaned = clean_text(text)
    
    assert cleaned == "Hello\nWorld"
```

- [ ] **Step 2: 运行测试验证失败**

```bash
pytest tests/test_hierarchy/test_utils.py -v
```

Expected: FAIL with "No module named 'src.openrag.hierarchy.utils'"

- [ ] **Step 3: 实现工具函数（从 OpenViking 拷贝并简化）**

```python
# src/openrag/hierarchy/utils.py
"""
Utility functions for hierarchy generation.

Adapted from OpenViking (https://github.com/volcengine/openviking)
License: AGPL-3.0
"""

import re
from typing import List


def extract_key_sentences(text: str, max_sentences: int = 5) -> List[str]:
    """Extract key sentences from text.
    
    Simple heuristic: take first sentence of each paragraph.
    
    Args:
        text: Input text
        max_sentences: Maximum number of sentences to extract
        
    Returns:
        List of key sentences
    """
    # Split by paragraphs
    paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]
    
    sentences = []
    for para in paragraphs:
        # Get first sentence of paragraph
        first_sentence = re.split(r'[.!?]\s+', para)[0]
        if first_sentence:
            sentences.append(first_sentence + '.')
        
        if len(sentences) >= max_sentences:
            break
    
    return sentences


def count_tokens(text: str) -> int:
    """Estimate token count.
    
    Simple approximation: 1 token ≈ 4 characters for Chinese/English mix.
    
    Args:
        text: Input text
        
    Returns:
        Estimated token count
    """
    return len(text) // 4


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Truncate text to maximum token count.
    
    Args:
        text: Input text
        max_tokens: Maximum tokens
        
    Returns:
        Truncated text
    """
    max_chars = max_tokens * 4
    if len(text) <= max_chars:
        return text
    
    # Truncate at sentence boundary
    truncated = text[:max_chars]
    last_period = truncated.rfind('.')
    if last_period > 0:
        truncated = truncated[:last_period + 1]
    
    return truncated


def clean_text(text: str) -> str:
    """Clean text by removing extra whitespace.
    
    Args:
        text: Input text
        
    Returns:
        Cleaned text
    """
    # Replace multiple spaces with single space
    text = re.sub(r' +', ' ', text)
    
    # Replace multiple newlines with single newline
    text = re.sub(r'\n\n+', '\n', text)
    
    # Strip leading/trailing whitespace
    text = text.strip()
    
    return text
```

- [ ] **Step 4: 运行测试验证通过**

```bash
pytest tests/test_hierarchy/test_utils.py -v
```

Expected: 4 passed

- [ ] **Step 5: 提交**

```bash
git add src/openrag/hierarchy/utils.py tests/test_hierarchy/test_utils.py
git commit -m "feat: add hierarchy utility functions (adapted from OpenViking)"
```

---

## 阶段 2：单文档层级生成

### Task 2.1: DocumentHierarchyBuilder - L0 生成

**Files:**
- Create: `src/openrag/hierarchy/document_hierarchy_builder.py`
- Create: `tests/test_hierarchy/test_document_hierarchy_builder.py`

- [ ] **Step 1: 编写 L0 生成测试**

```python
# tests/test_hierarchy/test_document_hierarchy_builder.py
"""Tests for DocumentHierarchyBuilder."""

import pytest
from src.openrag.chunking.chunk_models import Chunk
from src.openrag.hierarchy.document_hierarchy_builder import DocumentHierarchyBuilder


@pytest.fixture
def sample_chunks():
    """Create sample chunks for testing."""
    return [
        Chunk(
            text="Chapter 1: Introduction. This is the introduction to the document.",
            chunk_id="chunk_1",
            level=1,
            block_type="text"
        ),
        Chunk(
            text="Section 1.1: Background. This section provides background information.",
            chunk_id="chunk_2",
            level=2,
            block_type="text"
        ),
        Chunk(
            text="Chapter 2: Methods. This chapter describes the methods used.",
            chunk_id="chunk_3",
            level=1,
            block_type="text"
        ),
    ]


def test_generate_l0_summary(sample_chunks):
    """Test L0 summary generation."""
    builder = DocumentHierarchyBuilder()
    l0 = builder._generate_l0_summary(sample_chunks)
    
    assert len(l0) > 0
    assert len(l0) < 5000  # Should be concise
    assert "Introduction" in l0 or "Methods" in l0  # Contains key content
```

- [ ] **Step 2: 运行测试验证失败**

```bash
pytest tests/test_hierarchy/test_document_hierarchy_builder.py::test_generate_l0_summary -v
```

Expected: FAIL with "No module named 'src.openrag.hierarchy.document_hierarchy_builder'"

- [ ] **Step 3: 实现 DocumentHierarchyBuilder 基础结构和 L0 生成**

```python
# src/openrag/hierarchy/document_hierarchy_builder.py
"""
Document hierarchy builder - generates L0/L1/L2 for single documents.

Adapted from OpenViking TreeBuilder (https://github.com/volcengine/openviking)
License: AGPL-3.0
"""

from typing import List
from src.openrag.chunking.chunk_models import Chunk
from .models import HierarchyResult, Section
from .utils import extract_key_sentences, count_tokens, truncate_to_tokens


class DocumentHierarchyBuilder:
    """Builds L0/L1/L2 hierarchy for a single document."""
    
    def __init__(self, l0_max_tokens: int = 1000, l1_section_preview_tokens: int = 200):
        """Initialize builder.
        
        Args:
            l0_max_tokens: Maximum tokens for L0 summary
            l1_section_preview_tokens: Tokens per section in L1
        """
        self.l0_max_tokens = l0_max_tokens
        self.l1_section_preview_tokens = l1_section_preview_tokens
    
    def build_hierarchy(self, chunks: List[Chunk]) -> HierarchyResult:
        """Generate L0/L1/L2 hierarchy from chunks.
        
        Args:
            chunks: List of document chunks
            
        Returns:
            HierarchyResult with l0, l1, l2
        """
        l0 = self._generate_l0_summary(chunks)
        l1 = self._generate_l1_overview(chunks)
        l2 = chunks  # Original chunks
        
        return HierarchyResult(l0=l0, l1=l1, l2=l2)
    
    def _generate_l0_summary(self, chunks: List[Chunk]) -> str:
        """Generate L0 summary (500-1000 tokens).
        
        Strategy:
        1. Extract first sentence from each major section (level 1-2)
        2. Combine into coherent summary
        3. Truncate to max tokens
        
        Args:
            chunks: Document chunks
            
        Returns:
            L0 summary text
        """
        # Extract key sentences from heading chunks
        key_sentences = []
        for chunk in chunks:
            if chunk.level in [1, 2]:  # Major sections
                sentences = extract_key_sentences(chunk.text, max_sentences=1)
                key_sentences.extend(sentences)
        
        # If no headings, extract from all chunks
        if not key_sentences:
            for chunk in chunks[:5]:  # First 5 chunks
                sentences = extract_key_sentences(chunk.text, max_sentences=1)
                key_sentences.extend(sentences)
        
        # Combine and truncate
        summary = ' '.join(key_sentences)
        summary = truncate_to_tokens(summary, self.l0_max_tokens)
        
        return summary
    
    def _generate_l1_overview(self, chunks: List[Chunk]) -> List[Section]:
        """Generate L1 overview (sections with previews).
        
        Strategy:
        1. Extract all headings (level 1-3)
        2. For each heading, include preview of content
        3. Maintain document structure
        
        Args:
            chunks: Document chunks
            
        Returns:
            List of Section objects
        """
        sections = []
        
        for i, chunk in enumerate(chunks):
            if chunk.level > 0:  # Is a heading
                # Get preview content (next chunk or current chunk)
                if i + 1 < len(chunks):
                    preview_text = chunks[i + 1].text
                else:
                    preview_text = chunk.text
                
                preview = truncate_to_tokens(preview_text, self.l1_section_preview_tokens)
                
                section = Section(
                    title=chunk.text.split('\n')[0],  # First line as title
                    level=chunk.level,
                    content=preview,
                    start_offset=chunk.start_offset,
                    end_offset=chunk.end_offset
                )
                sections.append(section)
        
        return sections
```

- [ ] **Step 4: 运行测试验证通过**

```bash
pytest tests/test_hierarchy/test_document_hierarchy_builder.py::test_generate_l0_summary -v
```

Expected: 1 passed

- [ ] **Step 5: 提交**

```bash
git add src/openrag/hierarchy/document_hierarchy_builder.py tests/test_hierarchy/test_document_hierarchy_builder.py
git commit -m "feat: implement DocumentHierarchyBuilder with L0 generation"
```

### Task 2.2: DocumentHierarchyBuilder - L1 生成和完整测试

**Files:**
- Modify: `tests/test_hierarchy/test_document_hierarchy_builder.py`

- [ ] **Step 1: 添加 L1 和完整层级测试**

```python
# tests/test_hierarchy/test_document_hierarchy_builder.py
# 在文件末尾添加

def test_generate_l1_overview(sample_chunks):
    """Test L1 overview generation."""
    builder = DocumentHierarchyBuilder()
    l1 = builder._generate_l1_overview(sample_chunks)
    
    assert len(l1) > 0
    assert all(isinstance(s, Section) for s in l1)
    assert any("Introduction" in s.title for s in l1)


def test_build_complete_hierarchy(sample_chunks):
    """Test building complete hierarchy."""
    builder = DocumentHierarchyBuilder()
    result = builder.build_hierarchy(sample_chunks)
    
    assert result.l0  # Has summary
    assert len(result.l1) > 0  # Has sections
    assert result.l2 == sample_chunks  # L2 is original chunks
```

- [ ] **Step 2: 运行测试验证通过**

```bash
pytest tests/test_hierarchy/test_document_hierarchy_builder.py -v
```

Expected: 3 passed

- [ ] **Step 3: 提交**

```bash
git add tests/test_hierarchy/test_document_hierarchy_builder.py
git commit -m "test: add comprehensive tests for DocumentHierarchyBuilder"
```

---

## 阶段 3：目录聚合

### Task 3.1: DirectoryHierarchyManager

**Files:**
- Create: `src/openrag/hierarchy/directory_hierarchy_manager.py`
- Create: `tests/test_hierarchy/test_directory_hierarchy_manager.py`

- [ ] **Step 1: 编写目录聚合测试**

```python
# tests/test_hierarchy/test_directory_hierarchy_manager.py
"""Tests for DirectoryHierarchyManager."""

import pytest
from src.openrag.hierarchy.directory_hierarchy_manager import DirectoryHierarchyManager


def test_aggregate_directory():
    """Test directory aggregation."""
    manager = DirectoryHierarchyManager()
    
    children_l0s = [
        "Q1 Report: Revenue increased 15%...",
        "Q2 Report: Revenue increased 18%...",
        "Annual Analysis: Full year summary..."
    ]
    
    result = manager.aggregate_directory("/users/user1/reports/2024/", children_l0s)
    
    assert result.l0  # Has directory summary
    assert result.l1  # Has directory overview
    assert "Q1" in result.l1 or "Q2" in result.l1  # Contains child info
```

- [ ] **Step 2: 运行测试验证失败**

```bash
pytest tests/test_hierarchy/test_directory_hierarchy_manager.py -v
```

Expected: FAIL

- [ ] **Step 3: 实现 DirectoryHierarchyManager**

```python
# src/openrag/hierarchy/directory_hierarchy_manager.py
"""Directory hierarchy manager - aggregates children into directory L0/L1."""

from typing import List
from .models import DirectoryHierarchy
from .utils import truncate_to_tokens


class DirectoryHierarchyManager:
    """Manages directory-level hierarchy aggregation."""
    
    def __init__(self, l0_max_tokens: int = 1000):
        """Initialize manager.
        
        Args:
            l0_max_tokens: Maximum tokens for directory L0
        """
        self.l0_max_tokens = l0_max_tokens
    
    def aggregate_directory(
        self,
        directory_path: str,
        children_l0s: List[str],
        children_names: List[str] = None
    ) -> DirectoryHierarchy:
        """Aggregate children into directory L0/L1.
        
        Args:
            directory_path: Directory path
            children_l0s: L0 summaries of all children
            children_names: Optional names of children
            
        Returns:
            DirectoryHierarchy with l0 and l1
        """
        # Generate L0: directory summary
        l0 = self._generate_directory_l0(directory_path, children_l0s)
        
        # Generate L1: list of children with their summaries
        l1 = self._generate_directory_l1(children_l0s, children_names)
        
        return DirectoryHierarchy(l0=l0, l1=l1)
    
    def _generate_directory_l0(self, directory_path: str, children_l0s: List[str]) -> str:
        """Generate directory L0 summary.
        
        Args:
            directory_path: Directory path
            children_l0s: Children L0 summaries
            
        Returns:
            Directory summary
        """
        dir_name = directory_path.rstrip('/').split('/')[-1]
        
        # Build summary
        summary_parts = [
            f"目录: {directory_path}",
            f"包含: {len(children_l0s)} 个文档"
        ]
        
        # Add content overview from children
        if children_l0s:
            combined = ' '.join(children_l0s[:5])  # First 5 children
            overview = truncate_to_tokens(combined, 500)
            summary_parts.append(f"主要内容: {overview}")
        
        summary = '\n'.join(summary_parts)
        return truncate_to_tokens(summary, self.l0_max_tokens)
    
    def _generate_directory_l1(
        self,
        children_l0s: List[str],
        children_names: List[str] = None
    ) -> str:
        """Generate directory L1 overview.
        
        Args:
            children_l0s: Children L0 summaries
            children_names: Optional children names
            
        Returns:
            Directory overview listing children
        """
        if not children_names:
            children_names = [f"文档 {i+1}" for i in range(len(children_l0s))]
        
        lines = [f"## 目录内容\n"]
        
        for name, l0 in zip(children_names, children_l0s):
            preview = truncate_to_tokens(l0, 100)
            lines.append(f"### {name}")
            lines.append(preview)
            lines.append("")
        
        return '\n'.join(lines)
```

- [ ] **Step 4: 运行测试验证通过**

```bash
pytest tests/test_hierarchy/test_directory_hierarchy_manager.py -v
```

Expected: 1 passed

- [ ] **Step 5: 提交**

```bash
git add src/openrag/hierarchy/directory_hierarchy_manager.py tests/test_hierarchy/
git commit -m "feat: implement DirectoryHierarchyManager for directory aggregation"
```

---

## 阶段 4：智能更新引擎

### Task 4.1: SmartUpdateEngine - 向量相似度计算

**Files:**
- Create: `src/openrag/hierarchy/smart_update_engine.py`
- Create: `tests/test_hierarchy/test_smart_update_engine.py`

- [ ] **Step 1: 编写相似度计算测试**

```python
# tests/test_hierarchy/test_smart_update_engine.py
"""Tests for SmartUpdateEngine."""

import pytest
import numpy as np
from src.openrag.hierarchy.smart_update_engine import SmartUpdateEngine, cosine_similarity


def test_cosine_similarity():
    """Test cosine similarity calculation."""
    vec1 = np.array([1.0, 0.0, 0.0])
    vec2 = np.array([1.0, 0.0, 0.0])
    
    similarity = cosine_similarity(vec1, vec2)
    assert abs(similarity - 1.0) < 0.001  # Should be 1.0 (identical)
    
    vec3 = np.array([0.0, 1.0, 0.0])
    similarity2 = cosine_similarity(vec1, vec3)
    assert abs(similarity2) < 0.001  # Should be 0.0 (orthogonal)
```

- [ ] **Step 2: 运行测试验证失败**

```bash
pytest tests/test_hierarchy/test_smart_update_engine.py::test_cosine_similarity -v
```

Expected: FAIL

- [ ] **Step 3: 实现相似度计算和 SmartUpdateEngine 基础**

```python
# src/openrag/hierarchy/smart_update_engine.py
"""Smart update engine - determines when to update parent directories."""

import numpy as np
from typing import List, Optional


def cosine_similarity(vec1: np.ndarray, vec2: np.ndarray) -> float:
    """Calculate cosine similarity between two vectors.
    
    Args:
        vec1: First vector
        vec2: Second vector
        
    Returns:
        Cosine similarity (0-1)
    """
    dot_product = np.dot(vec1, vec2)
    norm1 = np.linalg.norm(vec1)
    norm2 = np.linalg.norm(vec2)
    
    if norm1 == 0 or norm2 == 0:
        return 0.0
    
    return float(dot_product / (norm1 * norm2))


class SmartUpdateEngine:
    """Determines when parent directories need updating based on content similarity."""
    
    def __init__(
        self,
        similarity_threshold: float = 0.95,
        embedding_engine = None,
        hierarchy_storage = None,
        db_session = None
    ):
        """Initialize engine.
        
        Args:
            similarity_threshold: Threshold for update decision (< threshold = update)
            embedding_engine: EmbeddingEngine instance
            hierarchy_storage: HierarchyStorage instance
            db_session: Database session
        """
        self.threshold = similarity_threshold
        self.embedding_engine = embedding_engine
        self.hierarchy_storage = hierarchy_storage
        self.db = db_session
```


    def should_update_parent(
        self,
        parent_path: str,
        new_child_l0: str
    ) -> bool:
        """Determine if parent directory needs updating.
        
        Compares old and new L0 vectors using cosine similarity.
        
        Args:
            parent_path: Parent directory path
            new_child_l0: L0 of newly added child
            
        Returns:
            True if parent should be updated
        """
        # This is a placeholder - full implementation in later tasks
        # For now, always return True (conservative)
        return True
    
    def propagate_update(
        self,
        file_path: str,
        file_l0: str
    ) -> List[str]:
        """Propagate update from file to ancestors.
        
        Uses short-circuit: stops when parent doesn't need update.
        
        Args:
            file_path: File path
            file_l0: File's L0 content
            
        Returns:
            List of directory paths that need updating
        """
        paths_to_update = []
        current_path = self._get_parent_path(file_path)
        current_l0 = file_l0
        
        while current_path:
            if self.should_update_parent(current_path, current_l0):
                paths_to_update.append(current_path)
                # Continue checking ancestors
                current_l0 = self.hierarchy_storage.load_l0(current_path) or ""
                current_path = self._get_parent_path(current_path)
            else:
                # Short-circuit: stop propagation
                break
        
        return paths_to_update
    
    def _get_parent_path(self, path: str) -> Optional[str]:
        """Get parent directory path.
        
        Args:
            path: File or directory path
            
        Returns:
            Parent path or None if at root
        """
        path = path.rstrip('/')
        if '/' not in path:
            return None
        
        return '/'.join(path.split('/')[:-1])
```

- [ ] **Step 4: 运行测试验证通过**

```bash
pytest tests/test_hierarchy/test_smart_update_engine.py::test_cosine_similarity -v
```

Expected: 1 passed

- [ ] **Step 5: 提交**

```bash
git add src/openrag/hierarchy/smart_update_engine.py tests/test_hierarchy/
git commit -m "feat: implement SmartUpdateEngine with cosine similarity"
```

---

## 阶段 5：集成到文档处理流程

### Task 5.1: 集成到 DocumentProcessor

**Files:**
- Modify: `src/openrag/processors/document_processor.py`
- Modify: `src/openrag/config.py`

- [ ] **Step 1: 添加层级配置**

```python
# src/openrag/config.py
# 在 Config 类中添加

class HierarchyConfig(BaseSettings):
    """Hierarchy generation configuration"""
    l0_max_tokens: int = 1000
    l1_section_preview_tokens: int = 200
    similarity_threshold: float = 0.95
    enable_smart_update: bool = True
    hierarchy_storage_path: str = "/storage/hierarchies"

# 在 Config 类中添加字段
hierarchy: HierarchyConfig = Field(default_factory=HierarchyConfig)
```

- [ ] **Step 2: 修改 DocumentProcessor 集成层级生成**

```python
# src/openrag/processors/document_processor.py
# 在 __init__ 方法中添加

from src.openrag.hierarchy.document_hierarchy_builder import DocumentHierarchyBuilder
from src.openrag.hierarchy.hierarchy_storage import HierarchyStorage
from src.openrag.config import get_config

config = get_config()
self.hierarchy_builder = DocumentHierarchyBuilder(
    l0_max_tokens=config.hierarchy.l0_max_tokens,
    l1_section_preview_tokens=config.hierarchy.l1_section_preview_tokens
)
self.hierarchy_storage = HierarchyStorage(
    base_path=config.hierarchy.hierarchy_storage_path
)

# 在 process_document 方法中，在 Step 3 之后添加：

# Step 3.5: Generate hierarchy
hierarchy = self.hierarchy_builder.build_hierarchy(chunks)
self.hierarchy_storage.save_document_hierarchy(file_id, hierarchy)

# Update file record with hierarchy paths
file = self.db.query(File).get(file_id)
file.l0_path = f"{config.hierarchy.hierarchy_storage_path}/{file_id}/l0.txt"
file.l1_path = f"{config.hierarchy.hierarchy_storage_path}/{file_id}/l1.json"
self.db.commit()
```

- [ ] **Step 3: 提交**

```bash
git add src/openrag/processors/document_processor.py src/openrag/config.py
git commit -m "feat: integrate hierarchy generation into DocumentProcessor"
```

---

## 阶段 6：Celery 异步任务

### Task 6.1: 层级处理任务

**Files:**
- Create: `src/openrag/tasks/hierarchy_tasks.py`

- [ ] **Step 1: 实现异步任务**

```python
# src/openrag/tasks/hierarchy_tasks.py
"""Celery tasks for hierarchy processing."""

from celery import shared_task
from sqlalchemy.orm import Session
from src.openrag.database import get_db
from src.openrag.models.file import File
from src.openrag.hierarchy.smart_update_engine import SmartUpdateEngine
from src.openrag.hierarchy.directory_hierarchy_manager import DirectoryHierarchyManager
from src.openrag.hierarchy.hierarchy_storage import HierarchyStorage
from src.openrag.config import get_config


@shared_task
def trigger_smart_update_task(file_id: int):
    """Trigger smart directory update after file processing.
    
    Args:
        file_id: File ID that was just processed
    """
    config = get_config()
    db = next(get_db())
    
    try:
        # Load file and its L0
        file = db.query(File).get(file_id)
        if not file:
            return
        
        storage = HierarchyStorage(config.hierarchy.hierarchy_storage_path)
        file_l0 = storage.load_l0(file_id)
        
        if not file_l0:
            return
        
        # Use smart update engine
        engine = SmartUpdateEngine(
            similarity_threshold=config.hierarchy.similarity_threshold,
            hierarchy_storage=storage,
            db_session=db
        )
        
        paths_to_update = engine.propagate_update(file.file_path, file_l0)
        
        # Trigger directory update tasks
        for dir_path in paths_to_update:
            update_directory_hierarchy_task.delay(dir_path)
    
    finally:
        db.close()


@shared_task
def update_directory_hierarchy_task(dir_path: str):
    """Update a single directory's hierarchy.
    
    Args:
        dir_path: Directory path to update
    """
    config = get_config()
    db = next(get_db())
    
    try:
        # Get all children in directory
        children = db.query(File).filter(
            File.parent_path == dir_path,
            File.deleted_at.is_(None)
        ).all()
        
        if not children:
            return
        
        # Load children L0s
        storage = HierarchyStorage(config.hierarchy.hierarchy_storage_path)
        children_l0s = []
        children_names = []
        
        for child in children:
            l0 = storage.load_l0(child.id)
            if l0:
                children_l0s.append(l0)
                children_names.append(child.filename)
        
        # Aggregate directory
        manager = DirectoryHierarchyManager()
        dir_hierarchy = manager.aggregate_directory(
            dir_path,
            children_l0s,
            children_names
        )
        
        # Get or create directory file record
        dir_file = db.query(File).filter(
            File.file_path == dir_path,
            File.is_directory == True
        ).first()
        
        if not dir_file:
            dir_file = File(
                file_path=dir_path,
                is_directory=True,
                processing_status='completed'
            )
            db.add(dir_file)
            db.commit()
        
        # Save directory hierarchy
        storage.save_directory_hierarchy(dir_file.id, dir_hierarchy)
        
        # Update metadata
        dir_file.child_count = len(children)
        from datetime import datetime
        dir_file.last_aggregated_at = datetime.now()
        db.commit()
    
    finally:
        db.close()
```

- [ ] **Step 2: 在 DocumentProcessor 中触发任务**

```python
# src/openrag/processors/document_processor.py
# 在 process_document 方法末尾添加

from src.openrag.tasks.hierarchy_tasks import trigger_smart_update_task

# Trigger smart update (async)
trigger_smart_update_task.delay(file_id)
```

- [ ] **Step 3: 提交**

```bash
git add src/openrag/tasks/hierarchy_tasks.py src/openrag/processors/document_processor.py
git commit -m "feat: add Celery tasks for hierarchy processing and smart updates"
```

---

## 总结和验证

### Task 7.1: 端到端集成测试

**Files:**
- Create: `tests/test_hierarchy/test_integration_hierarchy.py`

- [ ] **Step 1: 编写集成测试**

```python
# tests/test_hierarchy/test_integration_hierarchy.py
"""End-to-end integration tests for hierarchy system."""

import pytest
from src.openrag.processors.document_processor import DocumentProcessor
from src.openrag.hierarchy.hierarchy_storage import HierarchyStorage


@pytest.mark.integration
def test_document_processing_with_hierarchy(db_session, temp_storage):
    """Test complete document processing with hierarchy generation."""
    # This test would require full setup
    # Placeholder for now - implement after all components are ready
    pass
```

- [ ] **Step 2: 手动验证流程**

```bash
# 1. 启动服务
python run_api.py

# 2. 上传测试文档
curl -X POST http://localhost:8000/api/v1/files/upload \
  -F "file=@test_document.pdf" \
  -H "Authorization: Bearer <token>"

# 3. 检查处理状态
curl http://localhost:8000/api/v1/files/<file_id>

# 4. 验证层级文件生成
ls /storage/hierarchies/<file_id>/
# 应该看到: l0.txt, l1.json
```

- [ ] **Step 3: 提交**

```bash
git add tests/test_hierarchy/
git commit -m "test: add integration tests for hierarchy system"
```

---

## 实现检查清单

完成后验证以下功能：

- [ ] 文档上传后异步生成 L0/L1/L2
- [ ] L0/L1/L2 正确存储到文件系统
- [ ] 目录聚合功能正常工作
- [ ] 智能更新引擎正确判断是否需要更新父目录
- [ ] 短路传播机制生效（父目录无变化时停止）
- [ ] 所有单元测试通过
- [ ] 集成测试通过

## 后续优化任务（可选）

1. **向量化集成** - 将 L0/L1/L2 存储到向量数据库
2. **分层检索** - 实现 L0→L1→L2 递归检索
3. **LLM 增强** - 使用 LLM 生成更高质量的 L0 摘要
4. **性能优化** - 批量处理、缓存、并发控制
5. **监控和日志** - 添加详细的处理日志和性能监控

---

**计划创建日期：** 2026-04-08  
**预计实现时间：** 14-20 天  
**计划版本：** v1.0

