# OpenRag Trace 与检索质量评测体系执行计划

> **给后续执行代理的要求：** 如果开始按本计划实现代码，需要使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 逐任务执行。本文中的步骤使用 checkbox（`- [ ]`）便于跟踪。

**目标：** 为 OpenRag 接入第一阶段 Trace Store 与检索质量评测能力，覆盖上传、文档处理、检索、融合、重排和 eval run 链路，并基于评测集计算 Precision、Recall、MRR、MAP、NDCG 等指标。

**架构：** 使用 PostgreSQL 保存 trace/eval 索引化数据和 parse 产物引用，MinIO 保存 parse 后完整文件内容的 `canonical.json + canonical.md`，并保存失败、debug 或采样命中的短期大对象 artifact。Trace 上下文通过 `contextvars` 在 API 和 worker 进程内传播；检索质量指标只基于 eval dataset 和 0-3 相关性标注计算，普通线上 query 只保存 trace、top50 快照和 proxy 指标。

**技术栈：** FastAPI、SQLAlchemy、PostgreSQL JSONB、Alembic、MinIO、Milvus、Elasticsearch、OpenRag 现有 parse/chunk/embed/retrieval/rerank 链路、pytest。

---

## 参考文档

- 设计文档：`docs/superpowers/specs/2026-05-26-trace-evaluation-observability-design.md`
- 评测集生成计划：`docs/superpowers/plans/2026-05-27-trace-evaluation-observability.md`
- 当前模型快照：`docs/CURRENT_MODEL_SCHEMA_DDL.md`
- 本地开发指南：`docs/LOCAL_DEV_GUIDE.md`

---

## 边界

第一阶段做：

- PostgreSQL trace/eval 表和 Alembic migration。
- API/Worker 侧 trace context 传播。
- TraceService、EvalService 和指标计算模块。
- Trace/Eval 查询与管理 API。
- parse 后完整文件内容持久化为 MinIO `canonical.json + canonical.md`，PostgreSQL 保存引用。
- 检索、ES 融合、rerank top50 紧凑快照。
- Eval run 批量执行和参数对比。
- trace/snapshot/artifact 7 天清理。

第一阶段不做：

- 完整 OpenTelemetry/Prometheus/Grafana/Jaeger 接入。
- 完整性能大盘。
- L0/L1 指标。
- 权限诊断指标。
- 任务等待时间统计。
- 在 Trace 表中直接保存文件正文、chunk 全文或 embedding 向量。
- 前端完整页面。第一版只要求 API 可用。

---

## 数据模型总览

模型文件建议：

```text
openrag/src/openrag/models/document_parse_artifact.py
openrag/src/openrag/models/trace_eval.py
```

`document_parse_artifact.py` 保存长期 parse 产物引用模型；`trace_eval.py` 保存 trace/eval 相关模型。

新增表：

- `document_parse_artifacts`
- `trace_runs`
- `trace_spans`
- `trace_snapshots`
- `trace_artifacts`
- `eval_datasets`
- `eval_queries`
- `eval_judgments`
- `eval_runs`
- `eval_results`

文档 parse 产物引用保留在 `document_parse_artifacts` 表；完整 parse 内容保存在 MinIO `canonical.json` 和 `canonical.md`。评测相关的长期数据保留在 `eval_*` 表；`trace_runs`、`trace_spans`、`trace_snapshots`、`trace_artifacts` 和短期 MinIO artifact 默认保留 7 天。

parse 产物 MinIO 路径：

```text
parsed/{workspace_id}/{file_id}/{source_doc_hash}/{parser_name}@{parser_version}/canonical.json
parsed/{workspace_id}/{file_id}/{source_doc_hash}/{parser_name}@{parser_version}/canonical.md
```

`canonical.json` 是权威结构化产物，保存 blocks、页码、标题、char span、bbox 和表格结构；`canonical.md` 是完整可读 parse 后文档，用于人工审核、LLM 生成 query 和排查。`canonical.txt` 不作为主产物，需要纯文本时由 `canonical.json` 或 `canonical.md` 派生。

---

