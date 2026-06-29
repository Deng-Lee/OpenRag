# 文件夹/文件范围内语义检索 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `/search`（内部 JWT）与 `/service/v1`（外部 service token）检索可按 `paths` 限定到指定文件夹/文件，复用现有 `file_id` 预过滤链路。

**Architecture:** `paths + workspace_id` → 专用 id 查询解析为 `scope_file_ids` → 与 `_accessible_file_ids` 求交集（只收窄）→ 作为既有 Milvus `file_id in [...]` 预过滤传入。空范围在 retrieval 层提前返回；内部 JWT 入口补 workspace read 校验。

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy / Milvus / pytest（in-memory SQLite）。

**Spec:** [docs/superpowers/specs/2026-06-28-folder-file-scoped-retrieval-design.md](../specs/2026-06-28-folder-file-scoped-retrieval-design.md)

**两条贯穿全程的硬不变量（Codex 复审）：**
1. 内部 JWT workspace read 校验只在 JWT 端点、`_execute_search` **之前**；绝不进入外部 service token 路径。
2. `paths=[]` 是**显式空范围**（→ 空结果）；只有 `paths is None` 才允许外部 API 回退 `path_prefix`。

**测试运行约定：** 所有命令在 `openrag/` 目录下执行（该目录有 `pytest.ini`）。形如 `cd openrag && python -m pytest tests/<file>::<test> -v`。

---

## File Structure

- `openrag/src/openrag/services/workspace_file_tree.py` — 新增 `normalize_scope_paths()`、`resolve_scope_file_ids()`（专用 id 查询，不调用 `list_entries_by_prefix`）。
- `openrag/src/openrag/retrieval/retrieval_service.py` — 新增 `_effective_file_ids()`；`search`/`_search_flat`/`_search_contextual` 加 `scope_file_ids` 并透传。
- `openrag/src/openrag/api/search_api.py` — `SearchRequest.paths`；`assert_search_workspace_read()`；`_execute_search` 解析 scope。
- `openrag/src/openrag/api/service_api.py` — 两个 service 请求体加 `paths`；`_resolve_scope_paths()`；退役 `_apply_path_prefix_filter`。
- `openrag/pytest.ini` — 增列 `test_scoped_retrieval.py`。
- 测试：`test_workspace_file_tree.py`（已收集）、`test_scoped_retrieval.py`（新增并增列）、`test_service_api.py`（已收集）。

---

## Task 1: scope 解析 helper（workspace_file_tree.py）

**Files:**
- Modify: `openrag/src/openrag/services/workspace_file_tree.py`
- Test: `openrag/tests/test_workspace_file_tree.py`

- [ ] **Step 1: 写失败测试**（追加到 `test_workspace_file_tree.py` 末尾）

```python
from openrag.services.workspace_file_tree import (
    normalize_scope_paths,
    resolve_scope_file_ids,
    SCOPE_MAX_PATHS,
)


def test_normalize_scope_paths_none_and_empty():
    assert normalize_scope_paths(None) is None
    assert normalize_scope_paths([]) == []


def test_normalize_scope_paths_dedup_and_normalize():
    assert normalize_scope_paths(["docs", "/docs/"]) == ["/docs"]


def test_normalize_scope_paths_rejects_blank():
    with pytest.raises(HTTPException) as ei:
        normalize_scope_paths(["/a", "  "])
    assert ei.value.status_code == status.HTTP_400_BAD_REQUEST


def test_normalize_scope_paths_rejects_traversal():
    with pytest.raises(HTTPException) as ei:
        normalize_scope_paths(["../secret"])
    assert ei.value.status_code == status.HTTP_400_BAD_REQUEST


def test_normalize_scope_paths_rejects_too_many():
    with pytest.raises(HTTPException) as ei:
        normalize_scope_paths([f"/d{i}" for i in range(SCOPE_MAX_PATHS + 1)])
    assert ei.value.status_code == status.HTTP_400_BAD_REQUEST


def test_normalize_scope_paths_rejects_too_long():
    with pytest.raises(HTTPException) as ei:
        normalize_scope_paths(["/" + "a" * 1024])
    assert ei.value.status_code == status.HTTP_400_BAD_REQUEST


def test_resolve_scope_file_ids_folder_recursive(db_session, workspace, owner):
    _dir(db_session, workspace, owner, "/docs", "docs")
    f1 = _file(db_session, workspace, owner, "/docs/a.txt", "a.txt")
    _dir(db_session, workspace, owner, "/docs/sub", "sub")
    f2 = _file(db_session, workspace, owner, "/docs/sub/b.txt", "b.txt")
    _file(db_session, workspace, owner, "/other/c.txt", "c.txt")
    ids = resolve_scope_file_ids(db_session, workspace.id, ["/docs"])
    assert ids == {f1.id, f2.id}


def test_resolve_scope_file_ids_single_file(db_session, workspace, owner):
    f1 = _file(db_session, workspace, owner, "/docs/a.txt", "a.txt")
    assert resolve_scope_file_ids(db_session, workspace.id, ["/docs/a.txt"]) == {f1.id}


def test_resolve_scope_file_ids_root_is_unrestricted(db_session, workspace, owner):
    assert resolve_scope_file_ids(db_session, workspace.id, ["/"]) is None


def test_resolve_scope_file_ids_none_is_unrestricted(db_session, workspace, owner):
    assert resolve_scope_file_ids(db_session, workspace.id, None) is None


def test_resolve_scope_file_ids_empty_list_is_empty_scope(db_session, workspace, owner):
    _file(db_session, workspace, owner, "/docs/a.txt", "a.txt")
    assert resolve_scope_file_ids(db_session, workspace.id, []) == set()


def test_resolve_scope_file_ids_nonexistent_path_is_empty(db_session, workspace, owner):
    assert resolve_scope_file_ids(db_session, workspace.id, ["/nope"]) == set()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd openrag && python -m pytest tests/test_workspace_file_tree.py -k "scope" -v`
