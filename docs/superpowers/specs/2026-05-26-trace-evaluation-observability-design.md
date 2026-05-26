# OpenRag Trace 与检索质量评测体系设计

日期：2026-05-26

## 1. 背景

OpenRag 当前已经具备文件上传、异步文档处理、文本切块、向量入库、全文索引、检索召回、ES 融合与重排等核心链路。现有可观测能力主要来自任务状态、文件状态、结构化日志以及已持久化的 `files`、`tasks`、`document_chunks`、MinIO 层级产物、Milvus 向量数据和 Elasticsearch 全文索引。

下一阶段需要接入 trace 体系，用于观测 OpenRag 的运行状态与检索质量。第一阶段目标不是建设完整性能大盘，也不是接入完整 OpenTelemetry/Prometheus 体系，而是优先支持：

- 追踪上传、解析、切块、入库、检索、召回、融合、重排的关键步骤。
- 保存召回、融合、重排阶段的 top50 紧凑快照。
- 基于评测集计算 Precision、Recall、HitRate、MRR、MAP、NDCG 等检索质量指标。
- 支持通过评测集反复调参，在上传大量文档或扩大知识库规模前确认参数效果。

## 2. 目标与非目标

### 2.1 目标

第一阶段采用“PostgreSQL 索引 + MinIO Artifact”的内部 Trace Store 方案：

- PostgreSQL 保存可查询、可聚合、可计算指标的数据。
- MinIO 仅在失败、debug 或采样命中时保存较大的 artifact。
- 默认不重复保存正文、文件内容、chunk 全文、L0/L1 内容或 embedding 向量。
- 检索质量评测以评测集为主入口，通过批量 eval run 计算真实质量指标。
- 普通线上检索保存 trace、top50 快照和 proxy 指标，但不计算 Precision/NDCG 等需要标注的真实指标。
- `trace_runs`、`trace_spans`、`trace_snapshots` 和大对象 artifact 统一保留 7 天。

### 2.2 非目标

第一阶段暂不做：

- 完整 OpenTelemetry、Prometheus、Grafana 或 Jaeger 接入。
- 完整性能大盘。
- 任务排队等待时间统计。
- 权限过滤诊断指标。
- L0/L1 层级召回相关指标。
- 父目录层级传播指标。
- 长期保存线上 trace 数据。

## 3. 核心原则

### 3.1 能反查的数据不重复保存

Trace 不做第二套业务数据仓库。凡是能从现有数据反查的内容，不在 trace 中重复保存：

- 文件元数据从 `files` 反查。
- 任务状态从 `tasks` 反查。
- chunk 元数据从 `document_chunks` 反查。
- chunk 正文从 MinIO L2 或本地层级存储反查。
- 文件内容从 MinIO 源文件反查。
- L0/L1/L2 层级内容从 `files.l0_path/l1_path/l2_path` 指向的 MinIO 对象反查。

Trace 只保存当时不可稳定反推的内容：

- 当时使用的检索参数、调参配置、模型配置摘要。
- 当时的召回、融合、重排 top50 排序和分数。
- 当时的质量指标结果。
- 当时的错误阶段和错误摘要。

### 3.2 检索质量指标必须基于 ground truth

Precision、Recall、NDCG、MRR、MAP 等真实质量指标必须依赖预先维护的评测集和相关性标注。普通线上 query 如果不在评测集中，不能计算这些真实指标，只能计算 proxy 指标。

### 3.3 评测集驱动调参

第一阶段调参流程为：

1. 维护评测数据集、评测 query 和 0-3 相关性标注。
2. 使用评测集批量执行真实检索链路。
3. 保存每条评测 query 的 trace、top50 快照和指标。
4. 汇总 eval run 指标。
5. 对比不同检索参数、ES 融合权重、rerank 开关或模型版本。
6. 参数稳定后再上传大量文档或扩大知识库规模。

## 4. 方案选择

### 4.1 已确认方案

采用方案二：PostgreSQL 索引 + MinIO Artifact。

PostgreSQL 存储：

