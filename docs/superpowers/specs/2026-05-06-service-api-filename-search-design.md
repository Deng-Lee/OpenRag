# Service Token API: 文件名模糊查询端点设计

**日期：** 2026-05-06
**状态：** Approved

## 背景

现有 `/service/v1` 端点只支持路径类查询（精确路径、路径前缀、目录树、一级子项），不支持按文件名搜索。JWT 层的 `GET /files/` 有 `filename` 参数做 `ilike` 子串匹配，但 service API 层没有对等能力。外部系统需要通过文件名快速定位文档时，只能先拉全量再本地过滤，效率低。

## 方案

新增独立端点 `GET /service/v1/workspaces/{workspace_name}/documents/search-by-name`，职责清晰，与现有 `by-path`（精确路径）、`by-prefix`（路径前缀扁平列表）形成互补三角：

| 端点 | 查询维度 | 典型用途 |
|------|----------|----------|
| `documents/by-path` | 精确路径 | 已知路径取元数据 |
| `entries/by-prefix` | 路径前缀 | 按目录浏览 |
| `documents/search-by-name` | 文件名子串 | 按名称定位文档 |

## 端点定义

**方法：** `GET`
**路径：** `/service/v1/workspaces/{workspace_name}/documents/search-by-name`
**所需令牌权限：** `read` 或 `write`
**仅返回文件（排除目录）**

### Query 参数

| 参数 | 类型 | 必填 | 默认 | 约束 | 说明 |
|------|------|------|------|------|------|
| `filename` | string | 是 | — | min_length=1 | 文件名子串，大小写不敏感；`%` 和 `_` 自动转义 |
| `path_prefix` | string | 否 | `/` | — | 限定搜索范围到该路径前缀下（含自身） |
| `skip` | int | 否 | 0 | ge=0 | 分页偏移 |
| `limit` | int | 否 | 50 | ge=1, le=200 | 分页大小 |

### 响应

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

`items` 每项字段：`id`, `path`, `name`, `size`, `mime_type`, `processing_status`, `updated_at`。
比 `_file_summary` 多 `processing_status`，因为是文档级查询，外部系统需知道文件是否可检索。

### 错误码

| 状态码 | detail | 条件 |
|--------|--------|------|
| 400 | `Invalid path: path traversal detected` | path_prefix 含 `..` |
| 401 | `Invalid or missing service token` | 令牌缺失/无效/已吊销 |
| 403 | `Token not authorized for this workspace` | 令牌绑定工作区不一致 |
| 403 | `Write/Read permission required` | 权限不足 |
| 404 | `Workspace not found` | 工作区名不存在 |

## 实现细节

### 1. 新增服务函数

在 `workspace_file_tree.py` 中新增：

```python
def search_documents_by_name(
    db: Session,
    workspace_id: int,
    filename_pattern: str,
    path_prefix: str = "/",
    skip: int = 0,
    limit: int = 50,
) -> tuple[list[File], int]:
```

- 复用 `_escape_ilike()`（从 `files_api.py` 提取到公共位置或就地复制）
- 查询：`File.workspace_id == ws_id`, `File.is_directory == False`, `File.name.ilike(f"%{escaped}%")`
- `path_prefix` 过滤复用 `_under_prefix()` 或 SQL `uri.startswith`
- total 为总匹配数（不计分页），items 为分页切片
- 排序：按 `name` 字母序

### 2. 新增端点

在 `service_api.py` 中新增路由函数 `service_search_documents_by_name`，调用 `search_documents_by_name`，返回 `{items, total, skip, limit}`。

### 3. `_escape_ilike` 复用

`files_api.py` 的 `_escape_ilike` 是私有函数（2 行），两种处理方式：
- **推荐：** 就地复制到 `workspace_file_tree.py`（同层服务，2 行代码不值得跨模块引入依赖）
- 不提取到公共模块，避免为了 2 行代码增加抽象

### 4. 响应字段

新增 `_document_summary(f)` 辅助函数（或直接在端点内构造 dict），返回 `id, path, name, size, mime_type, processing_status, updated_at`。

### 5. 不新增数据库索引

`name` 列目前无索引。工作区内文件量通常可控（数千级），`ilike` 全扫描性能可接受。若后续需优化可加 pg_trgm 索引，但不在本次范围内。

## 文档更新

- `docs/05-Service-Token接入指南.md` 第 4 节接口一览表新增一行
- 新增 5.x 子节详细说明
- 第 7 节状态码表新增条目
- 第 6 节调用示例新增 cURL 和 Python 示例