Expected: FAIL，`ImportError: cannot import name 'normalize_scope_paths'`。

- [ ] **Step 3: 实现 helper**（在 `workspace_file_tree.py` 顶部 import 区加 `from sqlalchemy import select`；在 `TREE_MAX_NODES` 等常量附近加常量；在 `_normalize_logical_path` 之后加两个函数）

```python
# 顶部 import 区追加
from sqlalchemy import select

# 常量区（与 TREE_MAX_NODES 同处）追加
SCOPE_MAX_PATHS = 50
SCOPE_MAX_PATH_LEN = 1024
```

```python
def normalize_scope_paths(paths):
    """把请求传入的 paths 归一化为去重、有序的逻辑路径列表。

    None -> None（未指定范围）；[] -> []（显式空范围）。
    超 50 个 / 单路径超 1024 字符 / 空白项 / `..` 穿越 -> 400。
    """
    if paths is None:
        return None
    if len(paths) > SCOPE_MAX_PATHS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Too many scope paths (limit {SCOPE_MAX_PATHS})",
        )
    out: List[str] = []
    seen: set[str] = set()
    for raw in paths:
        if raw is None or not str(raw).strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Scope path must not be blank",
            )
        if len(str(raw)) > SCOPE_MAX_PATH_LEN:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Scope path too long (limit {SCOPE_MAX_PATH_LEN})",
            )
        norm = _normalize_logical_path(str(raw))  # `..` -> 400
        if norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def resolve_scope_file_ids(db: Session, workspace_id: int, paths):
    """把 paths 解析为该 workspace 下的文件 id 集合（不含目录行）。

    返回 None 表示"不限范围"（paths 未指定，或含根 `/`）。
    返回 set（可能为空）表示限定范围；空 set 表示空范围 -> 空结果。
    复用与 `_under_prefix` 一致的前缀规则，但只查 File.id、不构树、不继承 TREE_MAX_NODES。
    """
    norm = normalize_scope_paths(paths)
    if norm is None:
        return None
    if "/" in norm:
        return None
    ids: set[int] = set()
    for p in norm:
        rows = db.execute(
            select(File.id).where(
                File.workspace_id == workspace_id,
                File.is_directory.is_(False),
                (File.uri == p) | (File.uri.startswith(p + "/")),
            )
        ).scalars().all()
        ids.update(int(r) for r in rows)
    return ids
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd openrag && python -m pytest tests/test_workspace_file_tree.py -k "scope" -v`
Expected: PASS（全部新用例）。

- [ ] **Step 5: 回归整文件**

Run: `cd openrag && python -m pytest tests/test_workspace_file_tree.py -v`
Expected: PASS（含既有用例）。

- [ ] **Step 6: 提交**

```bash
git add openrag/src/openrag/services/workspace_file_tree.py openrag/tests/test_workspace_file_tree.py
git commit -m "feat(retrieval): add scope path resolution helpers" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: RetrievalService 交集与透传（retrieval_service.py）

**Files:**
- Modify: `openrag/src/openrag/retrieval/retrieval_service.py`
- Modify: `openrag/pytest.ini`
- Test: `openrag/tests/test_scoped_retrieval.py`（新建）

- [ ] **Step 1: 把新测试文件纳入收集**（编辑 `openrag/pytest.ini`，在 `python_files` 列表末尾追加一行）

```ini
    test_scoped_retrieval.py
```

- [ ] **Step 2: 写失败测试**（新建 `openrag/tests/test_scoped_retrieval.py`）

```python
"""RetrievalService scope intersection + empty-scope early-return."""

from unittest.mock import Mock

from openrag.retrieval.retrieval_service import RetrievalService


class _FakeEmbeddingEngine:
    dimension = 3

    def embed_text(self, text):
        return [0.1, 0.2, 0.3]


class _FakeVectorStore:
    def __init__(self):
        self.calls = []

    def search(self, *, query_embedding, top_k, file_ids=None):
        self.calls.append({"top_k": top_k, "file_ids": file_ids})
        return [{"chunk_id": "c1", "file_id": 2, "text": "hit", "score": 0.9}]


def _service(vector_store):
    svc = RetrievalService(
        db=Mock(),
        embedding_engine=_FakeEmbeddingEngine(),
        vector_store=vector_store,
        layer_store=None,
    )
    svc._enrich_hits = lambda hits: None
    return svc


def test_effective_file_ids_intersects_and_handles_none():
    svc = _service(_FakeVectorStore())
    svc._accessible_file_ids = lambda user_id, workspace_id=None: [1, 2, 3]
    assert sorted(svc._effective_file_ids(1, 7, {2, 3, 9})) == [2, 3]
    assert svc._effective_file_ids(1, 7, None) == [1, 2, 3]
    assert svc._effective_file_ids(1, 7, set()) == []
    svc._accessible_file_ids = lambda user_id, workspace_id=None: None
    assert sorted(svc._effective_file_ids(1, None, {5, 6})) == [5, 6]
    assert svc._effective_file_ids(1, None, None) is None


def test_search_passes_scope_intersection_to_vector_store():
    vs = _FakeVectorStore()
    svc = _service(vs)
    svc._accessible_file_ids = lambda user_id, workspace_id=None: [1, 2, 3]
    svc.search("q", user_id=1, workspace_id=7, top_k=5, scope_file_ids={2, 3, 99})
    assert sorted(vs.calls[0]["file_ids"]) == [2, 3]