- trace run、span、snapshot。
- 评测集、评测 query、相关性标注。
- eval run 和 eval result。
- 可直接聚合的质量指标与 proxy 指标。

MinIO 存储：

- 失败时的完整解析块快照。
- debug 模式下的完整 `DocumentBlock` 或 OCR/layout/table 原始结构。
- LLM 辅助标注解释等较大对象。

第一阶段默认不写大对象 artifact。

### 4.2 不选其他方案的原因

不采用 PostgreSQL JSONB-only：

- 初期实现简单，但大对象和大量快照容易撑大主库。
- 后续 artifact 生命周期和清理不够清晰。

不采用 OpenTelemetry-first：

- 标准 span 不适合承载 RAG 检索 top50、融合分、rerank delta 这类领域中间数据。
- 第一阶段重点是检索质量评测，不应被标准观测栈牵引。

## 5. 数据模型设计

### 5.1 trace_runs

表示一次完整链路。第一阶段主要包括：

- `upload`
- `document_processing`
- `retrieval`
- `eval_retrieval`

关键字段：

- `id`
- `trace_id`
- `trace_type`
- `workspace_id`
- `user_id`
- `file_id`
- `task_id`
- `eval_run_id`
- `eval_query_id`
- `query_hash`
- `query_preview`
- `status`
- `started_at`
- `ended_at`
- `duration_ms`
- `error_stage`
- `error_message`
- `sampling_reason`
- `search_config_snapshot`
- `otel_trace_id`
- `created_at`

索引建议：

- `(trace_id)` 唯一索引
- `(trace_type, created_at)`
- `(workspace_id, created_at)`
- `(file_id, created_at)`
- `(task_id)`
- `(eval_run_id, eval_query_id)`
- `(query_hash, created_at)`

### 5.2 trace_spans

表示链路中的阶段步骤。

关键字段：

- `id`
- `span_id`
- `trace_id`
- `parent_span_id`
- `stage`
- `status`
- `started_at`
- `ended_at`
- `duration_ms`
- `input_summary`
- `output_summary`
- `metrics`
- `artifact_refs`
- `error_message`
- `otel_span_id`
- `created_at`

`input_summary`、`output_summary` 和 `metrics` 使用 JSONB。

索引建议：

- `(trace_id, started_at)`
- `(trace_id, stage)`
- `(stage, created_at)`

### 5.3 trace_snapshots

保存检索、融合、重排阶段 top50 紧凑快照。该表独立于 `trace_spans.metrics`，便于按 `chunk_id`、`stage`、`rank`、`score` 查询和诊断。

关键字段：

- `id`
- `trace_id`
- `span_id`
- `stage`
- `rank`
- `chunk_id`
- `file_id`
- `score`
- `score_parts`
- `metadata`
- `created_at`

`stage` 第一阶段包括：

- `chunk_recall`
- `es_fusion_input`
- `es_fusion_output`
- `rerank_input`
- `rerank_output`

`score_parts` 示例：

```json
{
  "vector_score": 0.82,
  "bm25_score": 0.61,
  "fused_score": 0.76,
  "rerank_score": 0.91
}
```

索引建议：

- `(trace_id, stage, rank)`
- `(chunk_id, created_at)`
- `(file_id, created_at)`

### 5.4 trace_artifacts

保存大对象引用，不保存正文内容本身。

关键字段：

- `id`
- `artifact_id`
- `trace_id`
- `span_id`
- `artifact_type`
- `storage_backend`
- `bucket`
- `object_key`
- `content_type`
- `size_bytes`
- `metadata`
- `created_at`
- `expires_at`

Artifact 默认只在失败、debug 开关、采样命中或指定 workspace/query 时保存。

### 5.5 eval_datasets

表示一套评测集。

关键字段：

- `id`
- `name`
- `description`
- `workspace_id`
- `status`
- `metadata`
- `created_by`
- `created_at`
- `updated_at`

### 5.6 eval_queries

表示评测 query。

关键字段：

- `id`
- `dataset_id`
- `query_text`
- `query_hash`
- `query_type`
- `expected_answer`
- `metadata`
- `created_at`
- `updated_at`

