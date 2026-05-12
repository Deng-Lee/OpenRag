# Elasticsearch 全文索引与向量检索融合设计

**日期**：2026-04-13  
**状态**：已定稿（待实现计划；2026-04-13 修订：按 workspace 拆索引且索引名用 **slug**）  
**范围**：OpenRag 主路径（`openrag` 包）在 L2 chunk 粒度增加 ES 全文索引，与现有 Milvus L0/L1/L2 上下文检索融合；删除与重处理时同步清理或覆盖 ES。

---

## 1. 背景与目标

- 当前上下文检索在 `RetrievalService` 中完成：Milvus L0/L1 粗筛候选文件，再在候选文件上对 L2 chunk 做向量检索，并对 L0/L1/L2 分数分别 min-max 归一化后按固定权重 0.2 / 0.3 / 0.5 融合排序。
- 目标：在同一 **L2 chunk** 粒度维护 **Elasticsearch 全文索引**，查询时结合 **向量融合分** 与 **全文相关性分**（归一化后加权），并可通过配置调节向量比重。
- 文档解析与索引构建本身为 **异步流水线**；在此前提下采用 **写入路径同步双写 Milvus + ES（方案 1）** 可接受，ES 短暂失败通过重试/降级策略处理（见第 6 节）。

---

## 2. 数据模型与索引形态

### 2.1 粒度与标识

- **一条 ES 文档 = 一个 L2 chunk**。
- 文档 `_id` **等于业务 `chunk_id`**，与 Milvus chunk 集合中的主键/标量 `chunk_id` 一致，便于 upsert 与按文档删除。

### 2.2 建议字段

| 字段 | 用途 |
|------|------|
| `chunk_id` | keyword，与 `_id` 一致（冗余便于查询脚本） |
| `file_id` | keyword/long，过滤与删除 |
| `workspace_id` | keyword/long，数据库主键（冗余，便于与 MySQL 对齐） |
| `workspace_slug` | keyword，项目空间 **slug**（与索引路由一致，便于运维排查） |
| `content` 或等价 | 参与 `multi_match` 的全文（以实际参与检索的文本为准，可与 `text_preview` 策略在实现中择一或组合） |
| 可选 | `chunk_index`、`uri` 等仅展示或调试，不参与核心权限逻辑 |

### 2.3 索引布局（按项目空间 / workspace 物理拆分，**以 slug 命名**）

- **强制要求**：不同 **项目空间（workspace）** 使用 **不同的 Elasticsearch 索引**（物理隔离），不得依赖「单索引 + 仅靠 filter」作为唯一隔离手段。
- **命名约定**：`openrag_ws_{slug_segment}_chunks`，其中 `slug_segment` 来自数据库 **`workspaces.slug`（项目空间名 / slug）**，**不得**使用 `workspaces.id` 数字主键作为索引名片段。
- **索引名片段规范化**（实现层单一函数，全库复用）：  
  - 将 slug **转小写**；  
  - 将不在 `[a-z0-9-]` 范围内的字符替换为 `-`，合并连续 `-`，并去掉首尾 `-`；  
  - 结果须匹配 Elasticsearch 索引名约束（长度上限 255 字节，若超长则 **安全截断** 并固定后缀哈希可选，避免碰撞）；  
  - 若规范化后为空（极端脏数据），**回退**为 `id{workspace_id}` 仅作逃生舱，并打 **error 级日志**（正常环境不应出现）。  
  说明：slug 在业务上应 **唯一**（与现有 `Workspace.slug` 约束一致）；索引名与 slug 一一对应，便于运维按「空间名」识别索引。
