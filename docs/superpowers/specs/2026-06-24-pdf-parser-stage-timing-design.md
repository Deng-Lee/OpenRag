# PDF Parser 阶段计时埋点 — 设计文档

- 日期：2026-06-24
- 状态：已实现（Implemented，见 docs/superpowers/plans/2026-06-24-pdf-parser-stage-timing.md；分支 feat/pdf-parser-stage-timing）
- 范围：`RAGFlowPdfParser.__call__` 解析路径的阶段级计时日志

## 1. 背景与目标

希望在 PDF 解析过程中，对以下 8 个阶段加上详细的计时埋点，能看到每个阶段各花了多长时间；同时把 `file_path` 和 `task_id` / `file_id` 打进日志，便于在**并发解析**时区分是哪个 PDF 的耗时。

目标阶段（用户给定命名）：

- `pdf.images_ocr`
- `pdf.layout_recognition`
- `pdf.table_transformer`
- `pdf.text_merge`
- `pdf.concat_downward`
- `pdf.filter_forpages`
- `pdf.extract_table_figure`
- `pdf.filterout_scraps`

## 2. 已确认的决策

| 决策点 | 选择 |
|---|---|
| 埋点范围 | **只给这 8 个阶段的计时日志带上下文**，不改全局 logging 配置，不动阶段方法内部已有日志 |
| 输出格式 | **结构化 JSON**（落在日志 message 字段里，`ensure_ascii=False`） |
| logger 命名 | **按阶段分名**：`pdf.<stage>` 各一个 child logger；汇总行用 `pdf.parse_profile`；统一在 `pdf.*` 命名空间下 |
| 计时入口 | 仅生产路径 `__call__`；`parse_into_bboxes`（另一条入口，已有自带计时/callback）本次不动 |
| 日志级别 | `INFO`（要能默认看得到） |

## 3. 关键现状（代码事实）

- 8 个阶段就是 `RAGFlowPdfParser.__call__` 里按顺序的 8 次调用
  （[pdf_parser.py:2163-2188](../../../openrag/src/openrag/parsers/ragflow/parser/pdf_parser.py)）：

  | 阶段名 | 实际调用 |
  |---|---|
  | `pdf.images_ocr` | `self.__images__(fnm, zoomin)` |
  | `pdf.layout_recognition` | `self._layouts_rec(zoomin)` |
  | `pdf.table_transformer` | `self._table_transformer_job(zoomin, auto_rotate=...)` |
  | `pdf.text_merge` | `self._text_merge()` |
  | `pdf.concat_downward` | `self._concat_downward()` |
  | `pdf.filter_forpages` | `self._filter_forpages()` |
  | `pdf.extract_table_figure` | `self._extract_table_figure(need_image, zoomin, return_html, True)` |
  | `pdf.filterout_scraps` | `self.__filterout_scraps(deepcopy(self.boxes), zoomin)` |

- `task_id` / `file_id` 已存在于 contextvars（[tracing/context.py](../../../openrag/src/openrag/tracing/context.py)），
  由 worker 在解析前 `set_trace_context(...)` 设置（[task_worker.py:176](../../../openrag/src/openrag/worker/task_worker.py)）。
  解析期间 `get_trace_context()` 取到的就是当前文件的值。
- 并发模型：**一进程一个 PDF、同步解析**（worker 每次只 pull 一个 task）。因此进程内 contextvars 不会串；
  多进程日志汇到一起时，靠 JSON 里的 `file_path` / `task_id` / `file_id` 区分。
- `file_path` 不在 trace context 里，但它是 `__call__` 的 `fnm` 参数（可能是 str 路径，也可能是 bytes）。
- 现状没有任何 logging filter 注入 trace context；生产 worker 用
  `basicConfig(format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")`（[task_worker.py:35](../../../openrag/src/openrag/worker/task_worker.py)），
  旧版 `init_root_logger`（带 `%(process)d`）只用于 `rag/svr/*`。
  → 故采用"上下文写进 JSON message"的方案，与具体 formatter 无关，两套配置下都稳。
- 文件里已有计时惯例：`from timeit import default_timer as timer`（`__ocr`、`parse_into_bboxes` 在用）。复用之。

## 4. 设计

### 4.1 机制：阶段上下文管理器

在 `RAGFlowPdfParser` 上新增一个 `@contextmanager` 方法 `_stage(stage_name)`，在 `__call__` 里用
`with self._stage("pdf.xxx"):` 包住每个阶段调用。所有埋点集中在 `__call__`，**不改阶段方法内部**。

职责：