### 5.7 eval_judgments

表示 query 与 chunk/file 的相关性标注。

ABC 三路统一使用 0-3 相关性等级：

- `0`：不相关
- `1`：弱相关
- `2`：相关
- `3`：强相关，可直接支撑答案

关键字段：

- `id`
- `dataset_id`
- `eval_query_id`
- `chunk_id`
- `file_id`
- `relevance_grade`
- `source`
- `weight`
- `judge_user_id`
- `judge_model`
- `judge_reason_ref`
- `metadata`
- `created_at`
- `updated_at`

`source` 包括：

- `gold_manual`：A 人工金标
- `business_import`：B 业务样本导入
- `llm_assisted`：C LLM 辅助标注

默认权重：

- A：`1.0`
- B：`0.7`
- C：`0.4`

权重应配置化，不在指标计算逻辑中写死。

### 5.8 eval_runs

表示一次评测执行。

关键字段：

- `id`
- `dataset_id`
- `name`
- `status`
- `search_config_snapshot`
- `code_version`
- `index_version`
- `started_at`
- `ended_at`
- `created_by`
- `metadata`
- `created_at`

### 5.9 eval_results

保存评测结果。可以按 eval run 汇总，也可以按 eval query 保存明细。

关键字段：

- `id`
- `eval_run_id`
- `eval_query_id`
- `trace_id`
- `metric_scope`
- `source_scope`
- `metrics`
- `created_at`

`metric_scope` 示例：

- `query`
- `run_summary`
- `rerank_delta`
- `stage_recall`

`source_scope` 示例：

- `gold_manual`
- `business_import`
- `llm_assisted`
- `weighted_all`

## 6. 采集点设计

### 6.1 上传链路

采集位置：

- `openrag/src/openrag/api/files_api.py`
- `openrag/src/openrag/services/file_ingest.py`

Span：

- `upload.validate`
- `upload.store_minio`
- `upload.create_records`

记录内容：

- 文件大小、MIME、parser_type 请求值、目标路径。
- MinIO bucket、object key、写入状态。
- `file_id`、`task_id`、`task_type`。
- 必要的粗粒度耗时和失败原因。

不保存文件内容。

### 6.2 文档处理链路

采集位置：

- `openrag/src/openrag/worker/task_worker.py`
- `openrag/src/openrag/processors/document_processor.py`

Span：

- `worker.download_file`
- `parse.document`
- `chunk.build`
- `embedding.chunks`
- `vector.milvus_insert`
- `fulltext.es_index`
- `storage.save_chunks`
- `metadata.persist_chunks`

记录内容：

- `parse.document` 的耗时只包括 `parser.parse()` 从开始到结束的执行时间，不包含任务等待时间或 worker 下载时间。
- parser 实际类、parser_type、block_count、page_count、block_type 分布。
- chunk_method、chunk_size、overlap、min_chunk_tokens。
- chunk_count、token min/max/mean、短 chunk 数、空 chunk 数、page/bbox 覆盖率。
- embedding model、dimension、batch_size、batch_count、chunk_count、成功/失败数。
- Milvus insert_count、collection、失败原因。
- ES index_name、doc_count、upsert_count、失败原因。
- `document_chunks` 写入数量，用于和 chunk_count、Milvus insert_count、ES doc_count 做一致性检查。

不采集：

- 任务等待时间。
- 父目录层级传播指标。
- L0/L1 指标。
- 完整 `DocumentBlock` 默认快照。
- embedding 向量。

### 6.3 检索链路

采集位置：

- `openrag/src/openrag/api/search_api.py`
- `openrag/src/openrag/retrieval/retrieval_service.py`
- `openrag/src/openrag/retrieval/reranker.py`

Span：

- `retrieval.request`
- `retrieval.embed_query`
- `retrieval.chunk_search`
- `retrieval.es_fusion`
- `retrieval.rerank`
- `retrieval.response`

记录内容：

