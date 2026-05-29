# Trace 评测集生成执行计划

> **给后续执行代理的要求：** 如果开始按本计划实现代码，需要使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 逐任务执行。本文中的步骤使用 checkbox（`- [ ]`）便于跟踪。

**目标：** 先构建 50 条经过人工审核的小规模 RAG 评测 query，用它们比较分块参数，再扩展到 500 条 `llm_assisted` 评测集。

**架构：** 第一轮不直接生成 500 条正式评测集，而是先用小样本做参数敏感度验证。每条 approved query 的评测真值绑定到源文档中的 evidence span，`chunk_id` 只作为某个 `index_version` 和分块参数下的派生映射结果。后续更换 `chunk_size`、`overlap`、parser 版本或索引版本时，根据 evidence span 重新映射到新的 chunk。

**技术栈：** OpenRag 现有 parse/chunk/embed/index 流水线、Trace/Eval 设计中的 PostgreSQL eval 表、MinIO 源文件、LLM 辅助生成、人工审核。

---

## 范围

本文只描述“评测集生成 + 分块参数敏感度试验”的执行指导，不实现 Trace Store、Eval API、Alembic 迁移或前端页面。

整体流程分五步：

1. 建立 392 个文件的 corpus manifest，明确文件来源、格式、hash 和可读取位置。
2. 解析 392 个文件，将 parse 后完整文件内容保存为 MinIO `canonical.json + canonical.md`，并形成可复用的规范化文档视图和 evidence span 定位能力。
3. 让 LLM 基于文档、章节、段落或证据片段生成 60-70 条原始候选 query。
4. 人工审核并保留 50 条 approved query，同时补齐源文档 evidence span 锚点。
5. 将 evidence span 映射到 2-3 组分块参数下的 chunk，再运行 mini eval。
6. 参数稳定后，再生成完整 500 条 `llm_assisted` 评测集。

核心数据关系：

```text
eval_query
  -> eval_judgment
  -> evidence_anchor(file_id, evidence_quote, source_position)
  -> chunk_mapping(index_version, chunk_params, chunk_id)
```

其中 `evidence_anchor` 是评测真值，`chunk_mapping` 是为了在某个分块版本下计算 chunk 级 Precision、Recall、NDCG 等指标而生成的派生结果。

---

## Corpus Manifest

本轮评测语料入口已确认为本地目录：

```text
E:\外汇文件
```

当前扫描结果为 392 个文件，格式分布如下：

| 扩展名 | 数量 |
| --- | ---: |
| `.md` | 153 |
| `.docx` | 116 |
| `.doc` | 59 |
| `.pdf` | 56 |
| `.xls` | 6 |
| `.xlsx` | 2 |

执行本计划前仍必须创建 corpus manifest。执行代理不能只根据“392 个文件”这个数字开始处理，必须以 manifest 固化本轮语料清单、hash 和格式分布。

推荐清单路径：

```text
docs/eval/corpus_manifest.2026-05-27.json
```

manifest 至少包含：

```json
{
  "corpus_id": "openrag-eval-corpus-2026-05-27",
  "expected_file_count": 392,
  "source_type": "local_dir",
  "source_root": "E:\\外汇文件",
  "format_summary": {
    "md": 153,
    "docx": 116,
    "doc": 59,
    "pdf": 56,
    "xls": 6,
    "xlsx": 2,
    "txt": 0,
    "html": 0,
    "other": 0
  },
  "files": [
    {
      "corpus_file_id": "corpus_000001",
      "file_name": "example.pdf",
      "file_ext": ".pdf",
      "source_uri": "",
      "local_path": "E:\\外汇文件\\example.pdf",
      "openrag_file_id": null,
      "workspace_id": null,
      "size_bytes": 0,
      "source_doc_hash": "sha256:<computed_sha256_hex>",
      "detected_mime_type": "application/pdf",
      "parser_hint": "ragflow",
      "is_scanned_or_image_pdf": false,
      "include_in_eval": true,
      "exclude_reason": ""
    }
  ]
}
```

来源规则：

- 本轮固定使用 `source_type=local_dir`，`source_root=E:\外汇文件`，执行代理通过 `local_path` 读取文件。
- `source_type=minio`：`source_root` 是 bucket/prefix，执行代理通过 `source_uri` 读取文件。
- `source_type=openrag_db`：文件已经上传进 OpenRag，执行代理通过 `openrag_file_id`、`workspace_id` 和数据库/对象存储记录读取文件。

验证规则：

- `files.length` 必须等于 `expected_file_count=392`。
- 每条文件记录必须有 `source_doc_hash`。hash 用于检测 Task 1 到 Task 7 之间文件是否被替换。
- `format_summary` 必须由实际文件列表统计得出，不能手写猜测。
- 不支持的格式也必须进入 manifest，并用 `include_in_eval=false` 和 `exclude_reason` 标明原因。

---

## LLM Query 生成策略

不能把 392 个文件全文一次性塞给 LLM。候选 query 生成采用“证据单元采样 + 小上下文 prompt”的方式。

### 模型配置

