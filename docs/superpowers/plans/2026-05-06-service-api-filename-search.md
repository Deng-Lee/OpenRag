# Service API 文件名模糊查询端点 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `/service/v1` 下新增 `GET .../documents/search-by-name` 端点，支持通过文件名子串（大小写不敏感）模糊搜索文件（排除目录），支持 path_prefix 限定范围和分页。

**Architecture:** 新增服务函数 `search_documents_by_name()` 在 `workspace_file_tree.py`（与现有文件查询服务同层），新增路由函数在 `service_api.py`，就地复制 `_escape_ilike` 避免跨模块依赖，响应格式 `{items, total, skip, limit}` 与 entries/by-prefix 风格一致。

**Tech Stack:** FastAPI, SQLAlchemy (ilike), Pydantic Field constraints

---

### Task 1: 新增服务函数 `search_documents_by_name`

**Files:**
- Modify: `openrag/src/openrag/services/workspace_file_tree.py` (追加函数)

- [ ] **Step 1: 添加 `_escape_ilike` 辅助函数**

在 `workspace_file_tree.py` 文件顶部（常量定义之后，`_normalize_logical_path` 之前）添加：

```python
def _escape_ilike(value: str) -> str:
    """Escape % and _ wildcards for PostgreSQL ilike patterns."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
```

- [ ] **Step 2: 添加 `search_documents_by_name` 函数**

在 `workspace_file_tree.py` 末尾（`build_nested_tree` 函数之后）添加：

```python
def search_documents_by_name(
    db: Session,
    workspace_id: int,
    filename_pattern: str,
    path_prefix: str = "/",
    skip: int = 0,
    limit: int = 50,
) -> tuple[list[File], int]:
    """
    Search files (not directories) by name substring (case-insensitive).

    Returns (matching_files, total_count). Results sorted by name ascending.
    """
    escaped = _escape_ilike(filename_pattern.strip())
    q = db.query(File).filter(
        File.workspace_id == workspace_id,
        File.is_directory.is_(False),
        File.name.ilike(f"%{escaped}%"),
    )

    # Optional path_prefix filter
    prefix = _normalize_logical_path(path_prefix).rstrip("/") or "/"
    if prefix != "/":
        q = q.filter(
            (File.uri == prefix) | (File.uri.startswith(prefix + "/"))
        )

    total = q.count()
    rows = q.order_by(File.name.asc()).offset(skip).limit(limit).all()
    return rows, total
```

- [ ] **Step 3: Commit**

```bash
git add openrag/src/openrag/services/workspace_file_tree.py
git commit -m "feat: add search_documents_by_name service function"
```

---

### Task 2: 新增 service API 端点

**Files:**
- Modify: `openrag/src/openrag/api/service_api.py` (追加路由 + 辅助函数 + import)

- [ ] **Step 1: 添加 import**

在 `service_api.py` 的 import 区块，追加 `search_documents_by_name` 到从 `workspace_file_tree` 的导入行：

```python
from openrag.services.workspace_file_tree import (
    build_nested_tree,
    get_file_document_by_path,
    list_entries_by_prefix,
    list_direct_children,
    search_documents_by_name,
)
```

- [ ] **Step 2: 添加 `_document_summary` 辅助函数**

在 `service_api.py` 的 `_file_summary` 函数之后，添加 `_document_summary`（比 `_file_summary` 多 `processing_status`）：

```python
def _document_summary(f: DbFile) -> dict[str, Any]:
    return {
        "id": f.id,
        "path": f.uri,
        "name": f.name,
        "size": f.size,
        "mime_type": f.mime_type,
        "processing_status": f.processing_status.value if f.processing_status else None,
        "updated_at": f.updated_at.isoformat() if f.updated_at else None,
    }
```

- [ ] **Step 3: 添加路由函数**

在 `service_api.py` 的 `service_document_by_path` 路由之后，添加新端点：

```python
@router.get("/workspaces/{workspace_name}/documents/search-by-name")
async def service_search_documents_by_name(
    workspace_name: str,
    filename: str = Query(..., min_length=1, description="Filename substring (case-insensitive)"),
    path_prefix: str = Query(default="/", description="Limit search scope to this path prefix"),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    ctx: ServiceTokenContext = Depends(get_service_token_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    ws = require_workspace_for_name(db, workspace_name)
    assert_token_workspace_permission(ctx, ws.id, "read")
    rows, total = search_documents_by_name(db, ws.id, filename, path_prefix, skip, limit)
    return {
        "items": [_document_summary(f) for f in rows],
        "total": total,
        "skip": skip,
        "limit": limit,
    }
```

- [ ] **Step 4: Commit**

