# 文件夹/文件范围内语义检索 —— 设计文档

- 日期：2026-06-28
- 分支：`query-in-folder-file`
- 状态：设计待评审（Design / pending review）
- 范围：后端检索链路 + 内部 `/search` API + 外部 `/service/v1` API；前端接线列为后续任务（见 §6）

## 1. 背景与目标

**现状**：语义检索以 workspace 为过滤单位。流程是 workspace → 该 workspace 下所有 `file_id` → 作为 Milvus 的 `file_id in [...]` **预过滤**条件 → 在子集上算 COSINE 取 top_k。无法把检索限定到某个**文件夹**或**单个文件**。

**目标**：让一次检索请求能把范围限定到 workspace 内用户指定的一组文件夹/文件（按逻辑路径），其余链路复用现状。

**与既有"文件列表收窄"功能的关系（正交，不重叠）**：
- [2026-06-24-file-preview-action-and-single-file-filter-design.md](2026-06-24-file-preview-action-and-single-file-filter-design.md) 与 [2026-06-24-filelist-filter-by-directory-plan.md](../../2026-06-24-filelist-filter-by-directory-plan.md) 都是**前端文件管理页右侧列表的展示过滤**，不影响检索。
- 本功能作用于**语义检索范围**，是检索层的净新增能力。两者的"文件夹递归含子孙"语义保持一致。

> **更新记录（2026-06-28）**：本文 §2–§8 已并入 Codex 审查的 7 项收敛决策（专用 scope 查询、空 scope 提前返回、contextual 全程 scope、`paths` 覆盖 `path_prefix`、`paths` 请求级约束、测试收集口径、内部 `/search` workspace read 校验）。详细审查记录见文末《Codex 审查结果》附录。

## 2. 已确认的决策

| 决策点 | 选择 |
|---|---|
| 标识方式 | **方案 B**：`paths: list[str]` + `workspace_id`（不用 `file_ids`） |
| 为何不用 `file_ids` | 复用前缀规则、契合既有 `{workspace_id, path}` 子树惯例、避免复活已 `@deprecated` 的 `file_ids` 过滤（见 §3） |
| 多选 | 支持多个路径，文件夹/文件可混合 |
| 文件夹递归 | **递归含子孙**（URI 前缀，与既有 `under_path` 语义一致） |
| `paths` 请求级约束 | ≤50 个、单路径 ≤1024 字符、空白项 → 400、归一化（拒 `..`）、去重保序、归一化为 `/` 即全 workspace（scope=None）（Codex #5） |
| 无效路径 / 空 scope | 不存在路径或 `paths=[]` → **空结果**（不报错）；`..` → 400；空 scope 必须在 retrieval 层提前返回，**不下传 store**（Codex #2） |
| 过滤方式 | **预过滤**（Milvus `file_id in [...]`），并贯穿 contextual 的 L0/L1/L2/ES（Codex #3） |
| 外部新旧参数优先级 | **`paths` 优先并覆盖 `path_prefix`**（含 `paths=[]`）；仅 `paths` 缺省/`null` 时回退 `path_prefix`（Codex #4） |
| 外部 API | **内部 + 外部都做**；`path_prefix` 由「后过滤」升级为「预过滤」 |
| 内部 `/search` 鉴权 | 进入检索前校验 workspace read（**403**，与 `get_chunk_context` 一致）；`workspace_id=None` 维持旧语义，admin 放行（Codex #7） |
| 权限收窄 | scope 与 `_accessible_file_ids` 求**交集**，只收窄、不放宽 |

## 3. 关键现状（代码事实）

**内部检索链路**（一条过滤通道，便于嵌入）：
- 入口 [_execute_search](../../../openrag/src/openrag/api/search_api.py)（search_api.py:237）→ `RetrievalService.search`（retrieval_service.py:128）→ `_search_flat`（retrieval_service.py:224）/ `_search_contextual`（retrieval_service.py:324）。
- `_accessible_file_ids(user_id, workspace_id)`（retrieval_service.py:695）是传给向量库的**唯一**过滤来源；它把 `workspace_id` 解析为该 workspace 的全部 `file_id`，admin 且无 workspace 时返回 `None`（= 全部）。
- 两个 `_search_*` 都有 `if accessible is not None and len(accessible) == 0: return []` 的空集合提前返回（retrieval_service.py:235、:338）。
- `vector_store.search(query_embedding, top_k, file_ids=...)`（milvus_store.py:205）把 `file_id` 列表拼成 `file_id in [...]` 的 `expr`（milvus_store.py:237-244）；超过 `_MAX_FILE_IDS_IN_EXPR` 时改为多召回 + 内存 `post_filter`。contextual 路径在 `len(accessible) > 512` 时也走 post-filter（retrieval_service.py:341）。**→ 大范围 id 列表已被现状兜底，向量层无需改动。**