第一阶段推荐使用 OpenAI-compatible Chat API，因为项目已有 `OPENAI_API_KEY`、`OPENAI_BASE_URL` 这类配置入口。执行前必须显式记录：

```json
{
  "generator_provider": "openai_compatible",
  "generator_base_url": "${OPENAI_BASE_URL}",
  "generator_model": "gpt-4o-mini",
  "prompt_version": "eval-query-gen-v1",
  "temperature": 0.2,
  "max_output_tokens": 2048
}
```

如果改用 Claude API、本地模型或内网模型网关，也可以执行，但必须在 generation metadata 中记录 provider、base_url、model、prompt_version 和参数。没有可用 LLM 配置时停止在原始候选生成之前，不使用 mock LLM 生成评测集。

### 采样策略

先从规范化文档视图中抽取 evidence candidate，再让 LLM 生成 query。采样时按以下维度分层：

- 文件格式：PDF、DOCX、XLSX、TXT/HTML、扫描件分别覆盖。
- 文档长度：短文档、中等文档、长文档分别覆盖。
- 文档主题：优先使用文件名、目录路径、标题、章节标题和关键词做主题分组；如果已有 embedding，可用文档/章节向量做聚类辅助。
- 证据类型：事实值、规则条件、流程步骤、表格字段、版本日期、异常/缺失信息分别覆盖。

建议为 60-70 条原始候选准备以下证据输入：

| 候选来源 | 数量 | 输入给 LLM 的上下文 |
| --- | ---: | --- |
| 单文档单证据 | 25-30 | 一个 evidence candidate + 文件名 + 标题/页码/章节 metadata |
| 单文档多证据 | 10-15 | 同一文档内 2-4 个相邻或同主题 evidence candidate |
| 多文档比较 | 10-12 | 2-4 个同主题、不同文档的 evidence candidate |
| 多场景条件提取 | 10-12 | 3-6 个相似场景或相似字段的 evidence candidate |
| 无答案/边界问题 | 5-8 | 一个主题描述 + 相似但不充分的弱证据 |

### Prompt 输入组织

每次 prompt 只输入小上下文，不输入整篇文档。推荐结构：

```json
{
  "task": "generate_eval_query",
  "query_type": "multi_scenario_condition_extract",
  "language": "zh",
  "evidence_candidates": [
    {
      "corpus_file_id": "corpus_000001",
      "file_name": "example.pdf",
      "file_ext": ".pdf",
      "section_heading": "适用范围",
      "page_number": 3,
      "text": "证据候选文本，建议 300-1200 字",
      "source_position": {
        "char_start": 15320,
        "char_end": 15880,
        "block_id": "block_045"
      }
    }
  ],
  "requirements": [
    "生成真实用户会问的问题",
    "输出 query_text、expected_answer、evidence_quote、answer_quote、evidence_role、reason",
    "不要引入 evidence_candidates 之外的知识"
  ]
}
```

LLM 输出必须是结构化 JSON。执行代理需要对输出做 JSON schema 校验，并确认 `evidence_quote` 可以在对应源文档中定位。

不建议用当前 RAG 检索结果作为生成 query 的主输入，因为这会把现有检索策略的偏差带入评测集。RAG 可以作为后处理辅助，用于检查候选 query 是否过于容易或完全无法召回，但不能替代源文档证据采样。

---

## 异常路径与决策门

### 语料漂移

每次执行 Task 2 之后的步骤前，都要重新校验 manifest 中的 `source_doc_hash`。

- 如果源文件 hash 变化：该文件相关 query 和 judgment 标记为 `corpus_drift`，暂停进入 eval。
- 如果文件被删除：先尝试从 `source_uri` 恢复；无法恢复时，将相关 query 标记为 `inactive`，并记录 `inactive_reason=source_missing`。
- 如果只是 OpenRag 内部 `file_id` 变化，但 `source_doc_hash` 不变：允许重新绑定新的 `openrag_file_id`，并保留原 `corpus_file_id`。

### Parser 输出漂移

如果 parser 升级导致 `canonical_text_hash` 变化：

- 相关 evidence anchor 的 `mapping_status` 标为 `needs_review`。
- 复核责任人是评测集维护人，也就是执行 Task 5 人工审核的人；复核后的 `reviewer_id` 和 `reviewed_at` 必须写回。
- 复核内容包括：`evidence_quote` 是否仍存在、`expected_answer` 是否仍成立、源文档位置是否需要更新。

### 三组分块参数都不理想

如果 A/B/C 三组指标都低于可接受线，不进入 500 条扩展，先执行诊断：

1. 查看 `Stage Recall@50`。如果召回阶段已经漏掉 `grade=3` 证据，优先调整召回参数，例如 `fetch_k`、ES 融合权重、向量模型或查询改写，而不是直接调整 rerank。
2. 查看 rerank 前后 `NDCG@10`。如果 rerank 下降，先尝试 rerank off 或更换 rerank 模型。
3. 抽查 10 条失败 query，判断是 query 过难、标注错误、parser 丢内容、还是 chunk mapping 错误。
4. 如果主要问题是 parser 或 evidence locator，回到文档解析与规范化任务修复。
5. 如果主要问题是参数空间不足，再增加一组 D 参数；D 参数必须记录在计划执行记录中，不能覆盖 A/B/C。