- query_hash、query_preview、workspace_id、top_k、strategy、rerank 开关、vector_similarity_weight。
- chunk recall top50：`rank/chunk_id/file_id/vector_score`。
- ES fusion 输入与输出 top50：`rank/chunk_id/file_id/vector_score/bm25_score/fused_score`。
- rerank 输入与输出 top50：`rank/chunk_id/file_id/original_rank/fused_score/rerank_score/rank_delta`。
- 最终返回数量、zero_hit、top50 来源文档数、融合前后 overlap、rerank 前后 overlap。

不采集：

- 权限过滤诊断指标。
- L0/L1 召回指标。
- query 正文以外的敏感上下文。
- chunk 正文。

## 7. 指标设计

### 7.1 真实质量指标

真实质量指标只在 eval run 中计算，依赖 `eval_judgments` 的 0-3 相关性等级。

第一阶段计算：

- `Precision@1/3/5/10/20/50`
- `Recall@1/3/5/10/20/50`
- `HitRate@1/3/5/10/20/50`
- `MRR@50`
- `MAP@50`
- `NDCG@1/3/5/10/20/50`
- `Stage Recall@50`
- `Rerank Delta`

二值相关性口径：

- `relevance_grade >= 2` 视为相关。

NDCG 口径：

- 使用 0-3 原始等级作为 gain。

### 7.2 ABC 双轨口径

采用双轨制：

- A 人工金标作为硬门槛。
- A/B/C 加权综合分用于趋势观察和策略对比。
- A、B、C 分来源指标单独输出，避免 LLM 辅助标注噪声掩盖问题。

### 7.3 Stage Recall

用于观察相关结果在各阶段是否被保留。

第一阶段 stage 包括：

- `chunk_recall`
- `es_fusion_output`
- `rerank_output`

不包括 L0/L1。

### 7.4 Rerank Delta

用于比较 rerank 前后质量变化。

示例指标：

- `NDCG@10_after - NDCG@10_before`
- `Precision@10_after - Precision@10_before`
- `MRR@50_after - MRR@50_before`
- top50 overlap
- rank_delta 分布

### 7.5 无标注 proxy 指标

普通线上检索不计算真实质量指标，只记录 proxy 指标：

- zero-hit
- top score 分布
- top1/top2 margin
- top50 来源文档数
- ES fusion 前后 top50 overlap
- rerank 前后 top50 overlap
- rerank rank change 分布

proxy 指标不能替代 Precision/NDCG，只用于异常观察。

## 8. Eval Run 工作流

### 8.1 创建评测集

用户维护：

- eval dataset
- eval query
- ABC 三路 0-3 相关性标注

标注对象优先使用 `chunk_id`。当业务样本只能定位到文件时，可先保存 `file_id` 级标注，但指标计算时应明确标注粒度，避免和 chunk 级指标混用。

### 8.2 执行评测

用户选择：

- dataset
- search_config
- rerank 开关或模型
- ES 融合权重
- top_k / fetch_k
- 其他调参参数

系统对每个 eval query 执行真实检索链路：

1. 创建 `eval_runs`。
2. 每条 eval query 创建 `trace_runs`，trace_type 为 `eval_retrieval`。
3. 保存 chunk recall、ES fusion、rerank top50 快照。
4. 查询 `eval_judgments`。
5. 计算 query 级指标。
6. 汇总 run 级指标。
7. 保存 `eval_results`。

### 8.3 参数对比

不同参数组合产生不同 eval run。系统支持比较：

- rerank on/off。
- 不同 `vector_similarity_weight`。
- 不同 fetch_k。
- 不同 embedding/rerank 模型。
- 不同 chunk 参数对应的索引版本。

## 9. API 设计

### 9.1 Trace 查询

- `GET /traces`
- `GET /traces/{trace_id}`
- `GET /traces/{trace_id}/snapshots`

支持筛选：

- trace_type
- workspace_id
- file_id
- task_id
- eval_run_id
- eval_query_id
- query_hash
- 时间范围

### 9.2 评测集管理

- `POST /eval/datasets`
- `GET /eval/datasets`
- `GET /eval/datasets/{id}`
- `POST /eval/queries`
- `POST /eval/judgments`
- `POST /eval/import`