**既有子树解析（借用其前缀规则，但不直接调用）**：
- [list_entries_by_prefix(db, workspace_id, path_prefix)](../../../openrag/src/openrag/services/workspace_file_tree.py)（workspace_file_tree.py:119）：内置路径归一化与 `..` 防护（`_normalize_logical_path`，:23）、URI 前缀递归匹配（`uri == p OR uri LIKE p + '/%'`），单文件路径只返回自身。**但**它会先取出前缀下所有目录/文件行、并在超 `TREE_MAX_NODES=5000` 时抛 400（:134-138）——选根/大文件夹会在进入向量层前失败（Codex #1）。故 scope 解析**复用其前缀规则、改走专用 id 查询**（见 §4.1），不继承该上限。

**两个易踩的现状（Codex 核实）**：
- **空 `file_ids` ≠ 空结果**：`MilvusStore.search(file_ids=[])` 把空列表清成 `None`（milvus_store.py:222-231）、`MilvusLayerStore.search_layers(file_ids=[])` 因 `if file_ids:` 为假（milvus_layer_store.py:167-168）——都退化为**不加过滤、搜全库**。故空 scope 必须在 retrieval 层提前 `return []`；所幸两个 `_search_*` 已有 `len==0 → return []` 守卫（:235、:338），只要 `_effective_file_ids` 对空交集返回 `[]`（非 `None`）即可命中。
- **内部 `/search` 无 workspace read 校验**：`_accessible_file_ids` 在"非管理员 + workspace_id"时直接返回该 workspace 全部 `File.id`，**未调用** `check_user_permission`（retrieval_service.py:714-722）；非管理员改请求体 `workspace_id` 即可搜任意 workspace。本次一并修复（§4.0）。

**`file_ids` 的历史**：前端 `SearchRequest` 仍保留 `file_ids?: number[]` 但已标 `@deprecated 后端以 workspace_id + 权限为准`（web/src/types/index.ts:108-109）；后端 `SearchRequest` 已无此字段。本设计不复用该字段。

**外部（service token）API 现状**：
- 单 workspace：`POST /service/v1/workspaces/{workspace_name}/search`，请求体 `ServiceSearchRequest`（service_api.py:69），已有 `path_prefix`（:73）。
- 多 workspace：`POST /service/v1/workspaces/multi_space/search`，请求体 `ServiceMultiWorkspaceSearchRequest`（:87），已有 `path_prefix`（:92）。
- 两端点都**先**构造内部 `SearchRequest` 并调用 `_execute_search`（:420、:488），**再**用 [_apply_path_prefix_filter](../../../openrag/src/openrag/api/service_api.py)（:140）对结果做过滤（:427、:495）。
- **缺陷**：`_apply_path_prefix_filter` 是**后过滤**——先在整个 workspace 检索取 top_k，再丢掉不在前缀下的命中。若 top_k 内落在该前缀的命中很少，前缀下排名靠后但更相关的 chunk 永远拿不到，**召回被削减**。本设计将其替换为预过滤。

## 4. 方案设计

### 4.0 内部 JWT 搜索入口：workspace read 前置校验（Codex #7）

- 新增 `assert_search_workspace_read(db, user_id, workspace_id)`：`workspace_id is None` 放行（维持"按用户可访问 workspace 集合检索"的旧语义）；用户为 admin 放行；否则 `WorkspaceService(db).check_user_permission(workspace_id, user_id, "read")`，未通过抛 **403**（与 `get_chunk_context` 一致，search_api.py:597-601）。
- 调用点：**仅** JWT 端点 `semantic_search`（search_api.py:394）、`hierarchical_search`（:416），在 `_execute_search` **之前**。必须先于解析 `paths`、embedding、向量检索与 trace，避免无权限用户借 `paths` 探测目录是否存在。
- **不**放进 `_execute_search`：该函数被外部 `/service/v1` 共用，外部用 service token 鉴权（`assert_token_workspace_permission` / `_token_can_read_workspace`）、`user_id=ws.owner_id`，不套这套 JWT 校验。

