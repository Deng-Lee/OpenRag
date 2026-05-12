# 计划 4：检索服务

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现检索服务，集成 OpenViking 分层检索，添加权限过滤，实现 Rerank 重排序

**Architecture:** OpenViking 分层检索 + 权限过滤层 + Rerank（利用位置信息和层级结构）

**Tech Stack:** OpenViking Hierarchical Retriever, Rerank 模型, 权限管理

**Dependencies:** 
- 计划 1（环境搭建与基础集成）必须完成
- 计划 2（权限管理系统）必须完成
- 计划 3（文档处理系统）必须完成

**Priority:** P1（核心功能）

---

## 文件结构

```
src/openrag/
├── retrieval/
│   ├── __init__.py
│   ├── retrieval_service.py        # 检索服务主类
│   ├── reranker.py                 # Rerank 重排序
│   └── filters.py                  # 权限过滤器
└── api/
    ├── __init__.py
    └── search_api.py               # 搜索 API

tests/
├── test_retrieval_service.py       # 检索服务测试
├── test_reranker.py                # Rerank 测试
└── test_search_api.py              # API 测试
```

---

### Task 1: 检索服务基础

**Files:**
- Create: `src/openrag/retrieval/retrieval_service.py`
- Create: `src/openrag/retrieval/filters.py`
- Create: `tests/test_retrieval_service.py`

**主要步骤：**
- 创建 RetrievalService 类
- 集成 OpenViking 分层检索
- 实现权限过滤层
- 获取用户可访问的 URI 列表
- 过滤检索结果
- 编写检索服务测试

---

### Task 2: Rerank 重排序

**Files:**
- Create: `src/openrag/retrieval/reranker.py`
- Create: `tests/test_reranker.py`

**主要步骤：**
- 实现基础 Rerank（使用 cross-encoder）
- 利用层级信息优化排序
- 利用位置信息（bbox）优化排序
- 利用 parent_chunk_id 进行上下文扩展
- 编写 Rerank 测试

---

### Task 3: 搜索 API

**Files:**
- Create: `src/openrag/api/search_api.py`
- Create: `tests/test_search_api.py`

**主要步骤：**
- 实现语义搜索 API
- 实现分层搜索 API
- 实现权限检查中间件
- 实现搜索结果格式化
- 编写 API 测试

---

### Task 4: 端到端检索测试

**Files:**
- Create: `tests/test_integration_retrieval.py`

**主要步骤：**
- 准备测试数据（上传文档）
- 测试语义搜索
- 测试分层搜索
- 测试权限过滤
- 测试 Rerank 效果
- 验证检索准确性

---

## 验收标准

完成此计划后，应该具备：

- ✅ OpenViking 分层检索集成正常
- ✅ 权限过滤功能正常
- ✅ Rerank 重排序功能正常
- ✅ 搜索 API 可用
- ✅ 所有测试通过
- ✅ 可以进行文档检索

## 下一步

完成此计划后，继续执行：
- **计划 5：API 和前端**