`POST /eval/import` 支持导入 A/B/C 三类标注数据，统一转换为 0-3 相关性等级。

### 9.3 评测执行与结果

- `POST /eval/runs`
- `GET /eval/runs`
- `GET /eval/runs/{id}`
- `GET /eval/runs/{id}/results`
- `GET /eval/runs/{id}/compare?baseline_id=...`

第一阶段可先提供 API，前端页面后续逐步补齐。

## 10. 前端查看方式

第一阶段建议提供轻量管理页：

- Trace 详情页：展示一次检索从 chunk recall 到 ES fusion 到 rerank 的 top50 排名变化。
- Eval Run 页：展示 Precision、Recall、HitRate、MRR、MAP、NDCG。
- Compare 页：比较两个 eval run 的指标变化。
- Query 详情页：展示某个 query 的标注相关 chunk、实际召回排名、漏召结果。

如果开发资源有限，第一阶段可以先完成 API 和后端数据结构，前端只做最小可用查询页。

## 11. 数据保留与清理

统一保留 7 天：

- `trace_runs`
- `trace_spans`
- `trace_snapshots`
- `trace_artifacts`
- MinIO 大对象 artifact

长期保留：

- `eval_datasets`
- `eval_queries`
- `eval_judgments`
- `eval_runs`
- `eval_results`

清理任务要求：

- 支持按 `created_at` 清理 trace 相关表。
- 支持按 `expires_at` 清理 artifact。
- MinIO artifact 删除失败时记录告警，不影响数据库清理继续进行。

## 12. 错误处理

Trace 写入不能影响主链路：

- trace 写入失败时记录 warning。
- 检索、上传、文档处理主流程继续执行。
- eval run 中单条 query 失败时记录 query 级失败，eval run 继续处理其他 query。

质量指标计算失败：

- 保存失败原因。
- 对应 eval query 标记为 failed。
- run_summary 排除 failed query，并记录 failed_count。

## 13. 测试计划

### 13.1 单元测试

- trace run/span/snapshot 创建与查询。
- top50 snapshot 截断逻辑。
- query hash 规范化。
- 0-3 相关性等级校验。
- Precision、Recall、HitRate、MRR、MAP、NDCG 计算。
- ABC 分来源和加权综合指标计算。
- Rerank Delta 和 Stage Recall 计算。

### 13.2 集成测试

- 上传文件后生成必要 trace。
- 文档处理后记录 parse/chunk/embedding/vector/es/storage 关键摘要。
- 检索后记录 chunk recall、ES fusion、rerank top50 快照。
- eval run 批量执行并生成 eval_results。
- trace 数据 7 天清理逻辑。

### 13.3 回归测试

- trace 写入失败不影响搜索接口返回。
- trace 写入失败不影响文档处理任务成功。
- L0/L1 关闭时不产生 L0/L1 指标。
- 权限诊断关闭时不产生权限过滤指标。

## 14. 验收标准

第一阶段完成后，应满足：

- 能保存一次检索的 chunk recall、ES fusion、rerank top50 快照。
- 能导入 ABC 三类 0-3 标注。
- 能基于评测集批量执行 eval run。
- 能计算 `Precision@K`、`Recall@K`、`HitRate@K`、`MRR@50`、`MAP@50`、`NDCG@K`。
- 能计算 rerank 前后质量变化。
- 能比较不同参数组合的 eval run。
- trace 相关数据和 artifact 统一 7 天清理。
- 不记录 L0/L1 指标。
- 不做权限诊断。
- 不重复保存文件正文、chunk 正文、L0/L1 内容或 embedding 向量。

## 15. 后续演进

第二阶段可考虑：

- 接入 OpenTelemetry trace 导出。
- 接入 Prometheus metrics 和 Grafana 大盘。
- 增加真实线上 query 的回放评测能力。
- 增加 L0/L1 层级召回指标。
- 增加权限过滤诊断。
- 增加长期质量趋势报表。
- 增加 LLM 辅助标注审核流。