### 4.1 共享主干：解析 → 约束 → 交集 → 预过滤

**a) `paths` 约束与归一化**（`normalize_scope_paths`，`workspace_file_tree.py`）：
- `None` → 返回 `None`（未指定新范围）。
- 校验：列表 > 50 → 400；任一项规范化前 > 1024 字符 → 400；空串/纯空白项 → 400。
- 逐项按既有规则规范化（补前导 `/`、折叠重复分隔与 `.`、拒绝 `..`，等价 `_normalize_logical_path`），规范化后去重并**保持首次出现顺序**（如 `["docs","/docs/"]` 只留 `/docs`）。
- 显式 `[]` → 返回 `[]`（空范围 → 产出空结果）。

**b) scope 解析**（`resolve_scope_file_ids → Optional[set[int]]`，**专用 id 查询，不调用** `list_entries_by_prefix`，Codex #1）：
```python
def resolve_scope_file_ids(db, workspace_id, paths) -> Optional[set[int]]:
    norm = normalize_scope_paths(paths)
    if norm is None:
        return None                       # 未指定范围
    if "/" in norm:
        return None                       # 含根 → 全 workspace（不继承 TREE_MAX_NODES）
    ids: set[int] = set()
    for p in norm:                        # p 已规范化、无尾斜杠
        rows = db.execute(
            select(File.id).where(
                File.workspace_id == workspace_id,
                File.is_directory.is_(False),
                (File.uri == p) | (File.uri.startswith(p + "/")),
            )
        ).scalars().all()
        ids.update(rows)
    return ids                            # norm==[] → 空 set（空范围）
```
- 只查 `File.id`、限定 `is_directory=False`，无树展示职责、不继承 5000 上限。
- 不存在路径 / 空文件夹 → 0 个 id；`norm==[]` → 空 set。两者都落到"空范围"。

**c) 交集 helper**（`_effective_file_ids`，`retrieval_service.py`）：
```python
def _effective_file_ids(self, user_id, workspace_id, scope_file_ids):
    accessible = self._accessible_file_ids(user_id, workspace_id)
    if scope_file_ids is None:
        return accessible                 # 未指定范围 → 维持现状
    if accessible is None:                # admin / “全部”
        return list(scope_file_ids)       # 可能为 []
    return [fid for fid in accessible if fid in scope_file_ids]   # 可能为 []
```
- scope 只**收窄** accessible，不可能越权。
- **空范围不变量（Codex #2）**：scope 非 `None` 时返回值恒为确定性 `list`（含空 `[]`），**绝不返回 `None`**，以命中下游 `len==0 → return []` 守卫。

**d) 空范围提前返回（Codex #2）**：`_search_flat` / `_search_contextual` 把 `_accessible_file_ids(...)` 换成 `_effective_file_ids(...)` 后，紧随的既有 `if effective is not None and len(effective)==0: return []`（:235、:338）即覆盖空范围；空 `[]` **不得**下传 `vector_store.search` / `layer_store.search_layers`（否则退化为搜全库）。

**e) scope 贯穿 contextual（Codex #3）**：替换发生在 `_search_contextual` 取 `accessible` 的**源头**，effective 自然贯穿 L0 搜索与大集合 post-filter、L1 `candidate_files`、L2 chunk 搜索、`_blend_elasticsearch_scores` 的 `filter_file_ids`。

**f) 透传**：`search` / `_search_flat` / `_search_contextual` 各加 `scope_file_ids: Optional[set[int]] = None`；`search()` 透传到**全部**分发分支——`precise/flat` 策略、`layer_store is None` 退化、contextual 无 L0 命中 fallback（retrieval_service.py:174、:189、:208、:357）。