def test_empty_scope_returns_empty_without_calling_store():
    vs = _FakeVectorStore()
    svc = _service(vs)
    svc._accessible_file_ids = lambda user_id, workspace_id=None: [1, 2, 3]
    results = svc.search("q", user_id=1, workspace_id=7, top_k=5, scope_file_ids=set())
    assert results == []
    assert vs.calls == []


def test_no_scope_keeps_existing_behavior():
    vs = _FakeVectorStore()
    svc = _service(vs)
    svc._accessible_file_ids = lambda user_id, workspace_id=None: None
    svc.search("q", user_id=1, top_k=5)
    assert vs.calls[0]["file_ids"] is None


class _FakeLayerStore:
    def __init__(self):
        self.calls = []

    def search_layers(self, query_embedding, layer, top_k, file_ids=None):
        self.calls.append({"layer": layer, "file_ids": file_ids})
        return [{"file_id": 2, "score": 0.8, "layer_row_id": "r", "text": "t"}]


class _EmptyL0LayerStore:
    def search_layers(self, query_embedding, layer, top_k, file_ids=None):
        return []


class _FakeFulltext:
    def __init__(self):
        self.calls = []

    def search_chunk_scores(self, *, index_names, query_text, file_ids, chunk_ids):
        self.calls.append({"file_ids": file_ids})
        return {}


def test_contextual_scope_limits_l0_l1_l2():
    vs = _FakeVectorStore()
    ls = _FakeLayerStore()
    svc = RetrievalService(
        db=Mock(), embedding_engine=_FakeEmbeddingEngine(),
        vector_store=vs, layer_store=ls,
    )
    svc._accessible_file_ids = lambda user_id, workspace_id=None: [1, 2, 3]
    svc._enrich_hits = lambda hits: None
    svc.search(
        "q", user_id=1, workspace_id=7, top_k=5,
        use_contextual=True, retrieval_strategy="deep", scope_file_ids={2},
    )
    l0 = next(c for c in ls.calls if c["layer"] == "l0")
    assert l0["file_ids"] == [2]                 # L0 收到 effective ids
    l1 = next(c for c in ls.calls if c["layer"] == "l1")
    assert set(l1["file_ids"]) <= {2}            # L1 候选在范围内
    assert set(vs.calls[0]["file_ids"]) <= {2}   # L2 chunk 搜索在范围内


def test_contextual_fallback_to_flat_carries_scope():
    vs = _FakeVectorStore()
    svc = RetrievalService(
        db=Mock(), embedding_engine=_FakeEmbeddingEngine(),
        vector_store=vs, layer_store=_EmptyL0LayerStore(),
    )
    svc._accessible_file_ids = lambda user_id, workspace_id=None: [1, 2, 3]
    svc._enrich_hits = lambda hits: None
    svc.search(
        "q", user_id=1, workspace_id=7, top_k=5,
        use_contextual=True, retrieval_strategy="deep", scope_file_ids={2},
    )
    assert set(vs.calls[0]["file_ids"]) <= {2}   # 无 L0 命中回退仍带 scope


def test_es_blend_filter_ids_within_scope():
    vs = _FakeVectorStore()
    ft = _FakeFulltext()
    svc = RetrievalService(
        db=Mock(), embedding_engine=_FakeEmbeddingEngine(),
        vector_store=vs, layer_store=None, fulltext_store=ft,
    )
    svc._accessible_file_ids = lambda user_id, workspace_id=None: [1, 2, 3]
    svc._enrich_hits = lambda hits: None
    svc._resolve_es_index_names = lambda workspace_id, file_ids: ["idx"]
    svc.search(
        "q", user_id=1, workspace_id=7, top_k=5,
        scope_file_ids={2, 3}, vector_similarity_weight=0.7,
    )
    assert ft.calls and set(ft.calls[0]["file_ids"]) <= {2, 3}
```

- [ ] **Step 3: 跑测试确认失败**

Run: `cd openrag && python -m pytest tests/test_scoped_retrieval.py -v`
Expected: FAIL，`AttributeError: 'RetrievalService' object has no attribute '_effective_file_ids'`。

- [ ] **Step 4: 新增 `_effective_file_ids` 方法**（加在 `_accessible_file_ids` 方法之后）

```python
    def _effective_file_ids(self, user_id, workspace_id, scope_file_ids=None):
        """accessible ∩ scope。scope 非 None 时恒返回确定性 list（含空 []），
        以命中下游 `len==0 -> return []` 守卫；绝不返回 None。"""
        accessible = self._accessible_file_ids(user_id, workspace_id)
        if scope_file_ids is None:
            return accessible
        if accessible is None:
            return list(scope_file_ids)
        scope = set(scope_file_ids)
        return [fid for fid in accessible if fid in scope]
```

- [ ] **Step 5: 给 `search` 加参数并透传**

`search()` 签名末尾（`vector_similarity_weight: float = 1.0,` 之后）追加：
```python
        scope_file_ids: Optional[set[int]] = None,
```
`search()` 体内三个分发调用各加 `scope_file_ids=scope_file_ids,`：
- 第一个 `self._search_flat(query, user_id, workspace_id, flat_top_k, vector_similarity_weight=vector_similarity_weight)` →
```python
            self._search_flat(
                query,
                user_id,
                workspace_id,
                flat_top_k,
                vector_similarity_weight=vector_similarity_weight,
                scope_file_ids=scope_file_ids,
            )
```
- `precise/flat` 分支的第二个 `self._search_flat(...)` 同样追加 `scope_file_ids=scope_file_ids,`。
- `self._search_contextual(...)` 调用追加 `scope_file_ids=scope_file_ids,`。

- [ ] **Step 6: `_search_flat` 用 effective**

签名（`*, vector_similarity_weight: float = 1.0,` 之后）追加：
```python
        scope_file_ids: Optional[set[int]] = None,