```bash
git add openrag/src/openrag/api/service_api.py
git commit -m "feat: add GET documents/search-by-name endpoint to service API"
```

---

### Task 3: 更新接入指南文档

**Files:**
- Modify: `docs/05-Service-Token接入指南.md`

- [ ] **Step 1: 更新接口数量和接口一览表**

修改第 1 节快速开始中的 "7 个机读接口" → "8 个机读接口"。

在接口一览表（第 4 节）中追加一行：

```markdown
| `GET` | `/workspaces/{workspace_name}/documents/search-by-name` | 按文件名子串模糊搜索 | read |
```

- [ ] **Step 2: 新增 5.8 子节**

在 5.7（语义检索）之后，新增 5.8 子节：

```markdown
### 5.8 GET `/workspaces/{workspace_name}/documents/search-by-name` — 按文件名模糊搜索

**Query 参数：**

| 参数 | 类型 | 必填 | 默认 | 约束 | 说明 |
|------|------|------|------|------|------|
| `filename` | string | **是** | — | min_length=1 | 文件名子串，大小写不敏感；`%` 和 `_` 自动转义 |
| `path_prefix` | string | 否 | `/` | — | 限定搜索范围到该路径前缀下（含自身） |
| `skip` | int | 否 | `0` | ge=0 | 分页偏移 |
| `limit` | int | 否 | `50` | ge=1, le=200 | 分页大小 |

仅返回文件（排除目录），按文件名字母序排序。

**响应示例：**

```json
{
  "items": [
    {
      "id": 45,
      "path": "/docs/report.pdf",
      "name": "report.pdf",
      "size": 1048576,
      "mime_type": "application/pdf",
      "processing_status": "completed",
      "updated_at": "2026-04-20T14:00:00+00:00"
    }
  ],
  "total": 12,
  "skip": 0,
  "limit": 50
}
```
```

- [ ] **Step 3: 新增 cURL 和 Python 调用示例**

在 cURL 示例区（6.1）追加：

```bash
**按文件名模糊搜索：**

```bash
curl -sS -H "X-OpenRag-Token: sk-xxxxxxxx" \
  "https://api.example.com/service/v1/workspaces/MyWorkspace/documents/search-by-name?filename=report&path_prefix=%2Fdocs"
```
```

在 Python 示例区（6.2）追加：

```python
# 按文件名模糊搜索
r = requests.get(
    f"{WS_URL}/documents/search-by-name",
    params={"filename": "report", "path_prefix": "/docs", "limit": 10},
    headers=HEADERS,
    timeout=30,
)
r.raise_for_status()
print(r.json())
```

- [ ] **Step 4: 更新状态码速查表**

在第 7 节状态码表追加一行（path_prefix 含 `..` 的情况已被现有条目覆盖，无需新增）。

但排障指南（第 8 节）追加一行：

```markdown
| 文件名搜索返回 0 结果 | 文件名不匹配或 `path_prefix` 限定范围内无文件 | 尝试缩短关键词或扩大 path_prefix 范围 |
```

- [ ] **Step 5: Commit**

```bash
git add docs/05-Service-Token接入指南.md
git commit -m "docs: update Service Token guide with search-by-name endpoint"
```

---

### Task 4: 验证

- [ ] **Step 1: 启动 API 服务**

```bash
cd openrag/src && python -m uvicorn openrag.api.main:app --reload
```

- [ ] **Step 2: 访问 `/docs` 确认新端点出现在 service 标签下**

浏览器打开 `http://localhost:8000/docs`，确认 `GET /service/v1/workspaces/{workspace_name}/documents/search-by-name` 存在，参数和响应模型正确。

- [ ] **Step 3: 测试基本调用**

使用已知的 service token 和 workspace，调用：

```bash
curl -sS -H "X-OpenRag-Token: sk-xxx" \
  "http://localhost:8000/service/v1/workspaces/<ws_name>/documents/search-by-name?filename=test"
```

确认返回 `{items, total, skip, limit}` 结构。

- [ ] **Step 4: 测试分页**

```bash
curl -sS -H "X-OpenRag-Token: sk-xxx" \
  "http://localhost:8000/service/v1/workspaces/<ws_name>/documents/search-by-name?filename=test&skip=0&limit=5"
```

- [ ] **Step 5: 测试 path_prefix 限定**

```bash
curl -sS -H "X-OpenRag-Token: sk-xxx" \
  "http://localhost:8000/service/v1/workspaces/<ws_name>/documents/search-by-name?filename=test&path_prefix=/docs"
```

- [ ] **Step 6: 测试错误场景**

- 缺少 `filename` 参数 → 422
- 无效/缺失 token → 401
- workspace 不存在 → 404
- path_prefix 含 `..` → 400