**数据流**：
```
JWT 端点: assert_search_workspace_read(db, user_id, workspace_id)   # §4.0，先于一切
  └─ _execute_search:
        scope = resolve_scope_file_ids(db, workspace_id, paths)     # None | set（可空）
        svc.search(..., scope_file_ids=scope)
  └─ _search_flat / _search_contextual:
        effective = _effective_file_ids(user, ws, scope)            # None | list（可空）
        if effective is not None and len(effective)==0: return []   # 空范围
        vector_store.search(query_vec, top_k, file_ids=effective)
  └─ Milvus ANN + file_id in [...] → COSINE top_k
```

### 4.2 内部 API（`/search`，`search_api.py`）

- `SearchRequest` 新增 `paths: Optional[list[str]] = Field(None, ...)`。
- 校验：`paths` 非空但 `workspace_id` 为空 → 400（路径仅在 workspace 内唯一；可放模型级校验）。
- `semantic_search` / `hierarchical_search` 入口先调 `assert_search_workspace_read`（§4.0），再 `_execute_search`。
- `_execute_search`（内外共用）：`scope = resolve_scope_file_ids(db, request.workspace_id, request.paths)`，传入两个 `svc.search` 分支（search_api.py:272-285、:288-297）。覆盖 `/search`、`/search/semantic`、`/search/hierarchical`。

### 4.3 外部 API（`/service/v1`，`service_api.py`）

- `ServiceSearchRequest`、`ServiceMultiWorkspaceSearchRequest` 各新增 `paths: Optional[list[str]] = None`；**保留** `path_prefix` 兼容。
- 新增 `_resolve_scope_paths(paths, path_prefix)`（**覆盖语义**，替代原并集 `_merge_paths`，Codex #4）：
```python
def _resolve_scope_paths(paths, path_prefix):
    if paths is not None:                  # 显式传 paths（含 []）→ 覆盖
        return paths                       # 忽略 path_prefix
    if path_prefix and str(path_prefix).strip() and str(path_prefix).strip() != "/":
        return [path_prefix]               # 仅 paths 缺省时回退旧字段
    return None                            # 不限范围
```
- 两个检索端点构造内部 `SearchRequest` 时设 `paths=_resolve_scope_paths(body.paths, body.path_prefix)`（service_api.py:408 多 workspace、:475 单 workspace）。
- **退役** `_apply_path_prefix_filter` 的两处后过滤（:427、:495）——预过滤为权威路径。
- 多 workspace：循环内每个 workspace 用各自 `ws.id` 构造 `SearchRequest`，`_execute_search` 按 `ws.id` 逐 workspace 解析同一组 `paths`；外部 `user_id=ws.owner_id`，`_effective_file_ids` 下 accessible=该 workspace 全部文件，交集即 scope。

## 5. 边界、安全与行为变化

- **鉴权（内部）**：JWT `/search` 进入检索前校验 workspace read（§4.0），无权限 → 403；校验先于 `paths` 解析，杜绝目录探测 oracle。`workspace_id=None` 维持旧语义，admin 放行。外部 `/service/v1` 继续走 service token 校验。
- **权限收窄**：scope 与 `_accessible_file_ids` 求交集，只收窄不放宽。
- **空范围**：不存在路径 / 空文件夹 / `paths=[]` → 空结果；空范围在 retrieval 层提前 `return []`，**不下传 store**（否则退化为搜全库）。
- **路径穿越** `..` → 400；超约束（>50 路径、单路径 >1024 字符、空白项）→ 400。
- **大范围**：`paths=["/"]` 或含根 → scope=None（不限范围、不继承 `TREE_MAX_NODES`）；交集结果即便很大也被向量层既有 post-filter 兜底（milvus_store.py:241、retrieval_service.py:341），向量/ES 层零改动。
- **向后兼容**：内部不传 `paths`、外部不传 `paths` 且 `path_prefix` 留空时，检索行为与现状一致。
- ⚠️ **行为变化（外部）**：`path_prefix` 由「后过滤」改为「预过滤」，结果更准、召回更多——非纯增量兼容，需在外部 API 文档/变更说明告知。
- ⚠️ **行为变化（内部鉴权）**：当前可凭任意 `workspace_id` 检索的非管理员用户，改造后对无 read 权限的 workspace 将收到 403（安全正向）。

## 6. 不做（Non-goals）