1. 进入时记录 `start = timer()` 与 `boxes_before = len(self.boxes)`（防御式，取不到记 `None`）。
2. `yield` 执行阶段；捕获异常 → `status="error"`、`error=<异常类型名>`，**记完日志后原样 re-raise**。
3. 退出（finally）：算 `duration_ms`，组装公共字段（见 4.3）+ `boxes_after`，
   通过 `logging.getLogger(stage_name).info(json.dumps(rec, ensure_ascii=False, default=str))` 输出。
4. 把 `{"stage", "duration_ms"}` 追加到 `self._stage_profile`，供汇总行使用。
5. **埋点自身绝不抛错**：取值、`json.dumps`、日志输出全部 try/except 兜底，保证不影响解析结果。

### 4.2 上下文来源

- `task_id` / `file_id`：`get_trace_context()` 读取（import 处用 try/except 兜底，取不到记 `None`）。
- `file_path`：`__call__` 入口暂存 `self._parse_file_path = fnm if isinstance(fnm, str) else "<bytes>"`。

### 4.3 JSON schema

**每阶段一行（logger = `pdf.<stage>`）：**

```json
{"evt":"pdf_stage","stage":"pdf.layout_recognition","task_id":"123","file_id":7,
 "file_path":"C:\\...\\doc_7_report.pdf","duration_ms":1234,
 "boxes_before":540,"boxes_after":498,"status":"ok"}
```

字段说明：

| 字段 | 含义 |
|---|---|
| `evt` | 固定 `"pdf_stage"`，便于过滤 |
| `stage` | 阶段名，与 logger 名一致（JSON 自解析时不依赖行首 `%(name)s`） |
| `task_id` / `file_id` | 来自 trace context；取不到为 `null` |
| `file_path` | 解析文件路径；bytes 输入时为 `"<bytes>"` |
| `duration_ms` | 阶段耗时（毫秒，int），与代码库其它 span 的 `duration_ms` 命名一致 |
| `boxes_before` / `boxes_after` | 阶段前后 `len(self.boxes)`；对 `images_ocr` 这种 `self.boxes` 为 list-of-lists 的阶段，该值是页数量级，仅作参考 |
| `status` | `"ok"`（阶段未向外抛异常）/ `"error"`（阶段向外抛异常）。注意：阶段内部自行 try/except 吞掉、未再抛出的异常（如 `__images__`）仍记 `"ok"` |
| `error` | 仅 `status="error"` 时出现，值为异常类型名 |

**结束汇总一行（logger = `pdf.parse_profile`）：**

```json
{"evt":"pdf_parse_profile","task_id":"123","file_id":7,"file_path":"...",
 "page_count":12,"total_ms":8421,"n_tables":3,"status":"ok",
 "stages":[{"stage":"pdf.images_ocr","duration_ms":4200},
           {"stage":"pdf.layout_recognition","duration_ms":1234},
           {"stage":"pdf.table_transformer","duration_ms":1500}]}
```

- `page_count`：`len(self.page_images)`（防御式，取不到 `None`）。
- `total_ms`：`__call__` 内 8 阶段的总墙钟时间。
- `n_tables`：`len(tbls)`。
- `stages`：按执行顺序的 `{stage, duration_ms}` 列表（即 `self._stage_profile`）。
- 汇总行在 `finally` 中输出：即使中途某阶段抛错也会出（`status` 反映整体成功/失败），
  这样"卡在哪个阶段、到那一步花了多久"始终可见。

### 4.4 `__call__` 改造（示意，非最终代码）

```python
self._parse_file_path = fnm if isinstance(fnm, str) else "<bytes>"
self._stage_profile = []
parse_start = timer()
overall_status = "ok"
tbls = []
try:
    with self._stage("pdf.images_ocr"):
        self.__images__(fnm, zoomin)
    with self._stage("pdf.layout_recognition"):
        self._layouts_rec(zoomin)
    with self._stage("pdf.table_transformer"):
        self._table_transformer_job(zoomin, auto_rotate=auto_rotate_tables)
    with self._stage("pdf.text_merge"):
        self._text_merge()
    try:
        with self._stage("pdf.concat_downward"):
            self._concat_downward()
    except Exception:
        logging.exception("[RAGFlowPdfParser.__call__] _concat_downward FAILED")
        raise
    with self._stage("pdf.filter_forpages"):
        self._filter_forpages()
    try:
        with self._stage("pdf.extract_table_figure"):
            tbls = self._extract_table_figure(need_image, zoomin, return_html, True)
    except Exception:
        logging.exception("[RAGFlowPdfParser.__call__] _extract_table_figure FAILED")
        raise
    with self._stage("pdf.filterout_scraps"):
        result_text = self.__filterout_scraps(deepcopy(self.boxes), zoomin)
    return result_text, tbls
except Exception:
    overall_status = "error"
    raise
finally:
    self._emit_parse_profile(
        total_ms=int((timer() - parse_start) * 1000),
        n_tables=len(tbls or []),
        status=overall_status,
    )
```