### LLM 生成异常

- LLM 未配置：停止在候选生成前，记录 `blocked_reason=llm_config_missing`。
- LLM 输出不是合法 JSON：最多重试 2 次；仍失败则记录 `generation_status=failed_json_schema`。
- `evidence_quote` 无法在源文档中定位：该候选标为 `rejected`，不能进入人工 approved 集。
- LLM 生成的问题依赖外部知识：该候选标为 `rejected_external_knowledge`。

---

## 50 条候选 Query 的类型设计

这 50 条 query 不是随机问题，也不是学术测试题。它们要模拟 OpenRag 在真实文档规模扩大后会遇到的问题类型：单文档查找、多文档比较、类似场景或条件下的精确提取、版本更新判断，以及没有充分答案的边界问题。

### 推荐配比

| Query 类型 | 数量 | 证据范围 | 真实场景模拟程度 | 主要目的 |
| --- | ---: | --- | --- | --- |
| `single_doc_fact` | 15 | 单个文档，通常一个 evidence span | 高 | 验证直接事实、数值、日期、名称等基础召回能力。 |
| `single_doc_rule_or_procedure` | 8 | 单个文档，常需要 2-3 个相邻 evidence span | 高 | 验证规则、条件、步骤、定义类问题的召回能力。 |
| `multi_doc_compare` | 8 | 多个相关文档 | 中高 | 验证跨文档比较、同主题聚合、结果覆盖度。 |
| `multi_scenario_condition_extract` | 10 | 多个相似文档、相似场景或不同条件 | 很高 | 验证按条件筛选后精确抽取信息的能力。 |
| `version_or_update_trace` | 4 | 多个版本或更新时间不同的文档 | 高 | 验证最新版本、修订变化、冲突内容的召回能力。 |
| `negative_or_boundary` | 5 | 没有充分答案，或只有弱相关证据 | 高 | 验证系统不要过度自信，暴露 zero-hit、弱命中和歧义问题。 |

实际操作时建议先生成 60-70 条原始候选，因为人工审核会剔除一部分。最终进入 mini eval 的 approved query 保持 50 条，并尽量贴近上表配比。

### 1. `single_doc_fact`

这类问题的答案可以在一个文档内找到，通常一个 evidence span 就足够回答。运行 eval 时，再把该 evidence span 映射到当前分块版本下的相关 chunk。

适合的问题：

- 某个日期、金额、名称、编号、状态、发行人、适用对象是什么。
- 某个术语的定义是什么。
- 某个条款中给出的阈值、比例、期限是什么。

标注方式：

- 直接包含答案的 evidence span 标 `relevance_grade=3`。
- 只提供解释背景、但不能单独回答的 evidence span 标 `grade=2`。
- 相似但不含答案的位置可以标 `grade=1` 或不标。

真实模拟价值：

- 这是最基础能力。如果这类问题表现不稳定，后续多文档和复杂条件类评测意义不大。

### 2. `single_doc_rule_or_procedure`

这类问题仍然只依赖一个文档，但答案通常分散在相邻段落或多个 evidence span 中。

适合的问题：

- “某类业务办理需要满足哪些条件？”
- “出现某种情况时应该如何处理？”
- “某个规则的适用范围是什么？”
- “某个流程有哪些步骤？”

标注方式：

- 包含关键条件、步骤或结论的 evidence span 标 `grade=3`。
- 只提供背景、术语解释、上下文的 evidence span 标 `grade=2`。
- 如果答案跨相邻段落或证据片段，所有必要 evidence span 都要标注，并在 `metadata.requires_multi_span=true` 中说明。

真实模拟价值：

- 这类问题更接近业务用户的操作型提问，不只是关键词查找。

### 3. `multi_doc_compare`

这类问题需要比较两个或多个相关文档。

适合的问题：

- “A 类文件和 B 类文件在某项规则上有什么差异？”
- “不同机构、产品、日期下的某个字段分别是什么？”
- “哪些文件都提到了同一个约束？”

标注方式：

- 每个参与比较的文档至少要有一个 `grade=3` evidence span。
- 解释比较背景的 evidence span 可以标 `grade=2`。
- 在 `metadata.evidence_role` 中标明 `comparison_left`、`comparison_right`、`comparison_support` 等角色。

真实模拟价值：

- 这类问题检验系统是否能覆盖多个文档，而不是只返回某一个文档里的许多相似 chunk。

### 4. `multi_scenario_condition_extract`

这类问题要求在多个类似场景、条件或文件中精确提取信息，是最接近生产使用的 query 类型之一。

适合的问题：

- “在满足条件 X 的文件里，字段 Y 的取值是什么？”
- “哪些场景下允许或禁止某操作？”
- “同一类业务在不同日期、机构、产品类型下分别对应什么要求？”
- “符合某个条件的文档中，具体金额、期限、比例分别是多少？”

标注方式：

- 同时包含条件和答案的 evidence span 标 `grade=3`。
- 只包含条件或只包含答案、但不能单独支撑最终答案的 evidence span 标 `grade=2`。
- 在 `metadata.evidence_role` 中标明 `condition`、`answer`、`condition_and_answer` 或 `counterexample`。