- **前端接线**列为后续独立任务：搜索页文件树多选 → 收集各节点 `uri` → 随 `workspace_id` 一起发 `paths`。前端已具备条件（节点带 `uri`、已发 `workspace_id`）。
- 不改 Milvus / ES schema 或检索算子。
- 不支持跨 workspace 的混合范围检索（方案 B 按 workspace 逐一解析路径）。
- 不引入与 `paths` 约束相关的可配置项（50 / 1024 为硬编码最小约束）。

> 注：原 §6 曾把"`_accessible_file_ids` 缺 workspace read 校验"列为 Non-goal；经 Codex #7 复核与用户确认，已移入本次范围（§4.0）。

## 7. 测试

> ⚠️ `pytest.ini` 的 `python_files` 白名单只收集特定文件名（Codex #6）。新用例须放入**已收集**文件，或在 `pytest.ini` 增列。

- **scope 解析**（放入已收集的 [test_workspace_file_tree.py](../../../openrag/tests/test_workspace_file_tree.py)）：单文件路径 / 文件夹递归 / 多路径混合去重保序 / 不存在→空 / `paths=[]`→空 / `paths=["/"]`→全 workspace(None) / `..`→400 / >50→400 / 超长→400 / 空白项→400。
- **retrieval 交集与空范围**（新增 `test_scoped_retrieval.py`，并在 `pytest.ini` 增列）：`_effective_file_ids`（scope=None、admin accessible=None、正常交集、空交集→`[]` 非 `None`）；flat 与 contextual 在空范围下提前 `return []` 且**不**调用 store；contextual 下范围外文件不得进入 L0/L1/L2/ES 候选；`scope_file_ids` 透传到 `precise/flat`、`layer_store is None`、无 L0 命中 fallback。
- **内部 API**（放入已收集文件，或新增并增列）：带 `paths` 只返回范围内 chunk；`paths` 缺 `workspace_id`→400；**鉴权**——有 read→200、无 read→403、admin 任意 workspace→200、`workspace_id=None` 旧行为不变、无权限 workspace+`paths` 先 403 不解析路径（不以空结果伪装成功）。
- **外部 API**（放入已收集的 [test_service_api.py](../../../openrag/tests/test_service_api.py)）：单/多 workspace 带 `paths` 走预过滤；`paths` 覆盖 `path_prefix`（`paths=["/财务"]`+`path_prefix="/法务"`→只搜 `/财务`；`paths=[]`+`path_prefix="/法务"`→空结果）；构造"全 workspace top_k 会挤掉目标目录命中"样例验证预过滤召回；service token 鉴权不受 JWT 鉴权 helper 影响。
- 外部 API 文档同步更新（`path_prefix` 后→预过滤的行为变化）。

## 8. 影响文件清单

| 文件 | 改动 |
|---|---|
| `openrag/src/openrag/services/workspace_file_tree.py` | 新增 `normalize_scope_paths()`、`resolve_scope_file_ids()`（专用 id 查询，不调用 `list_entries_by_prefix`） |
| `openrag/src/openrag/retrieval/retrieval_service.py` | 新增 `_effective_file_ids()`；`search`/`_search_flat`/`_search_contextual` 加 `scope_file_ids` 并透传（4 分支）；空范围提前返回不变量 |
| `openrag/src/openrag/api/search_api.py` | `SearchRequest` 加 `paths`；`paths⇒workspace_id` 校验；新增 `assert_search_workspace_read()` 并在 JWT 端点前置调用；`_execute_search` 解析 scope 并传入 |
| `openrag/src/openrag/api/service_api.py` | 两个 service 请求体加 `paths`；新增 `_resolve_scope_paths()`（覆盖语义）；两端点设 `SearchRequest.paths`；退役 `_apply_path_prefix_filter` |
| `openrag/pytest.ini` | 增列新增 retrieval scope 测试文件名 |
| 外部 API 文档 | 记录 `paths`、`paths` 覆盖 `path_prefix`、`path_prefix` 后→预过滤变化 |
| 相应测试文件 | 见 §7 |

## Codex 审查结果（2026-06-28）

来源：Codex 对本设计文档与当前 `query-in-folder-file` worktree 代码事实做静态审查后的补充；已按用户确认把收敛决策合并到对应风险点。未修改前文原始方案。

### 结论

方案主线是收敛的：用 `paths + workspace_id` 解析出文件范围，再与 `_accessible_file_ids` 求交集，最后复用现有 `file_id` 预过滤链路，比恢复已废弃的 `file_ids` 请求字段更合适。内部 `/search` 与外部 `/service/v1` 统一走 `_execute_search` 也能减少重复实现。