## Task 1：引入 Alembic 与 Trace/Eval 数据模型

**文件：**
- 创建：`openrag/alembic.ini`
- 创建：`openrag/alembic/env.py`
- 创建：`openrag/alembic/versions/20260527_0001_trace_eval_tables.py`
- 创建：`openrag/src/openrag/models/document_parse_artifact.py`
- 创建：`openrag/src/openrag/models/trace_eval.py`
- 修改：`openrag/src/openrag/models/__init__.py`
- 修改：`openrag/src/openrag/database.py`
- 测试：`openrag/tests/test_trace_eval_models.py`

- [x] 新增 Alembic 基础配置，`env.py` 使用 `openrag.database.get_database_url()` 和 `Base.metadata`。
- [x] 保留 `Base.metadata.create_all()`，作为本地空库和测试兜底；生产部署以 `alembic upgrade head` 为准。
- [x] 创建 `DocumentParseArtifact` 模型，字段包含 `artifact_id`、`file_id`、`workspace_id`、`source_doc_hash`、`parser_name`、`parser_version`、`canonical_text_hash`、`canonical_json_bucket`、`canonical_json_object_key`、`canonical_json_size_bytes`、`canonical_md_bucket`、`canonical_md_object_key`、`canonical_md_size_bytes`、`block_count`、`page_count`、`block_type_counts`、`status`、`error_message`、`created_at`、`updated_at`。
- [x] 创建 `TraceRun` 模型，字段包含 `trace_id`、`trace_type`、`workspace_id`、`user_id`、`file_id`、`task_id`、`eval_run_id`、`eval_query_id`、`query_hash`、`query_preview`、`status`、`started_at`、`ended_at`、`duration_ms`、`error_stage`、`error_message`、`sampling_reason`、`search_config_snapshot`、`otel_trace_id`、`created_at`。
- [x] 创建 `TraceSpan` 模型，字段包含 `span_id`、`trace_id`、`parent_span_id`、`stage`、`status`、`started_at`、`ended_at`、`duration_ms`、`input_summary`、`output_summary`、`metrics`、`artifact_refs`、`error_message`、`otel_span_id`、`created_at`。
- [x] 创建 `TraceSnapshot` 模型，字段包含 `trace_id`、`span_id`、`stage`、`rank`、`chunk_id`、`file_id`、`score`、`score_parts`、`metadata`、`created_at`。
- [x] 创建 `TraceArtifact` 模型，字段包含 `artifact_id`、`trace_id`、`span_id`、`artifact_type`、`storage_backend`、`bucket`、`object_key`、`content_type`、`size_bytes`、`metadata`、`created_at`、`expires_at`。
- [x] 创建 `EvalDataset`、`EvalQuery`、`EvalJudgment`、`EvalRun`、`EvalResult` 模型，字段与设计文档保持一致。
- [x] `eval_judgments.relevance_grade` 限制为 0-3；`source` 支持 `gold_manual`、`business_import`、`llm_assisted`；默认权重 A/B/C 为 1.0/0.7/0.4。
- [x] 给 `document_parse_artifacts(file_id, source_doc_hash, parser_name, parser_version)` 建唯一索引。
- [x] 给 `trace_id`、`trace_type + created_at`、`workspace_id + created_at`、`file_id + created_at`、`eval_run_id + eval_query_id`、`query_hash + created_at`、`trace_id + stage + rank`、`chunk_id + created_at` 等字段建索引。
- [x] 在 `openrag/src/openrag/models/__init__.py` 导出新增模型，确保 `Base.metadata` 能发现。

验证：

- `cd openrag; alembic upgrade head` 可创建 `document_parse_artifacts` 和 9 张 trace/eval 新表。
- `pytest openrag/tests/test_trace_eval_models.py -v` 通过。
- 空库启动仍可通过 `init_db()` 创建表。
- `docs/CURRENT_MODEL_SCHEMA_DDL.md` 后续实现时同步更新 trace/eval 表结构。

### Task 1 执行进度与结论