真实模拟价值：

- 这类问题能检验系统在相似文档很多时，是否能准确筛选条件并提取正确内容。很多 RAG 系统在文档规模扩大后，最容易在这里退化。

### 5. `version_or_update_trace`

这类问题关注最新规则、旧版与新版差异、修订内容或冲突判断。

适合的问题：

- “最新版本中某项要求是什么？”
- “相比旧版本，某条规则发生了什么变化？”
- “两个文件说法不一致时，以哪个为准？”

标注方式：

- 最新、权威或最终生效的 evidence span 标 `grade=3`。
- 旧版本证据如果有助于解释变化，标 `grade=2`，并设置 `metadata.evidence_role=previous_version`。
- 如果某文件已被明确替代，设置 `metadata.is_superseded=true`。

真实模拟价值：

- 这类问题检验系统能否处理文档生命周期，而不是把所有 chunk 都当作同等有效的信息。

### 6. `negative_or_boundary`

这类问题在当前语料中没有充分答案，或者只有弱相关、部分相关、存在歧义的证据。

适合的问题：

- 问一个听起来合理但语料中不存在的问题。
- 问一个过于具体的问题，文档只提供部分答案。
- 问一个需要澄清条件的问题。

标注方式：

- 设置 `expected_answer_type=no_answer`、`partial_answer` 或 `ambiguous`。
- 除非确实有充分证据，否则不要创建 `grade>=2` 的 judgment。
- 弱相关证据可以标 `grade=1`。

真实模拟价值：

- 这类问题用于检验系统是否过度自信。它们应进入 zero-hit、weak-hit 和异常分析，但不要和普通有答案 query 混在一起计算 NDCG/Precision，除非后续单独定义 no-answer 指标。

---

## 稳定锚点原则

人工审核时不能只存 `chunk_id`，甚至不应该把 `chunk_id` 作为评测真值。

原因是 `chunk_id` 只在当前分块版本下稳定。一旦 `chunk_size`、`overlap`、parser 版本、清洗规则或文档结构化方式发生变化，同一段证据可能落到新的 chunk 中。如果只存旧 `chunk_id`，后续就无法判断新参数下哪个 chunk 才覆盖了原本的答案证据。

稳定锚点的核心是保存“这条标注为什么成立”以及“这段证据在源文档哪里”。因此评测真值采用 `query -> expected_answer -> evidence span -> index_version chunk mapping` 的结构。

### Evidence Span 必填字段

这些字段建议存入 `eval_judgments.metadata`。如果后续实现独立的 evidence anchor 表，也可以迁移过去。

```json
{
  "anchor_version": "v2",
  "file_id": "file_abc",
  "source_uri": "minio://bucket/object-key-or-original-uri",
  "source_doc_hash": "sha256-if-available",
  "canonical_doc_version": "parser_name@parser_version",
  "canonical_text_hash": "sha256-of-normalized-text-if-available",
  "evidence_quote": "用于支持答案的原文短摘录，建议 50-300 字。",
  "answer_quote": "直接回答问题的答案片段，如果和 evidence_quote 不同则单独保存。",
  "expected_answer": "人工确认后的答案或答案摘要。",
  "evidence_role": "primary",
  "review_status": "approved",
  "reviewer_id": "user_or_operator_id",
  "reviewed_at": "2026-05-27T00:00:00+08:00",
  "locators": {
    "char_start": 15320,
    "char_end": 15508,
    "page_number": 3,
    "section_heading": "适用范围",
    "block_id": "block_045"
  }
}
```

字段说明：

- `file_id`：OpenRag 中稳定的文件 ID。
- `source_uri`：源文件对象地址或原始路径，便于导出、重放和排查。
- `source_doc_hash`：源文件 hash，如果已有就保存，避免文件被替换后锚点悄悄漂移。
- `canonical_doc_version`：生成规范化文本和定位信息时使用的 parser 名称与版本。
- `canonical_text_hash`：规范化文本 hash，如果已有就保存，用来判断 parser 输出是否发生变化。
- `evidence_quote`：最关键的锚点。它是能证明答案的原文短摘录，后续重分块时主要靠它重新定位。
- `answer_quote`：答案本身的原文片段，适合答案只占 evidence quote 中一小段的情况。
- `expected_answer`：人工确认后的答案、答案片段或答案摘要。
- `evidence_role`：说明该证据在回答中扮演什么角色。
- `review_status`：只有 `approved` 的 query 和 judgment 才能进入 mini eval。
- `locators`：源文档位置。文本类文档优先使用 `char_start/char_end`；PDF/OCR 类文档优先使用页码、bbox、block 信息。

### Chunk 映射缓存字段

`chunk_id` 不作为真值，但可以作为某个分块版本下的缓存映射结果保存，方便 eval run 直接计算 chunk 级指标。