但按当前写法直接实现仍有几个需要先收口的风险，尤其是“大文件夹/根目录范围”“空集合语义”和“外部新旧参数合并语义”。这些不解决，功能容易在小样例通过、在真实大库或边界请求下偏离预期。

### 主要风险点与收敛决策

1. **P1：直接复用 `list_entries_by_prefix` 会继承 `TREE_MAX_NODES=5000` 上限。**
   文档 §5 说大范围 id 列表可由向量层现有 post-filter 兜底，但 `list_entries_by_prefix()` 在 `workspace_file_tree.py` 中会先把前缀下所有目录/文件行取出，并在超过 5000 节点时抛 400。这样选择根目录或大文件夹时，可能还没进入向量层就失败。
   **收敛决策：采纳。** `resolve_scope_file_ids()` 不直接调用 `list_entries_by_prefix()`，而是新增 scope 专用查询：只查 `File.id`，条件包含 `workspace_id`、`is_directory=False`，并复用同一套路径归一化与前缀匹配规则。这样它的职责固定为“路径列表 -> 文件 id 集合”，不承担文件树展示职责，也不继承 `TREE_MAX_NODES`。

2. **P1：空 scope 必须在 retrieval 层提前返回，不能传进向量/层级 store。**
   当前 `MilvusStore.search(file_ids=[])` 会把空列表清洗成 `None`，等价于“不加 file_id 过滤”；`MilvusLayerStore.search_layers(file_ids=[])` 也会因为 `if file_ids` 为假而不加过滤。
   **收敛决策：采纳。** `_effective_file_ids()` 得到空列表时，flat/contextual 都必须在调用 vector store 或 layer store 前直接返回 `[]`。contextual 的 L0、L1、L2 以及 L0 无命中 fallback 到 flat 的路径都要遵守这个规则。测试要覆盖“不存在路径 / 空列表 scope 不会退化为全 workspace 检索”。

3. **P2：contextual 检索需要确认 scope 贯穿 L0/L1/L2/ES 融合。**
   如果只在最终 L2 chunk 搜索应用 scope，范围外文件仍可能进入 L0/L1 候选，影响融合排序和 L1 LLM 导航。
   **收敛决策：采纳。** effective file ids 必须同时用于 L0 搜索、L0 大集合 post-filter、L1 `candidate_files`、L2 chunk 搜索，以及 `_blend_elasticsearch_scores()` 的 `filter_file_ids`。`RetrievalService.search()` 增加 `scope_file_ids` 后，所有 `_search_flat()` / `_search_contextual()` 分支都要透传，包括 `precise/flat` 策略、`layer_store is None` 退化分支、contextual 无 L0 命中 fallback。

4. **P2：外部 API 同时传 `paths` 与 `path_prefix` 时需要明确优先级。**
   原计划中的 `_merge_paths()` 是并集语义，虽然不会越权，但会让调用方难以判断最终范围。例如旧客户端固定传 `path_prefix=/法务`，新逻辑又传 `paths=["/财务"]`，并集会实际搜索两个目录。
   **收敛决策：`paths` 优先，忽略 `path_prefix`。** 只要请求体显式传入 `paths`，包括 `paths=[]`，就只按 `paths` 解析范围，`path_prefix` 不参与合并、不报错。只有 `paths` 缺省或为 `null` 时，才回退兼容旧字段 `path_prefix`；`path_prefix="/"`、空字符串或缺省表示不限范围。建议把 helper 命名为 `_resolve_scope_paths()`，避免继续使用暗示“合并”的 `_merge_paths()`。