```
体内替换：
```python
        # before
        accessible_file_ids = self._accessible_file_ids(user_id, workspace_id)
        # after
        accessible_file_ids = self._effective_file_ids(user_id, workspace_id, scope_file_ids)
```
（紧随的 `if accessible_file_ids is not None and len(accessible_file_ids) == 0: return []` 保持不变。）

- [ ] **Step 7: `_search_contextual` 用 effective + fallback 透传**

签名末尾追加：
```python
        scope_file_ids: Optional[set[int]] = None,
```
体内替换：
```python
        # before
        accessible = self._accessible_file_ids(user_id, workspace_id)
        # after
        accessible = self._effective_file_ids(user_id, workspace_id, scope_file_ids)
```
无 L0 命中回退到 flat 的调用追加 `scope_file_ids=scope_file_ids,`：
```python
            return self._search_flat(
                query,
                user_id,
                workspace_id,
                top_k,
                vector_similarity_weight=vector_similarity_weight,
                scope_file_ids=scope_file_ids,
            )
```

- [ ] **Step 8: 跑测试确认通过**

Run: `cd openrag && python -m pytest tests/test_scoped_retrieval.py tests/test_l0_l1_retrieval_flag.py -v`
Expected: PASS（新用例 + 既有 L0/L1 用例不回归）。

- [ ] **Step 9: 提交**

```bash
git add openrag/src/openrag/retrieval/retrieval_service.py openrag/tests/test_scoped_retrieval.py openrag/pytest.ini
git commit -m "feat(retrieval): intersect scope with accessible file ids" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: 内部 /search API（search_api.py）

**Files:**
- Modify: `openrag/src/openrag/api/search_api.py`
- Test: `openrag/tests/test_scoped_retrieval.py`

- [ ] **Step 1: 写失败测试**（追加到 `test_scoped_retrieval.py`）

```python
import pytest
from fastapi import HTTPException

import openrag.api.search_api as sapi


def test_assert_search_workspace_read_blocks_without_permission(monkeypatch):
    monkeypatch.setattr(
        sapi.WorkspaceService, "check_user_permission",
        lambda self, ws, uid, perm: False,
    )
    with pytest.raises(HTTPException) as ei:
        sapi.assert_search_workspace_read(Mock(), user_id=1, workspace_id=7)
    assert ei.value.status_code == 403


def test_assert_search_workspace_read_allows_with_permission(monkeypatch):
    monkeypatch.setattr(
        sapi.WorkspaceService, "check_user_permission",
        lambda self, ws, uid, perm: True,
    )
    sapi.assert_search_workspace_read(Mock(), user_id=1, workspace_id=7)


def test_assert_search_workspace_read_skips_when_no_workspace():
    sapi.assert_search_workspace_read(Mock(), user_id=1, workspace_id=None)


import asyncio


def test_semantic_search_blocks_before_execute(monkeypatch):
    """无 read 权限的 workspace 必须在进入 _execute_search（含 resolve/embedding）前 403。"""
    monkeypatch.setattr(
        sapi.WorkspaceService, "check_user_permission",
        lambda self, ws, uid, perm: False,
    )
    called = {"exec": False}
    monkeypatch.setattr(
        sapi, "_execute_search",
        lambda *a, **k: called.__setitem__("exec", True),
    )
    req = sapi.SearchRequest(query="q", workspace_id=7, paths=["/docs"])
    with pytest.raises(HTTPException) as ei:
        asyncio.run(sapi.semantic_search(req, user_id=1, db=Mock()))
    assert ei.value.status_code == 403
    assert called["exec"] is False               # 未进入检索，不解析 paths、不泄露路径存在性
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd openrag && python -m pytest tests/test_scoped_retrieval.py -k assert_search -v`
Expected: FAIL，`AttributeError: module 'openrag.api.search_api' has no attribute 'assert_search_workspace_read'`。

- [ ] **Step 3: 实现**（`search_api.py`）

import 区追加：
```python
from openrag.services.workspace_file_tree import resolve_scope_file_ids
```
（`WorkspaceService` 已在文件内 import；若提示未导入，加 `from openrag.services.workspace_service import WorkspaceService`。）

`SearchRequest` 加字段（放在 `vector_similarity_weight` 之后）：
```python
    paths: Optional[list[str]] = Field(
        None,
        description="限定检索范围到这些逻辑路径（文件夹递归 / 单文件）；需配合 workspace_id。空数组=空范围",
    )
```

新增模块函数（放在 `_execute_search` 之前）：
```python
def assert_search_workspace_read(db: Session, user_id: int, workspace_id: Optional[int]) -> None:
    """内部 JWT 搜索入口的 workspace read 前置校验。workspace_id=None 维持旧语义；
    admin 由 check_user_permission 放行。仅供 JWT 端点使用，不用于 service token 路径。"""
    if workspace_id is None:
        return
    if not WorkspaceService(db).check_user_permission(workspace_id, user_id, "read"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Read permission required",
        )
```

`_execute_search` 开头（`start = time.time()` 之前）追加：
```python
    if request.paths is not None and request.workspace_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="workspace_id is required when paths is set",
        )
    scope_file_ids = resolve_scope_file_ids(db, request.workspace_id, request.paths)
```
`_execute_search` 内两个 `svc.search(...)` 调用各加 `scope_file_ids=scope_file_ids,`（contextual 分支与非 contextual 分支各一处）。

`semantic_search` 与 `hierarchical_search` 两个端点在 `try:` 块**之前**各加一行（确保 403 直接抛出，且先于 `_execute_search` 内的 resolve / embedding / 向量检索）：
```python
    assert_search_workspace_read(db, user_id, request.workspace_id)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd openrag && python -m pytest tests/test_scoped_retrieval.py -v`
Expected: PASS（含权限 helper 用例 + 之前的 retrieval 用例）。