- **建索引时机**：某 workspace **首次** 需要写入 chunk 全文前，根据当前 slug 计算索引名，调用 `indices.create`（若不存在）并挂上统一 **mapping**（可由 index template 或代码内联 mapping 二选一，实现计划定稿）。
- **单工作区检索**：检索请求若带 `workspace_id`，服务端 **查表** 得到对应 `slug`，换算索引名后 **只访问该索引**。
- **跨工作区检索**（例如未传 `workspace_id`、但权限层给出可访问 `file_id` 列表）：由 `file_id` → `workspace_id` → **`slug`（去重）** 解析出 **若干索引名**，仅对这些索引发起 ES `multi-index` 查询（如 `openrag_ws_acme_chunks,openrag_ws_demo_chunks/_search`），查询体内继续用 `terms` filter 限制 `file_id`，避免跨空间泄漏。
- **slug 变更**：若管理员修改某空间的 `slug`，旧索引名与新索引名 **不一致**；本 spec **不要求**产品内自动双写迁移。运维可选：reindex 至新索引后删旧索引，或由实现计划在「禁止已存在数据的空间改 slug」等产品规则上收口。

### 2.4 权限

- 与现有 Milvus 检索一致：候选集合由服务端解析的 **可访问 `file_id` 集合**（或表达式）约束。
- ES 侧：**索引名已限定 workspace**；查询体内仍须带 **`file_id` 的 filter**（例如 `terms`），以处理同一 workspace 内无权文件边界情况（与 Milvus 表达式/内存过滤语义对齐）。

---

## 3. 写入路径（ingest / 重处理）

- 在 chunk 已成功持久化且 **Milvus chunk 向量 upsert 成功** 的同一异步任务阶段，根据文件所属 workspace **查表得到 `slug`**，按 2.3 节规范化后 **解析目标索引名**，确保索引存在后，对同一 `chunk_id` **写入或覆盖** ES 文档（index API 或 bulk）；文档中写入 `workspace_id` 与 `workspace_slug`（规范化后）冗余字段。
- **重处理**：同一 `chunk_id` 再次生成时，ES 与 Milvus 均为 **覆盖写**，不产生重复文档。
- **失败策略**：实现阶段明确——对异步任务建议 **重试队列或任务级重试**；若单次请求失败，不视为整文件成功条件的一部分（与现有向量写入成功判定对齐，细节在实现计划中落地）。

---

## 4. 查询与分数融合

### 4.1 检索流程（上下文模式）

1. 保持现有 **L0 → L1 → L2（Milvus）** 流程与候选 `file_id` 约束不变。
2. 得到 L2 **chunk 候选列表**（含 Milvus `score` 及 file/chunk 元数据）。
3. 使用用户 **同一原始 query** 对 ES 发起全文检索：
   - **索引路由**：按 2.3 节选择 **一个或多个** `openrag_ws_*_chunks` 索引（由候选 `file_id` 反查得到的 **slug 集合** 换算）；与权限内的 workspace 边界一致。
   - **候选范围**：默认在 **L0/L1 约束下的候选 `file_id` 集合** 内检索（与第 4.2 节一致），避免全文把全库拉入。
   - 对 **当前 Milvus 返回的 chunk_id 集合** 取 ES 分数；若某 chunk 在 ES 中无命中或分数缺失，**全文分记为 0**（参与 min-max 时在实现中保证数值稳定，见 4.3）。

### 4.2 纯全文权重与候选范围

- 配置 `vector_similarity_weight = 0` 时，排序主要由 **归一化全文分** 决定，但 **仍限制在 L0/L1 筛出的候选 `file_id` 集合** 内做 ES 检索与排序，除非产品后续明确放宽（本 spec 不包含全库全文检索）。

### 4.3 归一化与融合公式

- 记 **向量侧融合分**（现有逻辑）：对 L0/L1/L2 分别 min-max 后  
  `S_v = 0.2 * norm_l0 + 0.3 * norm_l1 + 0.5 * norm_l2`（与当前代码权重一致；若实现中权重提取为配置，以代码为准，本 spec 要求 **默认行为与现网一致**）。
- 对当前候选 chunk 集合上的 ES `_score` 做 **min-max** 得到 `S_t`；若所有 ES 分为 0 或集合为空，`S_t` 对排序影响为中性（例如全为同一常数，等价于不按全文区分）。
- 设 `w = vector_similarity_weight`，取值区间 **[0, 1]**，默认 **1**。
- **最终分**：`S_final = w * S_v + (1 - w) * S_t`。
- 当 `w = 1` 时与当前仅向量融合行为一致。

