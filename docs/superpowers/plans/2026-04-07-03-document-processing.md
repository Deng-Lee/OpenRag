# 计划 3：文档处理系统

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 集成 RAGFlow 文档处理能力，实现增强切片引擎，与 OpenViking 存储集成

**Architecture:** RAGFlow Parser 解析 → 增强切片引擎 → OpenViking TreeBuilder → VikingFS 存储 → 向量化

**Tech Stack:** RAGFlow deepdoc, OpenViking, Celery (异步任务), pymilvus

**Dependencies:** 
- 计划 1（环境搭建与基础集成）必须完成
- 计划 2（权限管理系统）必须完成

**Priority:** P1（核心功能）

---

## 文件结构

```
src/openrag/
├── parsers/
│   ├── __init__.py
│   ├── base.py                     # 解析器基类
│   ├── ragflow_parser.py           # RAGFlow 解析器封装
│   └── parser_registry.py          # 解析器注册表
├── chunking/
│   ├── __init__.py
│   ├── chunk_engine.py             # 增强切片引擎
│   └── chunk_models.py             # 切片数据模型
├── embedding/
│   ├── __init__.py
│   └── embedding_engine.py         # Embedding 引擎
├── processors/
│   ├── __init__.py
│   └── document_processor.py       # 文档处理编排
└── tasks/
    ├── __init__.py
    └── celery_tasks.py             # Celery 异步任务

tests/
├── test_ragflow_parser.py          # RAGFlow 解析器测试
├── test_chunk_engine.py            # 切片引擎测试
├── test_embedding_engine.py        # Embedding 测试
└── test_document_processor.py      # 文档处理测试
```

---

### Task 1: RAGFlow 解析器封装

**Files:**
- Create: `src/openrag/parsers/base.py`
- Create: `src/openrag/parsers/ragflow_parser.py`
- Create: `tests/test_ragflow_parser.py`

**主要步骤：**
- 定义解析器基类接口
- 封装 RAGFlow PDF Parser
- 封装 RAGFlow Markdown Parser
- 提取文本块和位置信息
- 编写解析器测试

---

### Task 2: 增强切片引擎

**Files:**
- Create: `src/openrag/chunking/chunk_models.py`
- Create: `src/openrag/chunking/chunk_engine.py`
- Create: `tests/test_chunk_engine.py`

**主要步骤：**
- 定义切片数据模型（包含位置信息）
- 实现段落切片策略
- 实现语义切片策略（使用 RAGFlow tokenizer）
- 实现固定长度切片策略
- 保留完整位置信息（页码、偏移、坐标、层级）
- 转换为 OpenViking 兼容格式
- 编写切片引擎测试

---

### Task 3: Embedding 引擎

**Files:**
- Create: `src/openrag/embedding/embedding_engine.py`
- Create: `tests/test_embedding_engine.py`

**主要步骤：**
- 实现 OpenAI Embedding 接口
- 实现批量向量化
- 实现向量缓存
- 编写 Embedding 测试

---

### Task 4: 文档处理编排

**Files:**
- Create: `src/openrag/processors/document_processor.py`
- Create: `tests/test_document_processor.py`

**主要步骤：**
- 实现完整文档处理流程
- 集成 RAGFlow Parser
- 集成增强切片引擎
- 集成 OpenViking TreeBuilder
- 集成 VikingFS 存储
- 权限检查集成
- 编写文档处理测试

---

### Task 5: Celery 异步任务

**Files:**
- Create: `src/openrag/tasks/celery_tasks.py`
- Create: `celery_config.py`

**主要步骤：**
- 配置 Celery
- 实现异步向量化任务
- 实现任务状态跟踪
- 编写异步任务测试

---

### Task 6: 端到端集成测试

**Files:**
- Create: `tests/test_integration_document_processing.py`

**主要步骤：**
- 准备测试文档（PDF、Markdown）
- 测试完整文档上传流程
- 验证 VikingFS 存储
- 验证向量数据库存储
- 验证 L0/L1/L2 生成
- 验证位置信息保存

---

## 验收标准

完成此计划后，应该具备：

- ✅ RAGFlow 文档解析功能正常
- ✅ 增强切片引擎支持多种策略
- ✅ 完整位置信息保存（页码、偏移、坐标、层级）
- ✅ OpenViking 存储集成正常
- ✅ 向量化和存储正常
- ✅ 异步任务队列正常工作
- ✅ 所有测试通过
- ✅ 可以上传和处理文档

## 下一步

完成此计划后，继续执行：
- **计划 4：检索服务**