- 实现日期：2026-05-27
- 实现代理：Task1 实现 subagent（019e6922-e1d4-7631-ba22-70d8cb3961e1）
- 验证代理：Task1 验证 subagent（当前）
- 验证命令结果：初次验证时裸 `python` / `alembic` 不在 PATH，曾使用 uv 等价命令完成验证；2026-05-27 已在 `C:\Users\lisiqi\.local\bin` 增加 `python.cmd` / `alembic.cmd` 包装器并补跑原始命令，`python -m pytest tests/test_trace_eval_models.py -v` 通过，`6 passed in 0.60s`；`alembic upgrade head` 通过，输出 `Context impl PostgresqlImpl`；此前 `git diff --check` 退出码 0，仅有 CRLF 提示；metadata 检查确认 10 张 Task1 表已注册。
- 结论：VERIFIED。Task1 所列模型、Alembic 配置、迁移、关键约束、默认权重、索引和 `Base.metadata` 注册均已独立验证通过。
- 遗留风险：裸 `python` 与 `alembic` 入口已通过本地包装器修复并完成补验；普通沙箱仍无法直接读取 uv 管理的 `C:\Users\lisiqi\AppData\Roaming\uv\python`，需要通过已授权的包装器/提升权限或后续正式 Python 安装来避免沙箱权限限制。

---

## Task 2：实现 Trace Context 与 TraceService

**文件：**
- 创建：`openrag/src/openrag/tracing/__init__.py`
- 创建：`openrag/src/openrag/tracing/context.py`
- 创建：`openrag/src/openrag/services/trace_service.py`
- 修改：`openrag/src/openrag/api/main.py`
- 修改：`openrag/src/openrag/worker/task_worker.py`
- 测试：`openrag/tests/test_trace_context.py`
- 测试：`openrag/tests/test_trace_service.py`

- [x] 在 `tracing/context.py` 中使用 `contextvars` 定义 `trace_id`、`span_id`、`trace_type`、`workspace_id`、`user_id`、`file_id`、`task_id`、`eval_run_id`、`eval_query_id`、`sampling_reason`。
- [x] 提供 `set_trace_context()`、`get_trace_context()`、`reset_trace_context()`、`push_span()`、`pop_span()`。
- [x] 在 `TraceService` 中实现 `start_run()`、`finish_run()`、`fail_run()`、`start_span()`、`finish_span()`、`fail_span()`、`record_snapshot()`、`record_artifact_ref()`。
- [x] `TraceService` 写入失败只记录 warning，不影响上传、文档处理、检索主链路。
- [x] 在 `openrag/src/openrag/api/main.py` 增加 middleware：读取或生成 `X-OpenRag-Trace-Id`，设置 contextvars，响应头返回 trace id，请求结束后清理 context。
- [x] 普通 `/search` 请求 trace_type 为 `retrieval`；eval run 内部检索 trace_type 为 `eval_retrieval`。
- [x] 在 `TaskWorker` 的任务执行入口创建 `document_processing` trace context，用 `task_id`、`file_id`、`workspace_id` 关联。
- [x] 不把 `trace_id` 层层加入业务函数签名；采集点通过 `TraceService` 读取 context。

验证：

- 并发请求的 trace context 不串线。
- 响应头包含 `X-OpenRag-Trace-Id`。
- trace 写入失败时搜索接口仍返回正常结果。
- `pytest openrag/tests/test_trace_context.py openrag/tests/test_trace_service.py -v` 通过。

### Task 2 执行进度与结论