5. **P2：`paths` 需要请求级约束与规范化。**
   目前设计没有限制路径数量、单个路径长度或空白字符串。外部 service token 可以一次传很多 path，导致多次 DB 前缀查询。
   **收敛决策：采纳以下最小约束，不引入配置项。** `paths` 为可选 `list[str]`；不传或 `null` 表示未指定新范围，外部 API 可回退 `path_prefix`。显式 `paths=[]` 表示空范围，直接返回空结果，并忽略 `path_prefix`。列表内空字符串或纯空白字符串返回 400。单次最多 50 个路径，单路径规范化前最多 1024 字符，超出返回 400。每个路径复用现有 `_normalize_logical_path` / `validate_path` 等价规则：补前导 `/`、折叠重复分隔与 `.`、拒绝 `..` 穿越。规范化后去重，保持首次出现顺序；例如 `["docs", "/docs/"]` 只解析一次 `/docs`。规范化后若包含 `/`，表示全 workspace 范围，可直接把 scope 视为 `None`，不用再解析其它路径。不存在路径或空文件夹合法，解析不到文件 id 时返回空结果。目录与文件允许混合，文件路径只贡献自身文件 id，目录路径贡献其子树下所有非目录文件 id。

6. **P2：测试计划引用的部分测试文件不在默认 pytest 收集白名单内。**
   当前 `openrag/pytest.ini` 只默认收集 `test_service_token_*.py`、`test_service_api.py`、`test_service_tokens_admin.py`、`test_workspace_file_tree.py`、`test_workspace_es_slug.py`、`test_files_api.py`、`test_*_parity.py`。文档 §7 提到的 `test_l0_l1_retrieval_flag.py`、`test_retrieval_trace.py`、`test_preview_external_api_docs.py` 默认不会被收集。建议把新增用例放进已收集文件，或同步更新 `pytest.ini` / CI 命令，避免“本地显式跑过、默认回归没覆盖”。

7. **P1：内部 JWT `/search` 缺少显式 workspace read 权限校验。**
   这是本功能之前就存在的后端授权边界问题，不是 `paths` 新增出来的漏洞。当前 `/search` 入口只通过 `get_current_active_user` 确认 JWT 有效；`_execute_search` 随后把 `request.workspace_id` 直接传给 `RetrievalService.search()`。`_accessible_file_ids(user_id, workspace_id)` 在非管理员且传入 `workspace_id` 时，会直接查询该 workspace 下的全部 `File.id`，没有显式调用 `WorkspaceService.check_user_permission(workspace_id, user_id, "read")`。因此，用户如果绕过前端直接修改请求体里的 `workspace_id`，后端搜索入口本身不能证明该用户有这个 workspace 的 read 权限。
   **收敛决策：本问题需要随本次检索范围改造一并修复。** 修复点放在内部 JWT 搜索入口或 `_execute_search` 进入检索前：当 `request.workspace_id` 非空时，先用 `WorkspaceService(db).check_user_permission(request.workspace_id, user_id, "read")` 校验；未通过则返回 403（或按项目统一风格返回 404，但需固定一种）。校验必须发生在解析 `paths`、embedding、向量检索和 trace 成本较高的步骤之前，避免无权限用户通过 `paths` 探测目录是否存在。外部 `/service/v1` 不使用这套 JWT 校验，它继续依赖已有 service token workspace 权限校验（如 `assert_token_workspace_permission` / `_token_can_read_workspace`）。

### 收敛性评估

整体是收敛的，改动面限定在五处：内部 JWT 搜索入口权限校验、scope 解析、retrieval 交集透传、内部请求模型、外部 service 请求模型与兼容参数。没有必要改 Milvus / ES schema，也不建议恢复前端已废弃的 `file_ids` 字段。

需要收敛的地方是 `resolve_scope_file_ids()` 的实现方式：它不应该承担“构建文件树”的职责，也不应该返回目录行后再过滤。更好的边界是“路径列表 -> 文件 id 集合”，只查询必要列，保持无副作用、无树展示上限、可单元测试。

### 建议的更好实现方式

1. 新增 scope 专用 helper，例如 `resolve_scope_file_ids(db, workspace_id, paths) -> set[int]`，内部先规范化并去重 paths。对每个 path 使用与 `_under_prefix` 一致的规则查询 `File.id`，条件包含 `workspace_id` 与 `is_directory=False`。不要调用 `list_entries_by_prefix()`。
2. 新增 `normalize_scope_paths(paths)`，集中处理最小约束：空白 400、最多 50 个、单路径最多 1024 字符、路径归一化、去重、`/` 表示全 workspace scope。
3. `_effective_file_ids()` 返回值建议保持 `None | list[int]`，并在返回前把 set/list 转成确定性的 list。对空交集，retrieval 层立即 `return []`，不要依赖 store 处理空列表。
4. `RetrievalService.search()` 增加 `scope_file_ids` 参数后，所有调用 `_search_flat()` / `_search_contextual()` 的分支都要透传，包括 `precise/flat` 策略、`layer_store is None` 退化分支、contextual 无 L0 命中 fallback。
5. 外部 API 使用 `_resolve_scope_paths(paths, path_prefix)`：显式传 `paths` 时优先使用 `paths` 并忽略 `path_prefix`；只有 `paths` 缺省或为 `null` 时才回退 `path_prefix`。
6. 内部 JWT `/search`、`/search/semantic`、`/search/hierarchical` 在进入检索前调用 `assert_search_workspace_read(db, user_id, request.workspace_id)`；`workspace_id=None` 保持既有“按用户可访问 workspace 集合搜索”的语义，admin 由 `WorkspaceService.check_user_permission` 放行。