- 现有围绕 `_concat_downward` / `_extract_table_figure` 的 `try/except + logging.exception` 保留（与埋点互补）。
- 现有两行 "Before/After _concat_downward: boxes=%s" 与新 JSON 的 `boxes_before/after` 有些重叠，本次**保留不删**（最小改动）；后续如嫌冗余可单独清理。
- 名字改写（`__images__` / `__filterout_scraps`）在 `__call__`（同类内）调用正常，不受影响。

### 4.5 不做（YAGNI / 非目标）

- 不加全局 logging filter / LogRecord factory（范围已定为只 8 个阶段）。
- 不改 `basicConfig` / `init_root_logger` 的 formatter。
- 不给阶段方法内部已有日志补 task_id/file_id。
- 不动 `parse_into_bboxes`。
- 不把 `file_path` 加进 trace context（只在埋点里带）。

### 4.6 增强：profile 也落 `parse.document` trace span（DB 可查，2026-06-24 追加）

在"只打日志"之外，额外把本次解析的 profile 子集 `{total_ms, page_count, n_tables, status, stages}` 写进**既有** `parse.document` trace span 的 `output_summary`（不新建 TraceRun/TraceSpan，只给已有 span 加字段）。

- 链路：`RAGFlowPdfParser._emit_parse_profile` 把 rec 存到 `self._last_parse_profile` → `PDFParserAdapter` 解析后读到 `self.last_parse_profile` → `document_processor._parse_span_profile(parser)` 取干净子集 → 加到 `parse.document` span 的 `output_summary["pdf_stage_profile"]`（非 PDF parser 返回 None，不加该字段）。
- 查询：`GET /traces?file_id=<id>` 拿 trace_id → `GET /traces/{trace_id}` → 找 `stage="parse.document"` 的 span → `output_summary.pdf_stage_profile`。
- 好处：有界（一文件一份 JSON）、可查、不随上传量堆日志文件。

## 5. 受影响文件

- `openrag/src/openrag/parsers/ragflow/parser/pdf_parser.py`
  - `import json`；`from openrag.tracing.context import get_trace_context`（try/except 兜底）；`from contextlib import contextmanager`。
  - 新增 `_stage()` 上下文管理器、`_emit_parse_profile()`、公共字段/计数小助手。
  - `__call__` 入口初始化 `self._parse_file_path` / `self._stage_profile`，用 `with` 包 8 个阶段，`finally` 出汇总。
- `openrag/tests/test_pdf_parse_timing.py`（新增，见 §6）。

## 6. 测试

- 单测：`set_trace_context(task_id="t1", file_id=42)` 后，mock 掉 8 个阶段方法（避免真跑模型），调用 `__call__`，用 `caplog` 断言：
  - 出现 8 条 `evt=pdf_stage`，`stage` 覆盖全部 8 个阶段名；
  - 每条都带 `task_id="t1"`、`file_id=42`、`file_path`、`duration_ms` 为 int；
  - 出现 1 条 `evt=pdf_parse_profile`，`stages` 长度为 8、含 `total_ms`；
  - 异常路径：让某阶段抛错，断言该阶段行 `status="error"` 且带 `error`，异常仍被抛出，汇总行 `status="error"` 仍输出。
- 取不到 trace context（未 set）时，`task_id`/`file_id` 为 `null`，不报错。

## 7. 验收标准

- 解析一个 PDF 后，日志中按顺序出现 8 条阶段 JSON + 1 条汇总 JSON。
- 每条 JSON 含 `task_id` / `file_id` / `file_path` 与该阶段 `duration_ms`。
- child logger 分名（`pdf.<stage>`）保留，便于将来按阶段控制级别。**注意**：`LOG_LEVELS` 环境变量调级只在调用了 `init_root_logger()` 的旧版 `rag/svr/*` 进程生效；当前生产 worker（`task_worker.py` 用 `logging.basicConfig`，未调用 `init_root_logger`）下 `pdf.*` 的 INFO 默认输出，但 `LOG_LEVELS=pdf=WARNING` **不生效**。本次不把 LOG_LEVELS 接进 worker（超范围）；如需在 worker 静音可运行时 `logging.getLogger("pdf").setLevel(...)`。故"可调级"为说明项、非硬性验收。
- 埋点异常不影响解析结果与返回值。

## 8. 开放问题

无（范围、格式、命名均已确认）。