- 实现日期：2026-05-27
- 实现代理：Task2 实现 subagent（019e6948-c776-7c71-8e25-d2ccb045a2ad）
- 验证代理：Task2 验证 subagent（当前）
- 验证命令结果：初次验证时裸 `python` 不在 PATH，曾使用 uv 等价命令完成验证；2026-05-27 已在 `C:\Users\lisiqi\.local\bin` 增加 `python.cmd` 包装器并补跑原始命令，`python -m pytest tests/test_trace_context.py tests/test_trace_service.py -v` 通过，`7 passed, 5 warnings in 1.74s`；此前 `git diff --check` 退出码 0，仅有 CRLF 提示。
- 结论：VERIFIED。Task2 所列 contextvars 字段、上下文 set/get/reset 与 span 栈、TraceService 方法与 warning-only 写入失败处理、API middleware trace id 传播、`/search` retrieval context、TaskWorker `document_processing` context，以及不向主要业务函数签名层层传递 `trace_id` 均已独立核对通过。`eval_retrieval` 的实际 eval run 调用点属于后续 Task4，本 Task2 已具备通过 `TraceService.start_run(trace_type="eval_retrieval")` 与 context 传入的基础能力。
- 遗留风险：裸 `python` 入口已通过本地包装器修复并完成补验；普通沙箱仍无法直接读取 uv 管理的 `C:\Users\lisiqi\AppData\Roaming\uv\python`，补验在已授权权限下运行；测试存在既有 Pydantic v2 class-based config 与 FastAPI `on_event` deprecation warnings。

---

## Task 3：实现 Eval 指标计算模块

**文件：**
- 创建：`openrag/src/openrag/evaluation/__init__.py`
- 创建：`openrag/src/openrag/evaluation/metrics.py`
- 创建：`openrag/src/openrag/evaluation/schemas.py`
- 测试：`openrag/tests/test_eval_metrics.py`

- [x] 实现 `precision_at_k(results, judgments, k)`。
- [x] 实现 `recall_at_k(results, judgments, k)`。
- [x] 实现 `hit_rate_at_k(results, judgments, k)`。
- [x] 实现 `mrr_at_k(results, judgments, k=50)`。
- [x] 实现 `average_precision_at_k(results, judgments, k=50)`。
- [x] 实现 `map_at_k(query_results, query_judgments, k=50)`。
- [x] 实现 `ndcg_at_k(results, judgments, k)`，gain 使用 0-3 原始相关性等级。
- [x] 实现 `stage_recall_at_k(stage_snapshots, judgments, k=50)`。
- [x] 实现 `rerank_delta(before_metrics, after_metrics)`。
- [x] 二值相关性口径：`relevance_grade >= 2` 视为相关。
- [x] 支持 `source_scope`：`gold_manual`、`business_import`、`llm_assisted`、`weighted_all`。

验证：

- 覆盖空结果、无正例、重复 chunk、不同 k、0-3 gain、ABC 加权等边界。
- `pytest openrag/tests/test_eval_metrics.py -v` 通过。

### Task 3 执行进度与结论

- 实现日期：2026-05-27
- 实现代理：Task3 实现 subagent（019e6960-ab6c-7d62-8063-29a80b147497）
- 验证代理：Task3 验证 subagent（当前）
- 验证命令结果：`python -m pytest tests/test_eval_metrics.py -v` 在 `openrag` 目录通过，`7 passed in 0.03s`；`git diff --check` 退出码 0，仅有既有 CRLF 提示。
- 结论：VERIFIED。Task3 所列 Precision/Recall/HitRate/MRR/AP/MAP/NDCG/stage recall/rerank delta 均已实现；二值相关性使用 `relevance_grade >= 2`；NDCG gain 使用 0-3 原始等级；`source_scope` 支持 `gold_manual`、`business_import`、`llm_assisted`、`weighted_all`，默认权重为 1.0/0.7/0.4；测试覆盖空结果、无正例、重复 chunk、不同 k、0-3 gain 和 ABC 加权边界。
- 遗留风险：裸 `python` 入口已可执行，但当前包装器实际使用 uv 管理的 Python 环境；`git diff --check` 仍提示若干既有文件未来可能被 Git 转为 CRLF。

---

## Task 4：实现 EvalService 与 Eval Run 执行链路

**文件：**
- 创建：`openrag/src/openrag/services/eval_service.py`
- 修改：`openrag/src/openrag/api/search_api.py`
- 测试：`openrag/tests/test_eval_service.py`