### 建议补充的验收用例

- 不存在路径、空文件夹、`paths=[]`、`paths=["/不存在"]` 均返回空结果，且不会退化为全 workspace 检索。
- `paths=["/"]` 表示全 workspace scope，不继承 `TREE_MAX_NODES=5000`；不传 `paths` 或 `paths=null` 时外部 API 才回退 `path_prefix`。
- contextual 检索开启时，范围外文件不能出现在 L0、L1、L2、ES 融合候选中。
- 外部 `path_prefix` 升级为预过滤后，构造一个“全 workspace top_k 会挤掉目标目录命中”的样例，验证新逻辑能召回目标目录内结果。
- 同时传 `paths` 与 `path_prefix` 时覆盖 `paths` 优先：`paths=["/财务"]` + `path_prefix="/法务"` 只搜索 `/财务`；`paths=[]` + `path_prefix="/法务"` 返回空结果。
- `paths` 约束覆盖：空白项 400、超过 50 个 400、超长路径 400、`..` 400、规范化去重、文件和目录混合去重。
- 内部 JWT `/search` 权限覆盖：有 read 权限的 workspace 返回 200；无 read 权限的 workspace 返回 403/约定错误；admin 搜索任意 workspace 返回 200；`workspace_id=None` 旧行为不变；无权限 workspace + `paths=["/某目录"]` 先权限失败，不解析路径、不返回空结果伪装成功。
- 外部 `/service/v1` 权限覆盖：service token 搜索仍走已有 token workspace 权限校验，不受 JWT `/search` 权限 helper 影响。

## Codex 复审结果（2026-06-28）

来源：Codex 对 Claude Code 修改后的当前计划进行复审。本节仅追加在文档末尾。

### 结论

当前计划的主体风险已经收敛，可以推进到下一步实现。上一轮指出的关键点已经进入正式方案正文，而不是只停留在审查附录里：

- `resolve_scope_file_ids()` 已改为 scope 专用 id 查询，不直接调用 `list_entries_by_prefix()`，避免继承 `TREE_MAX_NODES=5000` 和树展示职责。
- 空 scope / 空交集明确在 retrieval 层提前返回，避免把 `[]` 传给 Milvus chunk store 或 layer store 后退化成全库检索。
- contextual 检索要求 scope 贯穿 L0、L1、L2、ES 融合和 fallback 分支，方向正确。
- 外部 API 已明确 `paths` 优先覆盖 `path_prefix`，含 `paths=[]` 的行为也已写清。
- `paths` 最小约束已收敛为固定规则：最多 50 个、单路径 1024 字符、空白项 400、规范化去重、`/` 表示全 workspace。
- 内部 JWT `/search` 的 workspace read 权限缺口已纳入本次范围，并明确只在 JWT 搜索入口前置校验，不影响 `/service/v1` 的 service token 鉴权链路。
- 测试收集风险已处理：新增 retrieval scope 测试计划写明需要在 `pytest.ini` 增列，其他用例尽量放入已收集文件。

### 非阻断提醒

- 实现时保持 §4.0 的边界：JWT 端点先校验权限再进 `_execute_search`；不要把 JWT workspace read 校验放进外部 service token 路径。
- `paths=[]` 是显式空范围，不是“不限范围”；`paths is None` 才允许外部 API 回退 `path_prefix`。这一点需要在请求解析 helper 和测试里保持一致。

除以上实现时需遵守的边界外，没有发现新的阻断性冲突或需要继续修改方案的主体风险。