```json
{
  "chunk_mappings": [
    {
      "index_version": "index_a_512_64",
      "chunk_params": {
        "chunk_size": 512,
        "chunk_overlap": 64,
        "min_chunk_tokens": 32
      },
      "chunk_id": "chunk_123",
      "coverage_ratio": 1.0,
      "mapping_method": "char_span_overlap",
      "mapping_status": "mapped"
    }
  ]
}
```

字段说明：

- `index_version`：当前分块和索引版本。
- `chunk_params`：产生该 chunk 的分块参数。
- `chunk_id`：该 index version 下覆盖 evidence span 的 chunk。
- `coverage_ratio`：chunk 覆盖 evidence span 的比例。
- `mapping_method`：可取 `char_span_overlap`、`exact_quote`、`page_block`、`fuzzy_quote`。
- `mapping_status`：可取 `mapped`、`mapped_partial`、`needs_review`、`unmapped`。

### 推荐锚点字段

如果 parser 或文档规范化结果能提供以下信息，应尽量补充：

```json
{
  "page_number": 3,
  "section_heading": "适用范围",
  "block_id": "block_045",
  "paragraph_index": 12,
  "char_start": 15320,
  "char_end": 15508,
  "bbox": [72.0, 144.2, 510.3, 188.7],
  "quote_before": "前置上下文 20-50 字",
  "quote_after": "后置上下文 20-50 字",
  "locator_confidence": "exact_quote",
  "human_note": "人工审核时记录的判断原因"
}
```

字段说明：

- `page_number`：PDF、扫描件、版式文档很有用。
- `section_heading`：长文档中同一句话可能重复出现，标题能帮助消歧。
- `block_id` / `paragraph_index`：如果 parser 能保持稳定块顺序，这是很好的定位信息。
- `char_start` / `char_end`：如果有全文字符偏移，这是文本类文档最强的锚点。
- `bbox`：对 PDF、OCR、表格、版面类文档有价值。
- `quote_before` / `quote_after`：当 `evidence_quote` 重复出现时，用前后文消歧。
- `locator_confidence`：记录锚点可靠性，可取 `char_span`、`exact_quote`、`page_heading`、`block_position`、`quote_only`。

### `evidence_role` 枚举

| 值 | 含义 |
| --- | --- |
| `primary` | 该 evidence span 可以直接回答问题。 |
| `supporting` | 该 evidence span 提供解释或背景，不能单独回答。 |
| `condition` | 该 evidence span 提供筛选条件。 |
| `answer` | 该 evidence span 提供答案值，但不包含完整条件。 |
| `condition_and_answer` | 该 evidence span 同时包含条件和答案。 |
| `comparison_left` | 比较问题中的一侧证据。 |
| `comparison_right` | 比较问题中的另一侧证据。 |
| `comparison_support` | 比较问题的辅助解释。 |
| `previous_version` | 旧版或被替代证据。 |
| `latest_version` | 最新或权威证据。 |
| `counterexample` | 看起来相似但不应作为答案的反例。 |

---

## 人工审核流程

LLM 可以负责生成候选 query，但第一轮 50 条必须人工审核。人工审核的目标不是“让问题看起来合理”，而是确认每条 query 的答案、证据和标注都能经得起重复评测。

### 审核清单

每条候选 query 都检查：

- [ ] 问法像真实用户，而不是复制标题或考试题。
- [ ] 答案确实来自当前 392 个文件，不依赖模型记忆。
- [ ] `query_type` 正确。
- [ ] `expected_answer` 明确，或 `expected_answer_type` 明确。
- [ ] 相关性等级使用 0-3，且含义一致。
- [ ] 每个 `grade>=2` 的 judgment 都有源文档 evidence span 锚点。
- [ ] 多文档问题覆盖所有必要文档。
- [ ] 多场景/条件提取问题同时标出条件证据和答案证据。
- [ ] 没有答案的问题被单独标为 `negative_or_boundary`。

### 审核结果

使用三个状态：

- `approved`：可以进入 50 条 mini eval。
- `rewrite`：问题方向可用，但 query 表述、答案或证据标注需要修改。
- `rejected`：不适合作为本轮评测数据。

只有 `approved` 计入最终 50 条。

### 最低 judgment 要求

普通有答案 query：

- 至少一个 `grade=3` judgment。
- 每个 `grade>=2` judgment 必须有 `file_id`、`source_uri`、`evidence_quote`、`expected_answer`、`evidence_role`、`review_status`，并至少具备一种源文档定位方式，例如 `char_start/char_end`、`page_number + bbox`、`block_id`、`paragraph_index` 或 `quote_before/quote_after`。
- 多文档 query 必须保证每个必要文档至少有一条 judgment。

`negative_or_boundary` query：

- 必须设置 `expected_answer_type=no_answer`、`partial_answer` 或 `ambiguous`。
- 可以没有 `grade>=2` judgment。
- 如果有弱相关证据，使用 `grade=1`。
- 后续指标汇总时单独统计，不和普通有答案 query 混算。

---

## 跨分块参数的锚点重映射

当测试新的分块参数时，不应该重新人工标注 50 条 query，而是先用源文档 evidence span 自动映射到当前分块版本下的 chunk。

### 重映射逻辑

对每个 approved judgment 执行：