- [x] 实现 `create_dataset()`、`list_datasets()`、`get_dataset()`。
- [x] 实现 `add_query()`、`add_judgment()`、`import_dataset()`。
- [x] 实现 `create_eval_run(dataset_id, search_config_snapshot)`，创建 `eval_runs` 记录。
- [x] 实现 `execute_eval_run(eval_run_id)`：逐条 eval query 调用真实检索链路，创建 `eval_retrieval` trace，保存 stage snapshots，计算 query 级指标。
- [x] 实现 `summarize_eval_run(eval_run_id)`，写入 `eval_results.metric_scope=run_summary`。
- [x] 单条 query 失败时记录 query 级 failed，不中断整个 eval run。
- [x] run summary 排除 failed query，并记录 `failed_count`。
- [x] 第一阶段 eval run 可以同步执行；后续再考虑异步任务化。

验证：

- 3 条 query 的小型 dataset 可创建 eval run 并生成 query 级和 run 级结果。
- 单条 query 失败不会导致整个 run 失败。
- `pytest openrag/tests/test_eval_service.py -v` 通过。
### Task 4 执行进度与结论
- 实现日期：2026-05-27
- 实现代理：Task4 实现 subagent（019e6973-176a-7441-9dec-38ab09b7d522）
- 验证代理：Task4 验证 subagent（当前）
- 验证命令结果：`python -m pytest tests/test_eval_service.py -v` 在 `openrag` 目录通过，结果为 `2 passed in 0.62s`；`git diff --check` 退出码 0，仅有既有 CRLF 提示。
- 结论：VERIFIED。Task4 所列 dataset/query/judgment/import、eval run 创建、同步执行、`eval_retrieval` trace、stage snapshots、query 级指标、run summary、失败 query 隔离和 `failed_count` 均已独立核对通过；检索链路支持 fake search 依赖注入测试，并保留通过 `search_api.SearchRequest` 与 `_execute_search()` 的生产默认入口。
- 遗留风险：当前 `python` 入口实际使用 uv 管理的 Python 环境；`git diff --check` 仍提示若干既有文件未来可能被 Git 转为 CRLF。

---

## Task 5：实现 Trace 与 Eval API

**文件：**
- 创建：`openrag/src/openrag/api/traces_api.py`
- 创建：`openrag/src/openrag/api/eval_api.py`
- 修改：`openrag/src/openrag/api/main.py`
- 测试：`openrag/tests/test_traces_api.py`
- 测试：`openrag/tests/test_eval_api.py`

- [x] 新增 `GET /traces`，支持按 `trace_type`、`workspace_id`、`file_id`、`task_id`、`eval_run_id`、`eval_query_id`、`query_hash`、时间范围筛选。
- [x] 新增 `GET /traces/{trace_id}`，返回 run、spans 和基础错误信息。
- [x] 新增 `GET /traces/{trace_id}/snapshots`，支持按 stage 返回 top50 快照。
- [x] 新增 `POST /eval/datasets`、`GET /eval/datasets`、`GET /eval/datasets/{id}`。
- [x] 新增 `POST /eval/queries`、`POST /eval/judgments`、`POST /eval/import`。
- [x] 新增 `POST /eval/runs`、`GET /eval/runs`、`GET /eval/runs/{id}`、`GET /eval/runs/{id}/results`。
- [x] 新增 `GET /eval/runs/{id}/compare?baseline_id=...`，返回两个 eval run 的 summary diff。
- [x] 第一版 API 返回 JSON 即可，不做前端页面。

验证：

- API 鉴权沿用现有 dependency。
- `pytest openrag/tests/test_traces_api.py openrag/tests/test_eval_api.py -v` 通过。
- `GET /openapi.json` 能包含 trace/eval 路由。

### Task 5 执行进度与结论

- 实现日期：2026-05-28。
- 实现代理：`019e6c7b-88a5-74d1-9a4b-0b302709cd84`。
- 验证代理：Task5 验证 subagent。
- 验证命令结果：
  - `cd openrag; python -m pytest tests/test_traces_api.py tests/test_eval_api.py -v`：通过，`5 passed, 5 warnings in 1.77s`。
  - `git diff --check`：退出码 0，仅提示 `docs/eval/foreign_exchange_file_word_summary.md` 与 `openrag/src/openrag/api/main.py` 未来可能被 Git 转为 CRLF。