- [ ] **Step 5: 提交**

```bash
git add openrag/src/openrag/api/search_api.py openrag/tests/test_scoped_retrieval.py
git commit -m "feat(api): scope /search by paths and enforce workspace read" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: 外部 /service/v1 API（service_api.py）

**Files:**
- Modify: `openrag/src/openrag/api/service_api.py`
- Test: `openrag/tests/test_service_api.py`

- [ ] **Step 1: 写失败测试**（追加到 `test_service_api.py` 末尾）

```python
from openrag.api.service_api import _resolve_scope_paths


def test_resolve_scope_paths_prefers_paths_over_prefix():
    assert _resolve_scope_paths(["/a"], "/b") == ["/a"]
    assert _resolve_scope_paths([], "/b") == []          # 显式空范围覆盖 path_prefix
    assert _resolve_scope_paths(None, "/b") == ["/b"]    # 仅 paths 缺省时回退
    assert _resolve_scope_paths(None, "/") is None
    assert _resolve_scope_paths(None, "  ") is None
    assert _resolve_scope_paths(None, None) is None


def test_service_search_forwards_paths_and_skips_post_filter(
    db, owner, workspace, service_token_headers
):
    captured = {}

    def _fake_execute(db_, user_id, request, *, endpoint, rerank_hierarchical_boost):
        captured["paths"] = request.paths
        return SearchResponse(results=[], total=0, query_time_ms=1.0)

    app.dependency_overrides[get_db] = lambda: db
    try:
        with patch("openrag.api.service_api._execute_search", _fake_execute):
            client = TestClient(app)
            resp = client.post(
                f"/service/v1/workspaces/{workspace.name}/search",
                headers=service_token_headers,
                json={"query": "q", "paths": ["/docs"], "path_prefix": "/ignored"},
            )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    assert captured["paths"] == ["/docs"]   # paths 覆盖 path_prefix
```

- [ ] **Step 1b: 更新既有 `path_prefix` 用例（改为断言"转发 + 不再后过滤裁剪"）**

这两个用例 mock 了 `_execute_search`，删除后过滤后 mock 返回值会原样透出，旧的"裁剪"断言必然失败。按下述整体替换断言。

`test_service_search_path_prefix_filter` 的结尾断言（`assert r.status_code == 200` 起至函数末）替换为：
```python
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 2                        # 不再后过滤裁剪
    assert [h["uri"] for h in data["results"]] == ["/docs/a.txt", "/other/b.txt"]
    mock_search.assert_called_once()
    assert mock_search.call_args[0][2].workspace_id == workspace.id
    assert mock_search.call_args[0][2].paths == ["/docs"]   # path_prefix -> paths
```

`test_service_multi_workspace_search_applies_path_prefix_per_workspace` 的结尾断言替换为：
```python
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 4                         # 不再后过滤裁剪，4 条全返回
    assert data["l1_llm_applied"] is True
    assert data["l1_llm_skip_reason"] == "mixed"
    assert [h["uri"] for h in data["results"]] == [
        "/docs/a.txt", "/other/x.txt", "/docs/b.txt", "/other/y.txt",
    ]
    for call in mock_search.call_args_list:
        assert call[0][2].paths == ["/docs"]          # 每个 workspace 都转发 paths
```

（真实的"预过滤召回"由 retrieval 层用例覆盖——Task 2 的 `test_contextual_scope_limits_l0_l1_l2` / `test_es_blend_filter_ids_within_scope`；service 层这里只验证 `paths` 转发与不再裁剪。）

- [ ] **Step 2: 跑测试确认失败**

Run: `cd openrag && python -m pytest tests/test_service_api.py -k "scope_paths or forwards_paths" -v`
Expected: FAIL，`ImportError: cannot import name '_resolve_scope_paths'`。

- [ ] **Step 3: 实现**（`service_api.py`）

`ServiceSearchRequest` 与 `ServiceMultiWorkspaceSearchRequest` 各加字段（放在 `path_prefix` 之后）：
```python
    paths: Optional[List[str]] = Field(
        None,
        description="限定检索范围到这些逻辑路径；显式传入时覆盖 path_prefix（含 []=空范围）",
    )
```

新增 helper（放在 `_apply_path_prefix_filter` 原位置；同时删除 `_apply_path_prefix_filter` 函数本体）：
```python
def _resolve_scope_paths(paths, path_prefix):
    """paths 优先并覆盖 path_prefix（含 paths=[]）；仅 paths 为 None 时回退 path_prefix。"""
    if paths is not None:
        return paths
    if path_prefix and str(path_prefix).strip() and str(path_prefix).strip() != "/":
        return [path_prefix]
    return None
```

`service_semantic_search`：构造 `SearchRequest(...)` 时加 `paths=_resolve_scope_paths(body.paths, body.path_prefix),`；把 `return _apply_path_prefix_filter(resp, body.path_prefix)` 改为 `return resp`。

`service_multi_workspace_semantic_search`：构造每个 `SearchRequest(...)` 时加 `paths=_resolve_scope_paths(body.paths, body.path_prefix),`；删除 `resp = _apply_path_prefix_filter(resp, body.path_prefix)` 这一行（保留其后的 `workspace_count += 1` 等逻辑）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd openrag && python -m pytest tests/test_service_api.py -v`
Expected: PASS（新用例 + 已更新的两条 `path_prefix` 用例 + 其余 service API 用例）。

- [ ] **Step 5: 提交**