1. 根据 `file_id` 读取同一个源文件。
2. 如果有 `source_doc_hash`，先确认源文件没有变化。
3. 如果有 `canonical_text_hash`，确认规范化文本版本没有变化；若变化，标记为需要复核。
4. 优先用 `char_start/char_end` 计算 evidence span 与新 chunk 的重叠比例。
5. 如果没有字符偏移，则在新分块结果中搜索 `evidence_quote`。
6. 如果只有一个 chunk 完整覆盖 evidence span 或完整包含 `evidence_quote`，直接映射到该新 `chunk_id`。
7. 如果多个 chunk 命中，优先匹配 `page_number`、`section_heading`、`block_id`、`paragraph_index`。
8. 如果证据被拆到多个 chunk：
   - 能单独回答问题的 chunk 保持 `grade=3`。
   - 只能提供部分证据的 chunk 调整为 `grade=2`。
9. 如果精确匹配失败，使用 `quote_before`、`quote_after`、页码、标题、块位置做模糊匹配。
10. 如果置信度低，标为 `mapping_status=needs_review`，进入人工复核。

### 重映射结果字段

映射结果可以写入 judgment metadata 的 `chunk_mappings`，也可以作为 eval run 级别的映射记录保存：

```json
{
  "judgment_id": "judgment_123",
  "index_version": "index_b_1000_150",
  "mapped_chunk_id": "chunk_789",
  "mapping_method": "char_span_overlap",
  "mapping_confidence": 0.98,
  "coverage_ratio": 1.0,
  "mapping_status": "mapped",
  "mapping_notes": "evidence span 被一个新 chunk 完整覆盖。"
}
```

`mapping_status` 取值：

- `mapped`：高置信度映射成功。
- `mapped_partial`：证据跨 chunk 或只有部分命中。
- `needs_review`：需要人工复核。
- `unmapped`：新分块结果中没有找到证据。

验收要求：

- 90% 以上的正向 judgment 应能自动映射为 `mapped` 或 `mapped_partial`。
- 所有 `needs_review` 和 `unmapped` 必须人工处理后，才能把该分块参数纳入最终对比。

---

## 分块参数 Mini Eval 方案

第一轮建议比较 3 组参数：

| 参数组 | chunk_size | overlap | min_chunk_tokens | 目的 |
| --- | ---: | ---: | ---: | --- |
| A | 512 | 64 | 32 | 业界常见基线。 |
| B | 1000 | 150 | 32 | 偏大 chunk，接近当前本地环境中可能使用的方向。 |
| C | 800 | 100 | 32 | 中等偏大 chunk，用于观察在保留较多上下文时的召回与 rerank 敏感度。 |

每组参数都执行：

1. 使用同一批 392 个文件重新分块和索引，或至少隔离出独立 index version。
2. 用源文档 evidence span 将 50 条 approved query 的 judgment 映射到当前 chunk set。
3. 人工处理所有 `needs_review` 和 `unmapped`。
4. 使用相同检索策略、相同 rerank 设置执行 eval run。
5. 保存 `search_config_snapshot`、`index_version` 和指标结果。

重点指标：

- `NDCG@10`
- `Recall@50`
- `Precision@10`
- `MRR@50`
- `Stage Recall@50`
- `Rerank Delta`
- `zero_hit_rate`

参数选择建议：

- 如果 `NDCG@10` 和 `MRR@50` 更好，且 `Recall@50` 没有明显下降，可以优先选择该参数。
- 如果 `Precision@10` 提升但 `Recall@50` 明显下降，要谨慎，因为这可能意味着召回阶段漏掉了正确证据。
- 如果单文档事实类提升，但多场景/多条件提取类下降，应按真实业务目标决定，不要只看总体均值。
- 如果 rerank 后指标下降，要检查 rerank 输入 top50 是否已经包含正确证据，以及 rerank 是否把 `grade=3` 证据降到后面。

---

## 500 条评测集扩展方案

评测真值已经绑定到源文档 evidence span，因此完整 500 条不再被某个 chunk 参数锁死。不过仍建议在 50 条 mini eval 跑完后再扩展，因为 mini eval 可以先验证 query 类型、标注规范、证据锚点和映射流程是否可靠，避免一次性放大标注错误。

推荐配比：

| Query 类型 | 数量 |
| --- | ---: |
| `single_doc_fact` | 150 |
| `single_doc_rule_or_procedure` | 80 |
| `multi_doc_compare` | 80 |
| `multi_scenario_condition_extract` | 120 |
| `version_or_update_trace` | 40 |
| `negative_or_boundary` | 30 |

质量规则：

- 默认生成结果都标为 `source=llm_assisted`。
- 人工完整审核通过的 query 可以升级为 `source=gold_manual`。
- 指标汇总必须保留 `gold_manual`、`llm_assisted` 和加权综合三个视角。
- `negative_or_boundary` 单独统计，不默认混入普通 NDCG/Precision。

---

## Task 1：建立 Corpus Manifest

**文件：**
- 创建：`docs/eval/corpus_manifest.2026-05-27.json`
- 参考：`docker/.env.example`
- 参考：`openrag/src/openrag/models/file.py`