- 结论：Task5 已完成并通过独立验证。`/traces`、`/traces/{trace_id}`、`/traces/{trace_id}/snapshots`、eval dataset/query/judgment/import/run/results/compare API 均已实现；鉴权与 DB session 沿用现有 FastAPI dependency；`GET /openapi.json` 已覆盖 trace/eval 路由。
- 遗留风险：测试输出仍包含既有 Pydantic v2 与 FastAPI `on_event` deprecation warnings；工作区存在用户侧 `docs/eval/*` 未提交改动，本次验证未触碰、未暂存。

---

## Task 6：检索、ES 融合与 Rerank Trace 采集

**文件：**
- 修改：`openrag/src/openrag/api/search_api.py`
- 修改：`openrag/src/openrag/retrieval/retrieval_service.py`
- 修改：`openrag/src/openrag/retrieval/reranker.py`
- 修改：`openrag/src/openrag/search/es_chunk_store.py`
- 测试：`openrag/tests/test_retrieval_trace.py`

- [x] 在 `retrieval.request` span 记录 `query_hash`、`query_preview`、`workspace_id`、`top_k`、`use_rerank`、`vector_similarity_weight`、`retrieval_strategy`。
- [x] 在 `retrieval.embed_query` span 记录 embedding model、dimension、是否成功。
- [x] 在 `retrieval.chunk_search` 阶段保存 chunk recall top50：`rank`、`chunk_id`、`file_id`、`vector_score`。
- [x] 在 `retrieval.es_fusion` 阶段保存 fusion input/output top50：`vector_score`、`bm25_score`、`fused_score`。
- [x] 在 `retrieval.rerank` 阶段保存 rerank input/output top50：`original_rank`、`fused_score`、`rerank_score`、`rank_delta`。
- [x] 在 `retrieval.response` span 记录 final result count、zero_hit、top50 source file count、fusion overlap、rerank overlap。
- [x] 不保存 query 正文以外的敏感上下文，不保存 chunk 正文。
- [x] L0/L1 关闭时不产生 L0/L1 指标。

验证：

- 一次普通搜索产生 `retrieval` trace 和 top50 snapshots。
- rerank on/off 都有可解释快照。
- ES 不可用时 trace 记录 skip reason，但搜索正常降级。
- `pytest openrag/tests/test_retrieval_trace.py -v` 通过。

### Task 6 执行进度与结论

- 实现日期：2026-05-28。
- 实现代理：`019e6c84-cce2-76c3-a48c-525267c8beda`。
- 验证代理：Task6 验证 subagent。
- 验证命令结果：
  - `cd openrag; python -m pytest tests/test_retrieval_trace.py -v`：通过，`2 passed, 5 warnings in 1.43s`。
  - `cd openrag; python -m pytest tests/test_reranker.py -v`：通过，`17 passed in 0.56s`。
  - trace commit 失败模拟脚本：通过，搜索仍返回 `trace_commit_failure_search_total=1`。
  - `git diff --check`：退出码 0，仅提示既有 CRLF warning。
- 结论：Task6 已完成并通过独立验证。普通搜索可生成 `retrieval` trace、request/embed/chunk_search/es_fusion/rerank/response spans 与 top50 snapshots；ES 不可用时记录 `skip_reason` 并正常降级；快照不保存 chunk 正文，ES 分数查询使用 `_source=False`；L0/L1 关闭时未产生 L0/L1 指标；trace commit 失败不会阻断搜索主链路。
- 遗留风险：测试输出仍包含既有 Pydantic v2 与 FastAPI `on_event` deprecation warnings；工作区存在用户侧 `docs/eval/*` 未提交/未跟踪改动，本次验证未触碰、未暂存；`git diff --check` 仍有 CRLF 提示。

---

## Task 7：上传与文档处理 Trace 采集

**文件：**
- 修改：`openrag/src/openrag/api/files_api.py`
- 修改：`openrag/src/openrag/services/file_ingest.py`
- 创建：`openrag/src/openrag/services/parse_artifact_service.py`
- 修改：`openrag/src/openrag/worker/task_worker.py`
- 修改：`openrag/src/openrag/processors/document_processor.py`
- 测试：`openrag/tests/test_document_processing_trace.py`
- 测试：`openrag/tests/test_parse_artifact_service.py`