### 4.4 非上下文（平面）检索

- 若存在仅 L2 Milvus、无 L0/L1 的路径：对该路径上的 chunk 候选同样执行 ES 分数拉取与上述融合；若无 L0/L1，则 `norm_l0`、`norm_l1` 按实现取 0 或与现网平面路径行为一致（实现计划中单开任务核对 `_search_flat` 行为）。

---

## 5. 配置

- **名称**：`vector_similarity_weight`。
- **语义**：向量融合分 `S_v` 在最终分中的权重；全文分权重为 `1 - vector_similarity_weight`。
- **范围**：`[0, 1]`；越接近 1 越偏向量，越接近 0 越偏全文（仍在 L0/L1 候选 file 约束下，见 4.2）。
- **暴露**：优先支持 **检索 API 请求体** 可选字段（便于按次调参）；可与全局/环境默认值合并（请求值覆盖默认）。

---

## 6. 删除、降级与可观测性

### 6.1 删除

- 在现有 `delete_milvus_vectors_for_file` 及文件清理/重处理路径中，根据待删文件 **解析所属 workspace 的 `slug`**（与写入时同一套规范化规则），定位索引 `openrag_ws_{slug_segment}_chunks`，对该索引执行 **按 `file_id` 的 `delete_by_query`**（或等价 bulk 删除）。
- 与「删除向量同时删除全文」要求一致；目录删除等递归删除需对每个 `file_id` 解析其所属 workspace **slug** 后分别清理对应索引。
- **工作区级删除**（若未来支持删除整个 workspace）：删除该 workspace 下所有文件后，可额外 `indices.delete` 对应 **`openrag_ws_{slug_segment}_chunks`**（可选优化，本阶段可不实现，但实现层应保留扩展点）。

### 6.2 ES 不可用

- **默认推荐**：降级为 **仅向量融合**（`S_final` 退化为 `S_v` 或等价于 `w` 临时视为 1），并记录 **warning 级日志**；可选指标计数供运维。
- 若产品要求「ES 必选」可切换为硬错误；本 spec 默认 **降级**。

---

## 7. 测试建议

- 单元：\(w=0, 0.5, 1\) 边界；min-max 单点、全零 ES 分；无 ES 命中时的数值稳定。
- 集成：双写后检索融合分顺序；删除文件后 ES 与 Milvus 均无残留；重处理覆盖同一 `chunk_id`。

---

## 8. 范围外

- 不在本 spec 内规定 ES 集群拓扑、**每个 workspace 索引内部** 的分片数、ILM；仅约定 **每 workspace 独立索引名** 与 mapping 策略。
- 不在本 spec 内替换 Milvus 为 ES kNN；全文仅作为 **BM25（或等价）** 分支。

---

## 9. 自检记录

- 无 TBD 占位；权重默认值与降级策略已写明。
- 「候选 file 约束」与「chunk 级 ES 文档」一致；**全文索引按 workspace 物理拆分，索引名以 slug 为准**；删除路径使用 **slug（经 workspace_id 查表）** 定位索引 + `file_id` / `chunk_id` 覆盖写策略一致。
- 跨 workspace 检索的索引列表由 **权限内的 file → workspace → slug（去重）推导**，不扫描全集群索引。
- 实现计划将拆分：`slug` 规范化工具、ES 客户端与 mapping/template、按 workspace 的 `ensure_index`、写入钩子、检索路由与 multi-index 查询、删除、docker-compose 可选依赖、测试。

---

## 10. 修订记录

- **2026-04-13**：将全文索引从「单索引 + filter」改为 **每项目空间（workspace）独立索引**；初版索引名片段为 `workspace_id`。
- **2026-04-13（第二次）**：索引名片段改为 **`workspaces.slug` 规范化后的 `slug_segment`**，形如 `openrag_ws_{slug_segment}_chunks`；补充规范化规则、slug 变更的运维说明、文档字段 `workspace_slug`；跨索引路由改为基于 **slug 集合**。