- [ ] 使用本轮固定来源：`source_type=local_dir`，`source_root=E:\外汇文件`。
- [ ] 为每个文件生成一条 manifest 记录，包含 `corpus_file_id`、`file_name`、`file_ext`、`source_uri` 或 `local_path`、`size_bytes`、`source_doc_hash`、`detected_mime_type`。
- [ ] 如果文件已经上传到 OpenRag，补充 `openrag_file_id` 和 `workspace_id`。
- [ ] 统计 `format_summary`，当前基线应为 `.md=153`、`.docx=116`、`.doc=59`、`.pdf=56`、`.xls=6`、`.xlsx=2`。
- [ ] 对不支持或本轮不参与评测的文件设置 `include_in_eval=false`，并写入明确 `exclude_reason`。

验证：

- `files.length == 392`。
- `include_in_eval=true` 的文件都可以从 manifest 中的路径或对象地址读取。
- 每条文件记录都有 `source_doc_hash`。
- `format_summary` 与 `files` 列表逐项统计一致，并与 `E:\外汇文件` 当前基线分布一致。
- 如果 manifest 不满足以上条件，停止后续任务。

---

## Task 2：解析文档并导出规范化文档视图

**文件：**
- 输入：`docs/eval/corpus_manifest.2026-05-27.json`
- 参考：`openrag/src/openrag/processors/document_processor.py`
- 参考：`openrag/src/openrag/parsers/`
- 产出：MinIO `parsed/{workspace_id}/{file_id}/{source_doc_hash}/{parser_name}@{parser_version}/canonical.json`
- 产出：MinIO `parsed/{workspace_id}/{file_id}/{source_doc_hash}/{parser_name}@{parser_version}/canonical.md`
- 产出：`docs/eval/canonical_docs.2026-05-27.jsonl`，只保存 parse 产物引用和定位索引摘要，不作为唯一完整正文存储。

- [ ] 对 `include_in_eval=true` 的文件执行 parse。
- [ ] 为每个文件记录 parser 名称、parser 版本、解析时间、解析状态、失败原因。
- [ ] 将 parse 后完整文件内容保存为 `canonical.json` 和 `canonical.md`。
- [ ] `canonical.json` 保存权威结构化 blocks、页码、标题、char span、bbox、表格结构和 block_type。
- [ ] `canonical.md` 保存完整可读 parse 后文档，用于人工审核、LLM 生成 query 和排查。
- [ ] 导出规范化文档视图引用索引，每行至少包含 `corpus_file_id`、`file_id`、`source_doc_hash`、`canonical_text_hash`、`parser_name`、`parser_version`、`canonical_json_object_key`、`canonical_md_object_key`。
- [ ] 导出可定位 block 或 paragraph，至少包含 `block_id`、`text`、`page_number`、`section_heading`、`char_start`、`char_end`；PDF/OCR 有 bbox 时同时保存 `bbox`。
- [ ] 对解析失败文件写入 `parse_status=failed` 和 `parse_error`，不要静默跳过。

验证：

- 每个 `include_in_eval=true` 文件都有一条 canonical doc 记录。
- 每个成功解析文件都有 MinIO `canonical.json` 和 `canonical.md`。
- 每个成功解析的文件至少有一种 evidence locator：`char_start/char_end`、`page_number + bbox`、`block_id/paragraph_index` 或 `evidence_quote + quote_before/quote_after`。
- `source_doc_hash` 与 manifest 一致。
- 解析失败文件数量和原因可汇总。

---

## Task 3：构建基线索引并导出 Chunk Inventory

**文件：**
- 输入：`docs/eval/corpus_manifest.2026-05-27.json`
- 输入：`docs/eval/canonical_docs.2026-05-27.jsonl`
- 参考：`openrag/src/openrag/chunking/chunk_params.py`
- 产出：`docs/eval/chunk_inventory.index_a_512_64.jsonl`

- [ ] 使用参数组 A：`chunk_size=512`、`overlap=64`、`min_chunk_tokens=32` 执行 chunk。
- [ ] 对 chunk 执行 embed 和 index，形成 `index_version=index_a_512_64`。
- [ ] 导出 chunk inventory，每行包含 `index_version`、`chunk_id`、`corpus_file_id`、`file_id`、`chunk_text_preview`、`token_count`、`source_position`、`page_number`、`section_heading`。
- [ ] 记录 embedding 模型、embedding 维度、Milvus collection、ES index、chunk 参数。

验证：

- 每个成功解析文件至少生成一个 chunk，除非文件为空或被明确排除。
- chunk inventory 能支持 evidence span 到 chunk 的映射。
- index metadata 中包含 `index_version`、chunk 参数和 embedding 模型。

---

## Task 4：基于文档证据生成原始候选 Query

**文件：**
- 输入：`docs/eval/canonical_docs.2026-05-27.jsonl`
- 创建：`docs/eval/query-generation-prompts.md`
- 产出：`docs/eval/eval_query_candidates.raw.2026-05-27.jsonl`