- [ ] 上传链路记录 `upload.validate`、`upload.store_minio`、`upload.create_records` span。
- [ ] 文档处理链路记录 `worker.download_file`、`parse.document`、`parsed_artifacts.persist`、`chunk.build`、`embedding.chunks`、`vector.milvus_insert`、`fulltext.es_index`、`storage.save_chunks`、`metadata.persist_chunks` span。
- [ ] `parse.document.duration_ms` 只统计 `parser.parse()` 开始到结束，不包含任务等待或 worker 下载时间。
- [ ] `parse_artifact_service.py` 将 parse 后完整内容写入 MinIO：
  - `canonical.json`：权威结构化产物，包含 blocks、页码、标题、char span、bbox、表格结构和 block_type。
  - `canonical.md`：完整可读 parse 后文档，保留标题、段落、列表、表格和页码标记。
- [ ] `parsed_artifacts.persist` span 记录 `canonical_json_object_key`、`canonical_md_object_key`、`canonical_text_hash`、`block_count`、`page_count`、`size_bytes` 和写入状态；不把正文写入 span。
- [ ] 在 `document_parse_artifacts` 中 upsert parse 产物引用，唯一键为 `file_id + source_doc_hash + parser_name + parser_version`。
- [ ] `chunk.build` 记录 `chunk_method`、`chunk_size`、`overlap`、`min_chunk_tokens`、`chunk_count`、token min/max/mean、短 chunk 数、空 chunk 数。
- [ ] `embedding.chunks` 记录 embedding model、dimension、batch_size、batch_count、chunk_count、成功/失败数。
- [ ] `vector.milvus_insert` 记录 insert_count、collection、失败原因。
- [ ] `fulltext.es_index` 记录 index_name、doc_count、upsert_count、失败原因。
- [ ] `metadata.persist_chunks` 记录 `document_chunks` 写入数，用于和 chunk_count、Milvus insert_count、ES doc_count 做一致性检查。
- [ ] 不记录任务等待时间、父目录传播指标、L0/L1 指标或 embedding 向量。
- [ ] 完整 parse blocks 不写入短期 trace artifact；它们作为长期 `canonical.json` 保存。

验证：

- 上传一个文件后能通过 trace 查询看到 upload trace。
- 文档处理完成后能看到 parse/chunk/embed/vector/es/storage 摘要。
- 文档处理完成后，MinIO 中存在 `canonical.json` 和 `canonical.md`，数据库中存在对应 `document_parse_artifacts` 记录。
- parser 失败时 run/span 标记 failed，主错误信息可查询。
- `pytest openrag/tests/test_document_processing_trace.py openrag/tests/test_parse_artifact_service.py -v` 通过。

---

## Task 8：评测集生成与 50 条 Mini Eval 联动

**文件：**
- 参考：`docs/superpowers/plans/2026-05-27-trace-evaluation-observability.md`
- 创建：`docs/eval/corpus_manifest.2026-05-27.json`
- 创建：`docs/eval/canonical_docs.2026-05-27.jsonl`
- 创建：`docs/eval/eval_dataset_50.reviewed.2026-05-27.jsonl`
- 创建：`docs/eval/eval_dataset_500.llm_assisted.2026-05-27.jsonl`

- [ ] 按评测集生成计划从 `E:\外汇文件` 建立 392 文件 corpus manifest。
- [ ] 先生成 50 条 reviewed query，并将 source document evidence span 固化为评测真值。
- [ ] 将 evidence span 映射到 A/B/C 三组分块参数下的 chunk。
- [ ] 使用 `EvalService` 对 A/B/C 分别执行 eval run。
- [ ] 如果 A/B/C 三组指标都不理想，先进行召回、rerank、parser、mapping 诊断，不扩展到 500 条。
- [ ] 参数与流程稳定后再生成 500 条 `llm_assisted` 评测集。

验证：