```bash
git add openrag/src/openrag/api/service_api.py openrag/tests/test_service_api.py
git commit -m "feat(service-api): scope search by paths, pre-filter over post-filter" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: 文档与全量回归

**Files:**
- Modify: `docs/04-外部系统接入与API.md`（外部 API 说明）

- [ ] **Step 1: 更新外部 API 文档**

在 `docs/04-外部系统接入与API.md` 的 `/service/v1/.../search` 小节补充：
- 新增请求字段 `paths: string[]`（逻辑路径列表，文件夹递归 / 单文件）。
- `paths` 优先并覆盖旧字段 `path_prefix`；`paths=[]` 表示空范围（返回空结果）；仅 `paths` 缺省时回退 `path_prefix`。
- ⚠️ 行为变化：`path_prefix` 由「检索后过滤」改为「预过滤」，结果更准、召回更多，与旧版不完全一致。

- [ ] **Step 2: 全量回归（默认收集集）**

Run: `cd openrag && python -m pytest -v`
Expected: PASS（含新增 `test_scoped_retrieval.py`、`test_workspace_file_tree.py`、`test_service_api.py`）。

- [ ] **Step 3: 提交**

```bash
git add docs/04-外部系统接入与API.md
git commit -m "docs(api): document paths scope and pre-filter change" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Codex 代码复审补充修复（空 scope 真提前返回 + 大 scope contextual L0 预过滤）

**Files:**
- Modify: `openrag/src/openrag/api/search_api.py`
- Modify: `openrag/src/openrag/retrieval/retrieval_service.py`
- Test: `openrag/tests/test_scoped_retrieval.py`

**目标：**
1. `paths=[]` / 不存在路径 / 空目录解析成空 scope 后，要在初始化 embedding、Milvus、layer/fulltext store 前直接返回空结果。
2. scope 与 accessible file ids 交集为空时，`_search_flat` / `_search_contextual` 要在 `_embed_query()` 前返回空结果。
3. contextual L0 即使遇到大 scope（>512 file ids），也要把 effective file ids 传给 `layer_store.search_layers()`，让 layer store 使用既有大列表兜底逻辑，而不是先全 workspace 取 L0 top 再过滤。

- [ ] **Step 1: 写失败测试**（追加到 `test_scoped_retrieval.py`）

```python
class _FailingEmbeddingEngine:
    dimension = 3

    def embed_text(self, text):
        raise AssertionError("embedding should not run for empty scope")


class _FailingVectorStore:
    def search(self, *args, **kwargs):
        raise AssertionError("vector store should not run for empty scope")


class _FailingLayerStore:
    def search_layers(self, *args, **kwargs):
        raise AssertionError("layer store should not run for empty scope")


def test_execute_search_empty_scope_returns_before_external_dependencies(monkeypatch):
    monkeypatch.setattr(sapi, "resolve_scope_file_ids", lambda *a, **k: set())

    def _fail(*args, **kwargs):
        raise AssertionError("external dependency should not be initialized for empty scope")

    monkeypatch.setattr(sapi, "_prepare_retrieval_trace", _fail)
    monkeypatch.setattr(sapi, "_get_embedding_engine", _fail)
    monkeypatch.setattr(sapi, "_get_vector_store", _fail)
    monkeypatch.setattr(sapi, "_get_layer_store", _fail)
    monkeypatch.setattr(sapi, "_get_fulltext_store", _fail)

    resp = sapi._execute_search(
        Mock(),
        1,
        sapi.SearchRequest(query="q", workspace_id=7, paths=[]),
        endpoint="semantic",
        rerank_hierarchical_boost=None,
    )

    assert resp.results == []
    assert resp.total == 0


@pytest.mark.parametrize("use_contextual", [False, True])
def test_empty_scope_intersection_returns_before_embedding_and_store(use_contextual):
    svc = RetrievalService(
        db=Mock(),
        embedding_engine=_FailingEmbeddingEngine(),
        vector_store=_FailingVectorStore(),
        layer_store=_FailingLayerStore() if use_contextual else None,
    )
    svc._accessible_file_ids = lambda user_id, workspace_id=None: [1, 2, 3]

    results = svc.search(
        "q",
        user_id=1,
        workspace_id=7,
        top_k=5,
        use_contextual=use_contextual,
        retrieval_strategy="deep",
        scope_file_ids={99},
    )

    assert results == []


class _LargeScopeLayerStore:
    def __init__(self):
        self.calls = []

    def search_layers(self, query_embedding, layer, top_k, file_ids=None):
        self.calls.append({"layer": layer, "file_ids": file_ids})
        fid = int(file_ids[0]) if file_ids else -1
        return [{"file_id": fid, "score": 0.8, "layer_row_id": "r", "text": "t"}]


def test_contextual_large_scope_passes_file_ids_to_l0_layer_store():
    large_scope = set(range(1000, 1600))  # >512
    vs = _FakeVectorStore()
    ls = _LargeScopeLayerStore()
    svc = RetrievalService(
        db=Mock(),
        embedding_engine=_FakeEmbeddingEngine(),
        vector_store=vs,
        layer_store=ls,
    )
    svc._accessible_file_ids = lambda user_id, workspace_id=None: list(range(900, 1700))
    svc._enrich_hits = lambda hits: None

    svc.search(
        "q",
        user_id=1,
        workspace_id=7,
        top_k=5,
        use_contextual=True,
        retrieval_strategy="deep",
        scope_file_ids=large_scope,
    )

    l0 = next(c for c in ls.calls if c["layer"] == "l0")
    assert l0["file_ids"] is not None
    assert set(l0["file_ids"]) == large_scope
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd openrag && python -m pytest tests/test_scoped_retrieval.py -k "empty_scope or large_scope" -v`

Expected:
- `test_execute_search_empty_scope_returns_before_external_dependencies` 失败，暴露 `_execute_search` 仍初始化外部依赖。
- `test_empty_scope_intersection_returns_before_embedding_and_store` 失败，暴露 `_search_flat` / `_search_contextual` 仍先 embedding。
- `test_contextual_large_scope_passes_file_ids_to_l0_layer_store` 失败，暴露 L0 大 scope 传入 `file_ids=None`。