- [ ] 从 canonical docs 中按文件格式、文档长度、主题、证据类型分层采样 evidence candidate。
- [ ] 按“单文档单证据、单文档多证据、多文档比较、多场景条件提取、无答案/边界问题”的配比组织 LLM prompt。
- [ ] 每次 prompt 只传 1-6 个 evidence candidate 和必要 metadata，不传 392 个文件全文。
- [ ] 使用已记录的 LLM 配置生成 60-70 条原始候选。
- [ ] 要求 LLM 输出 `query_type`、`query_text`、`expected_answer`、`candidate_file_ids`、`evidence_quote`、`answer_quote`、`evidence_role`、`reason`。
- [ ] 对 LLM 输出做 JSON schema 校验，并验证 `evidence_quote` 能在源文档中定位。

验证：

- 原始候选覆盖六类 query。
- 至少 20% 原始候选涉及多文档或多场景证据。
- 除 no-answer 候选外，每条候选至少有一个 proposed evidence quote，且该 quote 能在源文档中定位。
- LLM provider、model、prompt_version、temperature 写入生成 metadata。

---

## Task 5：人工审核 50 条 Query 并补 Evidence Span 锚点

**文件：**
- 输入：`docs/eval/eval_query_candidates.raw.2026-05-27.jsonl`
- 输入：`docs/eval/canonical_docs.2026-05-27.jsonl`
- 产出：`docs/eval/eval_dataset_50.reviewed.2026-05-27.jsonl`
- 后续表目标：`eval_queries`
- 后续表目标：`eval_judgments`

- [ ] 审核 query 表述，必要时改写成真实用户问法。
- [ ] 从源文档确认答案，不能依赖 LLM 的解释。
- [ ] 按六类 taxonomy 确认 `query_type`。
- [ ] 为所有关键证据分配 0-3 相关性等级。
- [ ] 为每个 `grade>=2` judgment 补齐源文档 evidence span 锚点。
- [ ] 如果 parser 提供页码、标题、块 ID、字符偏移，则补推荐锚点字段。
- [ ] 将基线 index version 下的 `chunk_id` 作为 `chunk_mappings` 缓存写入，而不是作为评测真值。
- [ ] 将最终可用 query 和 judgment 标为 `approved`。

验证：

- 最终正好有 50 条 approved query。
- 普通有答案 query 至少有一个 `grade=3` judgment。
- 每个 `grade>=2` judgment 都有 `file_id`、`source_uri`、`evidence_quote`、`expected_answer`、`evidence_role`、`review_status` 和至少一种源文档定位方式。
- `chunk_mappings` 中可以保存基线 `chunk_id`，但任何指标和后续参数比较都不能把基线 `chunk_id` 当作唯一评测真值。
- 多文档和多场景 query 的证据角色完整。

---

## Task 6：用 50 条 Query 跑分块参数 Mini Eval

**文件：**
- 输入：`docs/eval/eval_dataset_50.reviewed.2026-05-27.jsonl`
- 输入：`docs/eval/canonical_docs.2026-05-27.jsonl`
- 产出：参数组 A/B/C 的 eval result 导出。

- [ ] 为参数组 A/B/C 分别重建或隔离 chunk/index。
- [ ] 用源文档 evidence span 把 reviewed judgments 映射到每组参数的新 chunk。
- [ ] 人工处理所有 `needs_review`、`unmapped`、`corpus_drift`。
- [ ] 每组参数使用相同 retrieval 和 rerank 设置执行 50 条 eval。
- [ ] 按 query type、source scope 和总体结果比较指标。
- [ ] 如果 A/B/C 三组都不理想，按“异常路径与决策门”执行诊断，不进入 500 条扩展。

验证：

- 每组参数都有 `search_config_snapshot` 和 `index_version`。
- 人工修复前，至少 90% 正向 judgment 可通过 evidence span 自动映射。
- 指标对比包含 `NDCG@10`、`Recall@50`、`Precision@10`、`MRR@50`、`Stage Recall@50`、`Rerank Delta`、`zero_hit_rate`。
- 最终选择的参数能用指标和 query type breakdown 解释清楚。

---

## Task 7：扩展生成 500 条评测集

**文件：**
- 输入：`docs/eval/corpus_manifest.2026-05-27.json`
- 输入：`docs/eval/canonical_docs.2026-05-27.jsonl`
- 输入：`docs/eval/eval_dataset_50.reviewed.2026-05-27.jsonl`
- 产出：`docs/eval/eval_dataset_500.llm_assisted.2026-05-27.jsonl`

- [ ] 重新校验 corpus manifest 的 `source_doc_hash`，确认语料未漂移。
- [ ] 复用 Task 4 的采样和 prompt 流程，按 500 条目标配比生成完整候选集。
- [ ] 自动附加候选 evidence span 锚点。
- [ ] 对高风险类型优先人工抽检，包括多文档、多场景、no-answer query。
- [ ] 将完整审核通过的样本升级为 `gold_manual`，其余保留 `llm_assisted`。

验证：

- 500 条 query 的类型分布与目标配比接近。
- 所有 `grade>=2` judgment 都有源文档 evidence span 锚点。
- 指标可以按 `gold_manual`、`llm_assisted`、加权综合分别输出。
- 该评测集可以用于上传大量生产文档前的重复调参。
