# R-01 权限解析 Fail-Open 修复方案

> 文档状态：已实施，独立复审通过；发布门禁存在既有 MinIO 环境失败
> 编制日期：2026-07-10
> 目标分支：`code-optimization`
> 基线提交：`a13e3cb43dab824cf2e39345d6293ba780cd6df6`
> 风险来源：[OpenRag 架构风险与检索优化评审](./2026-07-10-OpenRag架构风险与检索优化评审.md#r-01-权限解析异常时fail-open搜索全部文件)

## 1. 背景与问题

当前风险位于 `openrag/src/openrag/retrieval/retrieval_service.py` 的 `_accessible_file_ids()`：普通用户进行未指定 `workspace_id` 的跨工作区搜索时，如果工作区权限解析发生异常，代码会记录 `searching all files` 并返回 `None`。

但在后续检索链路中，`None` 同时是“无需文件过滤”的合法信号，因此数据库超时、成员关系坏数据、代码回归或意外类型错误都可能把一次权限解析失败放大为全局检索，形成跨工作区数据泄漏路径。

当前语义需要调整为：

- `None`：仅允许表示已经确认的系统管理员全局搜索。
- `[]`：权限正常解析，但用户没有任何可访问文件。
- 权限解析异常：中止本次检索并返回 `503`，不能返回 `None` 或伪装为空结果。
- 不使用缓存权限、历史权限或无过滤检索作为异常降级手段。

该策略遵循 OWASP 的 deny-by-default、权限检查失败安全退出及 RAG 全链路 fail-closed 原则：

- [OWASP Authorization Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html)
- [OWASP RAG Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/RAG_Security_Cheat_Sheet.html)
- [OWASP API1:2023 Broken Object Level Authorization](https://owasp.org/API-Security/editions/2023/en/0xa1-broken-object-level-authorization/)

## 2. 目标与成功标准

本方案的目标是彻底消除“权限范围无法证明时扩大检索范围”的行为，同时保持现有正常检索接口兼容。

成功标准：

1. 任意权限解析异常都不能触发 Embedding、Milvus、层级存储、Elasticsearch 或 reranker。
2. 权限解析异常统一返回脱敏 `503 Service Unavailable`，不能暴露数据库异常和内部堆栈。
3. 权限正常但没有可访问文件时仍返回 `200` 和空结果。
4. 明确缺少指定工作区 read 权限时仍返回 `403`。
5. 即使底层检索存储忽略 `file_ids` 过滤条件，越权命中也不能进入 Trace、数据库回表或最终响应。
6. 管理员全局搜索和现有正常受限搜索行为保持不变。

## 3. 实施范围

### 3.1 权限范围解析

在检索层新增内部异常 `PermissionScopeResolutionError`，用于表达“权限范围未能可靠解析”。

调整 `_accessible_file_ids()`：

- 把用户查询、工作区查询、成员/角色权限查询和文件范围查询纳入同一个 fail-closed 边界。
- 捕获权限范围解析过程中出现的异常，使用 `logger.exception()` 记录上下文与堆栈，然后抛出 `PermissionScopeResolutionError`。
- 禁止在任何异常分支返回 `None`。
- 已确认系统管理员且未指定工作区时返回 `None`。
- 指定工作区时返回该工作区内未软删除文件的 ID；工作区没有有效文件时返回 `[]`。
- 普通用户进行全局搜索时，返回其具有 read 权限的工作区内未软删除文件的 ID。
- 用户不存在、没有可访问工作区或权限正常解析但没有文件时返回 `[]`。

不改变存储层当前 `Optional[list[int]]` 接口，避免无关的类型和调用链重构；通过异常契约确保 `None` 只能来自已经确认的管理员全局路径。

### 3.2 检索前置门禁

平面检索和层级检索必须在以下操作之前完成权限范围解析：

1. Query Embedding。
2. Milvus L0/L1/L2 检索。
3. Elasticsearch 全文评分。
4. L1 LLM 导航。
5. reranker 重排。

权限范围解析抛出 `PermissionScopeResolutionError` 后必须立即终止，不允许继续执行或降级到其他召回通道。

### 3.3 受限命中反向校验

对非 `None` 的受限文件范围构造授权 ID 集合，对底层检索返回的命中进行反向校验：

- 平面检索：Milvus 命中必须属于授权集合。
- 层级检索：L0、L1 和最终 L2 命中必须分别属于当前有效授权集合或已授权候选文件集合。
- `file_id` 缺失、无法转换为整数或不在授权集合中的命中直接丢弃。
- 反向校验必须发生在 Trace Snapshot、`_enrich_hits()` 数据库回表、ES 融合、LLM 导航和结果格式化之前。
- 发现非法或越权命中时记录安全告警日志，仅包含阶段、用户、工作区、命中数量等元数据，不记录 chunk 正文。

该校验是存储层过滤之外的纵深防御，不能替代 Milvus 和 Elasticsearch 的前置授权过滤。

### 3.4 API 错误映射

在 `openrag/src/openrag/api/search_api.py` 的 `_execute_search()` 公共执行入口统一处理 `PermissionScopeResolutionError`：

- HTTP 状态：`503 Service Unavailable`。
- 响应内容：

```json
{
  "detail": "Search authorization temporarily unavailable"
}
```

- 不向客户端返回原始异常信息、SQL、表名、连接地址或堆栈。
- JWT 语义搜索、JWT 层级搜索、单工作区 Service Token 搜索和多工作区 Service Token 搜索都通过 `_execute_search()` 获得一致行为。
- 当前各入口已有的 `except HTTPException: raise` 行为保持不变，避免 Service Token 接口把 `503` 再转换为 `500`。

### 3.5 Trace 与日志

权限解析失败时：

- Trace Run 标记为 `failed`。
- `error_stage` 固定为 `authorization.scope_resolution`。
- Trace 中只保存稳定、脱敏的错误标识，不保存底层数据库异常文本。
- 应用日志记录事件名、`user_id`、`workspace_id`、搜索入口和底层异常类型，并通过异常日志保留服务端堆栈。
- 不记录 query 正文、文件名、文件 ID 列表或 chunk 内容。

项目当前没有统一 Prometheus 指标基础设施，本次不引入新的监控依赖。上线后基于 `authorization.scope_resolution` Trace 和对应安全日志配置告警。

## 4. 接口与兼容性

### 4.1 保持不变

- 搜索请求模型不变。
- 成功响应结构不变。
- 数据库结构不变，不需要迁移。
- 管理员全局搜索继续允许无文件 ID 过滤。
- 指定工作区明确无 read 权限继续返回 `403`。
- 权限正常但范围为空继续返回 `200` 和空结果。

### 4.2 新增行为

- 权限范围无法可靠解析时返回 `503`。
- `503` 表示客户端可重试的服务故障，不代表用户被拒绝授权。
- 不设置允许恢复 fail-open 的配置开关。

### 4.3 明确不在本次范围

- 不处理 R-02 文件级 ACL 未进入实际数据面的问题。
- 不调整 workspace role、team、owner 和 service token 的授权优先级。
- 不引入权限缓存、安全域分片或新的权限模型。
- 不重构 Milvus、Elasticsearch 或 Trace 的公共接口。
- 不修改前端搜索交互；现有统一错误处理继续展示后端 `503`。

## 5. 测试方案

### 5.1 权限解析故障注入

覆盖以下异常：

- 用户查询异常。
- 工作区权限查询超时或 SQLAlchemy 异常。
- 成员或角色关系解析异常。
- 指定工作区文件范围查询异常。

每种情况都必须断言：

- 抛出 `PermissionScopeResolutionError`。
- 不返回 `None`。
- Embedding、Milvus、Layer Store、Elasticsearch 和 reranker 调用次数均为零。

### 5.2 API 契约

覆盖语义、层级和 Service Token 搜索入口：

- 权限解析异常返回 `503`。
- `detail` 固定为 `Search authorization temporarily unavailable`。
- 响应中不包含注入的底层异常文本。
- Trace 的 `error_stage` 为 `authorization.scope_resolution`。

### 5.3 权限语义回归

- 管理员全局搜索仍返回 `None` 范围并执行无文件 ID 过滤检索。
- 管理员指定空工作区返回 `[]`，不能扩大为全局搜索。
- 普通用户没有可访问工作区时返回 `[]` 和空结果。
- 指定工作区明确无 read 权限时，在进入 `_execute_search()` 前返回 `403`。
- 软删除文件不进入授权范围。
- 请求路径范围继续与授权文件范围取交集。

### 5.4 纵深防御

使用伪造存储实现忽略传入的 `file_ids` 并返回授权范围外的命中，验证：

- 平面检索只返回授权文件命中。
- 层级检索的 L0/L1/L2 均不能使用越权命中。
- 越权命中不会生成 Trace Snapshot。
- `_enrich_hits()` 不会对越权文件执行数据库回表。
- Elasticsearch 不会收到越权文件 ID。

### 5.5 测试命令

在 `openrag` 目录执行：

```powershell
pytest -q tests/test_scoped_retrieval.py tests/test_soft_delete_retrieval.py
pytest -q tests/test_retrieval_trace.py tests/test_search_api.py tests/test_service_api.py
pytest -q
```

其中 `test_scoped_retrieval.py` 属于当前 `pytest.ini` 默认收集范围，R-01 的核心安全回归测试应优先放入该文件，确保默认测试门禁不会遗漏。

## 6. 发布与运行要求

- 本修复不使用功能开关，随版本直接生效。
- 定向测试和项目默认测试全部通过后才能发布。
- 上线后监控 `authorization.scope_resolution` 失败日志和 Trace。
- 若 `503` 数量异常升高，应修复数据库或权限解析服务，禁止回退为无过滤搜索。
- 任一越权故障注入用例失败都应阻断发布。

## 7. 审核清单

- [x] 权限解析异常统一返回脱敏 `503`。
- [x] `None` 仅保留给已确认管理员的全局搜索。
- [x] 受限命中在 Trace 和数据库回表前执行反向校验。
- [x] 本次不处理 R-02 文件级 ACL。
- [x] 不增加 fail-open 配置开关或权限缓存降级。
- [x] 核心安全测试进入默认 pytest 收集范围。

## 8. 实施记录

实施日期：2026-07-10。

本次实际修改：

- `retrieval_service.py`
  - 新增 `PermissionScopeResolutionError`。
  - 新增不可变 `ResolvedFileScope`，权限范围在每个请求中只解析一次。
  - 权限异常不再返回 `None`；`None` 仅用于已确认管理员的全局搜索。
  - 平面、L0、L1、L2 命中在 Trace Snapshot 和数据库回表前按授权 `file_id` 反向校验。
  - 层级搜索回退到平面检索时复用同一权限快照，不再次查询权限。
- `search_api.py`
  - 工作区 read 校验、路径范围解析和文件权限范围统一进入 fail-closed 边界。
  - 权限范围在 Embedding、Milvus、Layer Store 和 Elasticsearch 工厂初始化前完成。
  - 权限解析异常统一返回脱敏 `503`，Trace 标记为 `authorization.scope_resolution`。
  - 非法路径等正常客户端错误继续保留原有 `400` 语义。
- `service_api.py`
  - Service Token 在完成 token-workspace 授权后显式传递预校验状态。
  - 预校验模式要求非空 `workspace_id`，禁止用于全局搜索。
- 测试
  - 增加权限数据库故障、JWT 语义/层级 503、路径解析故障、外部依赖零初始化、层级回退单次权限解析、越权 L0/L1/L2 命中和 Service Token 503 等回归用例。

本次无数据库迁移、无请求模型变更、无前端改动、无新增依赖，也未实现 R-02 文件级 ACL。

## 9. 测试结果

使用 `E:\project\OpenRag\openrag\venv\Scripts\python.exe`，测试加载的是 `code-optimization` 工作区源码。

### 9.1 R-01 核心与 Service API

```text
pytest -q tests/test_scoped_retrieval.py tests/test_service_api.py
69 passed
```

### 9.2 相关功能回归

```text
pytest -q tests/test_scoped_retrieval.py tests/test_soft_delete_retrieval.py \
  tests/test_search_api.py tests/test_service_api.py tests/test_eval_service.py
91 passed
```

### 9.3 默认测试门禁

```text
pytest -q
183 passed, 4 failed
```

4 个失败均位于 `tests/test_files_api.py`，分别是文件删除、两个文件移动和目录创建测试；失败原因均为测试访问现有 MinIO 时返回 `InvalidAccessKeyId`。失败调用链不经过本次修改的搜索、权限范围或 Service Token 检索代码。

### 9.4 已知基线测试问题

方案原建议执行的 `test_retrieval_trace.py` 有 2 个测试在当前分支和未修改的 `main` 基线中均失败：测试数据库未创建对应 File 记录，既有软删除/有效文件回表过滤会将 FakeVectorStore 命中清空。该问题不是本次 R-01 引入，本次未修改这组无关测试夹具。

### 9.5 静态检查

- Python `py_compile`：通过。
- `git diff --check`：通过。
- 未执行 `git reset`、`git checkout` 或其他风险恢复操作。

## 10. 独立 Agent 审查结果

审查 Agent：`/root/r01_independent_review`。审查过程只读，Agent 未参与首次实现，也未修改文件。

### 10.1 首轮审查

首轮结论：未发现 P0，发现 2 个 P1、2 个 P2 和 1 个 P3。

两个 P1 阻断项：

1. 工作区 read 校验与 `paths` 解析位于统一 503 边界之外，异常可能返回原始 `str(exc)`。
2. 权限范围在依赖工厂初始化之后解析，层级回退还会二次解析权限，不满足“权限异常时外部检索零调用”。

整改：

- 将工作区、路径和文件权限范围纳入 `_execute_search()` 的统一异常边界。
- 引入不可变 `ResolvedFileScope`，在依赖工厂前只解析一次并贯穿整个请求。
- 增加 JWT 端点、路径故障和层级回退测试。

### 10.2 第二轮复审

第二轮结论：原 P1-01、P1-02 均已关闭，未发现新的 P0/P1，当前 Service Token 预校验调用链不存在实际权限绕过。

第二轮发现的 P2 处理情况：

- 非法路径错误被误包装为 503：已修复，HTTP 400 回归测试通过。
- `workspace_access_prevalidated` 是裸布尔安全旁路：已增加非空工作区保护，并测试 Service Token 调用必须显式传入预校验状态；后续如扩展更多调用者，可考虑改为不可伪造的授权上下文。
- `chunk_id` 与 `file_id` 数据库归属未联合校验：保留为后续索引完整性纵深防御项，见下节。

独立审查最终结论：R-01 核心 fail-open 路径已关闭，方案约定的 P0/P1 均已消除，可以进入人工审核和后续发布环境验证。

## 11. 保留风险与后续项

### 11.1 `chunk_id` 与 `file_id` 联合归属校验

当前纵深校验确认检索命中的 `file_id` 属于授权集合，但没有在 Snapshot 前查询数据库确认该 `chunk_id` 实际归属于同一文件。如果索引被污染，出现“授权 `file_id` + 越权 `chunk_id`”的矛盾元数据，仍可能附加错误的 chunk 元数据。

该风险不属于本次权限解析异常 fail-open 的直接路径，也不是本次代码引入。建议在后续索引完整性治理中增加 `(chunk_id, file_id)` 联合批量校验及恶意索引测试。

### 11.2 发布环境测试

默认测试中的 4 个 MinIO 凭据失败需要在有效测试环境中解决并重跑。解决方式应是修复测试隔离或 MinIO 凭据，不能修改 R-01 fail-closed 行为进行规避。