- [ ] **Step 3: 修复 `_execute_search` 空 scope 真提前返回**

在 `_execute_search()` 中，`resolve_scope_file_ids(...)` 之后、`_prepare_retrieval_trace(...)` 和所有 `_get_*store()` 之前增加：

```python
    if scope_file_ids is not None and len(scope_file_ids) == 0:
        elapsed_ms = (time.time() - start) * 1000
        return SearchResponse(
            results=[],
            total=0,
            query_time_ms=elapsed_ms,
            l1_llm_applied=None,
            l1_llm_skip_reason=None,
        )
```

注意：这只处理“请求 scope 本身为空”的情况；无权限 workspace 的 JWT 403 仍然由端点前置 `assert_search_workspace_read(...)` 先处理。

- [ ] **Step 4: 修复 RetrievalService 空交集在 embedding 前返回**

`_search_flat()` 中把 effective file ids 计算和空列表 guard 移到 `_embed_query(query)` 之前：

```python
        accessible_file_ids = self._effective_file_ids(user_id, workspace_id, scope_file_ids)
        if accessible_file_ids is not None and len(accessible_file_ids) == 0:
            return []
        query_vec = self._embed_query(query)
```

`_search_contextual()` 同样把 effective file ids 计算和空列表 guard 移到 `_embed_query(query)` 之前：

```python
        accessible = self._effective_file_ids(user_id, workspace_id, scope_file_ids)
        if accessible is not None and len(accessible) == 0:
            return []
        query_vec = self._embed_query(query)
```

- [ ] **Step 5: 修复 contextual 大 scope L0 预过滤**

在 `_search_contextual()` 中，L0 调用不要因为 `len(accessible) > 512` 就传 `None`。改为始终把 effective file ids 交给 layer store；layer store 已有大列表兜底逻辑。

```python
        use_expr_filter = accessible is not None and len(accessible) <= 512
        l0_file_filter = accessible if accessible is not None else None

        l0_hits = self.layer_store.search_layers(
            query_vec, "l0", top_k=l0_cap, file_ids=l0_file_filter
        )
        if accessible is not None and not use_expr_filter:
            acc_set = set(accessible)
            l0_hits = [h for h in l0_hits if h.get("file_id") in acc_set]
```

说明：这里保留大列表后的内存二次过滤作为防御；真实 `MilvusLayerStore.search_layers()` 收到大列表后会扩大召回并做 post-filter，避免 retrieval 层先全 workspace top-N 再过滤导致范围内候选被挤掉。

- [ ] **Step 6: 跑测试确认通过**

Run: `cd openrag && python -m pytest tests/test_scoped_retrieval.py -v`

Expected: PASS。

Run: `cd openrag && python -m pytest tests/test_scoped_retrieval.py tests/test_workspace_file_tree.py tests/test_service_api.py -v`

Expected: PASS。

- [ ] **Step 7: 提交**