- 50 条 mini eval 可以跑完并输出 query type breakdown。
- A/B/C eval run 可通过 `/eval/runs/{id}/compare` 对比。
- 500 条生成前必须有 50 条 mini eval 的诊断结果。

---

## Task 9：Trace 数据清理与部署文档

**文件：**
- 创建：`openrag/src/openrag/services/trace_retention.py`
- 修改：`openrag/src/openrag/scheduler.py`
- 修改：`docker/docker-compose.dev.yml`
- 修改：`docker/docker-compose.worker.yml`
- 修改：`docker/docker-compose.prod.yml`
- 修改：`docs/LOCAL_DEV_GUIDE.md`
- 测试：`openrag/tests/test_trace_retention.py`

- [ ] 实现 `cleanup_expired_traces(retention_days=7)`，按 `created_at` 清理 `trace_snapshots`、`trace_spans`、`trace_runs`。
- [ ] 实现 `cleanup_expired_artifacts()`，按 `expires_at` 删除 `trace_artifacts` 记录和 MinIO 对象。
- [ ] MinIO artifact 删除失败时记录 warning，不中断数据库清理。
- [ ] `document_parse_artifacts` 和 MinIO `canonical.json` / `canonical.md` 不参与 7 天 trace 清理；它们的生命周期跟随源文件、source hash 和 parser 版本。
- [ ] 在 scheduler 或独立维护入口中注册每日清理任务。
- [ ] 在 docker compose 文档中说明生产环境启动 API/Worker 前执行 `alembic upgrade head`。
- [ ] 在本地开发指南中说明 `create_all()` 与 Alembic 的关系。

验证：

- 构造 8 天前 trace 数据后清理成功。
- `eval_*` 数据不被清理。
- `document_parse_artifacts` 和 parse 产物不被 trace retention 清理。
- `pytest openrag/tests/test_trace_retention.py -v` 通过。

---

## Task 10：全链路回归与验收

**文件：**
- 测试：`openrag/tests/test_trace_eval_integration.py`
- 参考：`docs/superpowers/specs/2026-05-26-trace-evaluation-observability-design.md`

- [ ] 跑新增 trace/eval 单元测试。
- [ ] 跑搜索相关回归测试：`test_search_api.py`、`test_retrieval_service.py`、`test_reranker.py`。
- [ ] 跑文档处理相关回归测试：`test_integration_document_processing.py`、`test_chunk_engine.py`、`test_parser_integration.py`。
- [ ] 手动或自动执行一次 eval run，确认 `trace_runs`、`trace_spans`、`trace_snapshots`、`eval_results` 都有数据。
- [ ] 验证 trace 写入失败不影响主链路。
- [ ] 验证 L0/L1 关闭时不产生 L0/L1 指标。
- [ ] 验证普通线上 query 不计算 Precision/NDCG，只记录 proxy 指标。

建议命令：

```powershell
cd openrag
python -m pytest tests/test_eval_metrics.py tests/test_trace_context.py tests/test_trace_service.py -v
python -m pytest tests/test_search_api.py tests/test_retrieval_service.py tests/test_reranker.py -v
python -m pytest tests/test_integration_document_processing.py tests/test_chunk_engine.py tests/test_parser_integration.py -v
```

验收标准：

- 可以保存一次检索的 chunk recall、ES fusion、rerank top50 快照。
- 可以导入 0-3 相关性标注。
- 可以基于评测集执行 eval run。
- 可以计算 `Precision@K`、`Recall@K`、`HitRate@K`、`MRR@50`、`MAP@50`、`NDCG@K`。
- 可以比较两个 eval run 的指标差异。
- 可以保存 parse 后完整文件内容为 MinIO `canonical.json` 和 `canonical.md`，并通过 `document_parse_artifacts` 反查。
- trace 相关数据和 artifact 支持 7 天清理。
- parse 产物不参与 7 天 trace 清理。
- 生产环境可以通过 Alembic 创建或升级 trace/eval schema。
- Trace context 通过 `contextvars` 传播，不需要在主要业务函数签名中层层增加 `trace_id`。