```bash
git add openrag/src/openrag/api/search_api.py openrag/src/openrag/retrieval/retrieval_service.py openrag/tests/test_scoped_retrieval.py
git commit -m "fix(retrieval): return empty scopes before external search dependencies" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review（计划对照 spec）

- **§4.0 鉴权**：Task 3（`assert_search_workspace_read` + JWT 端点前置 + 403）。✓
- **§4.1a 约束/归一化**：Task 1（`normalize_scope_paths`：50/1024/空白→400、去重保序、`..`→400）。✓
- **§4.1b 专用 id 查询（不用 list_entries_by_prefix，不继承 TREE_MAX_NODES，`/`→None）**：Task 1（`resolve_scope_file_ids`）。✓
- **§4.1c 交集 + 空范围返回 list 非 None**：Task 2（`_effective_file_ids`）。✓
- **§4.1d 空范围提前返回、不下传 store**：Task 2（`test_empty_scope_returns_empty_without_calling_store`）。✓
- **§4.1e/f scope 贯穿 contextual + 全分支透传**：Task 2（`_search_contextual` 源头替换 + fallback 透传 + 三处分发）。✓
- **§4.2 内部 paths + workspace_id 校验 + _execute_search**：Task 3。✓
- **§4.3 外部 paths 覆盖 path_prefix + 退役后过滤**：Task 4（`_resolve_scope_paths`、删除 `_apply_path_prefix_filter`）。✓
- **§7 测试收集口径**：Task 2（`pytest.ini` 增列 `test_scoped_retrieval.py`）+ 其余放入已收集文件。✓
- **§8 影响文件**：Task 1–5 全覆盖（含 `pytest.ini`、外部文档）。✓

**两条硬不变量**：JWT 校验只在端点、不入 `_execute_search`（Task 3）；`paths=[]`≠不限范围（Task 1 `resolve` + Task 4 `_resolve_scope_paths` 的 `paths is not None` 分支与对应用例）。✓

---

## Codex 复审结论（来自 Codex）

整体判断：主体方案已经收敛，没有发现架构方向偏离。专用 id 查询、空 scope 提前返回、`paths` 覆盖 `path_prefix`、JWT read 前置校验这些关键决策都对齐 spec，可以继续推进。推进前建议补齐以下执行细节，避免实现阶段出现回归测试失败或关键路径漏测。

1. **P1：Task 4 与现有 `path_prefix` 测试存在冲突，需要同步更新旧用例。**

   Task 4 计划删除 `_apply_path_prefix_filter` 并把 `path_prefix` 升级为预过滤，这是正确方向；但现有 `test_service_api.py` 中仍有单 workspace 与 multi workspace 用例断言“检索后过滤”的返回结果。按当前计划直接实现后，这些旧断言会因为不再裁剪 mock 返回结果而失败。建议在 Task 4 中明确同步调整旧用例：不再断言 response 被后过滤裁剪，而是断言构造给 `_execute_search` 的内部 `SearchRequest.paths == [path_prefix]`；同时保留/新增一个预过滤召回样例，证明目标目录命中不会再被全 workspace top_k 挤掉。

2. **P2：contextual scope 的实现步骤已覆盖，但测试还不够证明 L0/L1/L2/ES 全链路受限。**

   文档已要求 scope 贯穿 `_search_contextual` 的源头、fallback 以及 ES 融合，方向正确；但当前计划中的 `test_scoped_retrieval.py` 主要覆盖 flat 路径、交集和空 scope，不足以证明 contextual 下范围外文件不会进入 L0/L1/L2/ES 候选。建议补最小 fake `layer_store` / `vector_store` 测试：断言 L0 收到 effective file ids，L1/L2 只基于范围内候选文件；再补一个“无 L0 命中 fallback 到 flat 时仍携带 scope”的用例。若 `vector_similarity_weight < 1`，还应确认 `_blend_elasticsearch_scores()` 的 `filter_file_ids` 仍来自范围内候选。

3. **P2：内部 `/search` 权限测试目前只覆盖 helper，不足以证明端点调用顺序。**

   `assert_search_workspace_read` 的单元测试是必要的，但安全边界还需要验证端点行为：无 read 权限的 workspace 应在进入 `_execute_search`、`resolve_scope_file_ids`、embedding 或向量检索前返回 403。建议补一个端点级或函数级测试，通过 patch `_execute_search` / `resolve_scope_file_ids` 断言无权限时它们未被调用；同时覆盖“无权限 workspace + paths”先 403，而不是返回空结果或暴露路径存在性。

结论：以上三点处理后，计划主体无明显冲突，可以进入实现阶段。不建议继续扩大范围到新的抽象或额外权限模型；本轮保持“路径 scope 预过滤 + 既有权限链路补强”的主线即可。

---

## 计划调整记录（应对 Codex 计划复审，2026-06-28）

三点均已核实并落入对应任务（未扩大范围）：

- **P1（旧 `path_prefix` 用例冲突）→ Task 4 Step 1b（新增）。** 已核实 `test_service_search_path_prefix_filter`、`test_service_multi_workspace_search_applies_path_prefix_per_workspace` 均 mock `_execute_search` 并断言后过滤裁剪。新增步骤把它们改为断言 `SearchRequest.paths` 转发 + 结果不再裁剪；Step 4 Expected 同步为"含已更新用例"。
- **P2（contextual 全链路受限证明不足）→ Task 2 Step 2（扩充）。** 新增 `test_contextual_scope_limits_l0_l1_l2`（断言 L0 收到 effective ids、L1/L2 候选在范围内）、`test_contextual_fallback_to_flat_carries_scope`（无 L0 命中回退仍带 scope）、`test_es_blend_filter_ids_within_scope`（`vector_similarity_weight=0.7` 时 ES `filter_file_ids` 在范围内）。
- **P3（端点调用顺序未验证）→ Task 3 Step 1（扩充）+ Step 3（明确位置）。** 新增 `test_semantic_search_blocks_before_execute`：无权限时 403 且 `_execute_search` 未被调用；并把 `assert_search_workspace_read` 明确放在端点 `try:` 之前（先于 resolve/embedding/向量检索）。

---

## 代码复审补充记录（来自 Codex，2026-06-29）

- **P1（空 scope 未真正提前返回）→ Task 6 Step 1-4（新增）。** 已把 `paths=[]` / 不存在路径 / 空目录解析成空 scope 后的返回时机明确为：`_execute_search` 在初始化 trace、embedding、Milvus、layer/fulltext store 前直接返回空 `SearchResponse`；`_search_flat` / `_search_contextual` 在 scope 与 accessible 交集为空时，也必须在 `_embed_query()` 前返回 `[]`。新增测试覆盖 `_execute_search` 不初始化外部依赖，以及 flat/contextual 空交集不调用 embedding/store。
- **P2（contextual 大 scope L0 仍先全 workspace 搜）→ Task 6 Step 1、Step 5（新增）。** 已明确当 effective file ids 超过 512 时，L0 仍要把 `file_ids` 传给 `layer_store.search_layers()`，由 layer store 的大列表 post-filter/oversampling 逻辑兜底；retrieval 层可保留二次内存过滤作为防御。新增测试用 >512 的 scope 断言 L0 收到完整 effective file ids，而不是 `None`。

**实现确认（2026-06-29）：** 两点均已落地并验证（先复核 diff 确认问题真实存在）。
- P1：`search_api._execute_search` 在 `resolve_scope_file_ids` 之后、`_prepare_retrieval_trace`/`_get_*store()` 之前对空 scope 直接返回空 `SearchResponse`；`retrieval_service._search_flat` / `_search_contextual` 把 `_effective_file_ids` 计算与空交集 guard 移到 `_embed_query()` 之前。
- P2：`_search_contextual` 的 `l0_file_filter = accessible if accessible is not None else None`（不再因 >512 置 `None`），保留 `not use_expr_filter` 时的内存二次过滤作为防御；已核实 `milvus_layer_store.search_layers()` 对 >512 列表用 `limit = min(max(top_k*50,500),4096)` 过采样 + 内部 post-filter。
- 测试：`tests/test_scoped_retrieval.py` 新增 3 个用例（`test_execute_search_empty_scope_returns_before_external_dependencies`、参数化的 `test_empty_scope_intersection_returns_before_embedding_and_store`、`test_contextual_large_scope_passes_file_ids_to_l0_layer_store`）；默认收集集 172 passed（唯一 1 failed 为既有 `test_upload_file_large_file`，与本次无关）。
