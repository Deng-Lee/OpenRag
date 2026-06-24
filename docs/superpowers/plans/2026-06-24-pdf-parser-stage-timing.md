# PDF Parser 阶段计时埋点 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `RAGFlowPdfParser.__call__` 的 8 个解析阶段加上结构化 JSON 计时埋点，每行带 `task_id`/`file_id`/`file_path`，并在结束时输出一条耗时汇总。

**Architecture:** 在 `RAGFlowPdfParser` 上新增一个 `_stage()` 上下文管理器（`@contextmanager`），用 `with` 包住 `__call__` 里的 8 个阶段调用；`task_id`/`file_id` 从既有 contextvars（`openrag.tracing.context.get_trace_context`）读取，`file_path` 来自 `__call__` 的 `fnm` 参数。每阶段用 child logger `pdf.<stage>` 输出一行 JSON，结束时用 `pdf.parse_profile` 输出汇总。不改全局 logging 配置、不动阶段方法内部。

**Tech Stack:** Python 3、标准库 `logging` / `json` / `contextlib` / `timeit`、pytest（`caplog`）。

参考 spec：[docs/superpowers/specs/2026-06-24-pdf-parser-stage-timing-design.md](../specs/2026-06-24-pdf-parser-stage-timing-design.md)

> **执行前必读（见文末「实现注意点」「二次评审的处理」）**：JSON 字段统一为 `file_path`；各 Task 的 `git commit` 步骤**默认跳过**，仅当用户明确要求提交时才执行——提交前先确认当前分支与分支策略（工作区当前在 `fix/upload-filename-too-long`，非 `main`）；`LOG_LEVELS` 调级在当前 worker **不生效**（只影响日志调级、不影响埋点输出）。

---

## File Structure

| 文件 | 职责 | 改动 |
|---|---|---|
| `openrag/src/openrag/parsers/ragflow/parser/pdf_parser.py` | PDF 解析器；新增埋点原语并在 `__call__` 接线 | Modify |
| `openrag/tests/test_pdf_parse_timing.py` | 埋点单测（不跑真实模型） | Create |

所有命令均在 **`openrag/` 目录**（`pytest.ini` 所在处）下运行，使用项目 venv（如 Windows：`.\venv\Scripts\python -m pytest ...`）。本计划用 `python -m pytest` 表述。新测试文件不在 `pytest.ini` 的 `python_files` 白名单内，但**显式传文件路径即可被收集**（与现有 `tests/test_pdf_parser_resilience.py` 同理）。

---

## Task 1: 埋点原语 `_stage()` 与上下文/计数辅助

**Files:**
- Modify: `openrag/src/openrag/parsers/ragflow/parser/pdf_parser.py`（imports；在 `def __call__` 之前插入新方法）
- Test: `openrag/tests/test_pdf_parse_timing.py`

- [ ] **Step 1: 写失败测试（新建测试文件）**

创建 `openrag/tests/test_pdf_parse_timing.py`：

```python
"""Tests for RAGFlow PDF parser per-stage timing instrumentation."""

import json
import logging

import pytest

from openrag.parsers.ragflow.parser import pdf_parser as pdf_parser_module
from openrag.tracing.context import reset_trace_context, set_trace_context

RAGFlowPdfParser = pdf_parser_module.RAGFlowPdfParser

STAGE_NAMES = [
    "pdf.images_ocr",
    "pdf.layout_recognition",
    "pdf.table_transformer",
    "pdf.text_merge",
    "pdf.concat_downward",
    "pdf.filter_forpages",
    "pdf.extract_table_figure",
    "pdf.filterout_scraps",
]


@pytest.fixture(autouse=True)
def _clean_trace_ctx():
    reset_trace_context()
    yield
    reset_trace_context()


def _bare_parser():
    """Create a parser instance without running the heavy __init__."""
    p = RAGFlowPdfParser.__new__(RAGFlowPdfParser)
    p._parse_file_path = "/tmp/doc_7_report.pdf"
    p._stage_profile = []
    p.boxes = []
    p.page_images = []
    return p


def _json_events(caplog, evt):
    """Return [(logger_name, parsed_json), ...] for records whose JSON evt matches."""
    out = []
    for r in caplog.records:
        msg = r.getMessage()
        if not msg.startswith("{"):
            continue
        try:
            d = json.loads(msg)
        except ValueError:
            continue
        if d.get("evt") == evt:
            out.append((r.name, d))
    return out


def test_stage_emits_json_with_context(caplog):
    set_trace_context(task_id="t1", file_id=42)
    p = _bare_parser()
    p.boxes = [1, 2, 3]
    with caplog.at_level(logging.INFO, logger="pdf"):
        with p._stage("pdf.layout_recognition"):
            p.boxes = [1, 2]  # simulate the stage mutating boxes
    events = _json_events(caplog, "pdf_stage")
    assert len(events) == 1
    name, d = events[0]
    assert name == "pdf.layout_recognition"
    assert d["stage"] == "pdf.layout_recognition"
    assert d["task_id"] == "t1"
    assert d["file_id"] == 42
    assert d["file_path"] == "/tmp/doc_7_report.pdf"
    assert isinstance(d["duration_ms"], int)
    assert d["boxes_before"] == 3
    assert d["boxes_after"] == 2
    assert d["status"] == "ok"
    assert p._stage_profile == [
        {"stage": "pdf.layout_recognition", "duration_ms": d["duration_ms"]}
    ]


def test_stage_logs_error_and_reraises(caplog):
    set_trace_context(task_id="t1", file_id=42)
    p = _bare_parser()
    with caplog.at_level(logging.INFO, logger="pdf"):
        with pytest.raises(ValueError):
            with p._stage("pdf.text_merge"):
                raise ValueError("boom")
    events = _json_events(caplog, "pdf_stage")
    assert len(events) == 1
    _, d = events[0]
    assert d["stage"] == "pdf.text_merge"
    assert d["status"] == "error"
    assert d["error"] == "ValueError"


def test_stage_without_trace_context(caplog):
    p = _bare_parser()
    with caplog.at_level(logging.INFO, logger="pdf"):
        with p._stage("pdf.images_ocr"):
            pass
    events = _json_events(caplog, "pdf_stage")
    assert len(events) == 1
    _, d = events[0]
    assert d["task_id"] is None
    assert d["file_id"] is None
    assert d["status"] == "ok"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_pdf_parse_timing.py -v`
Expected: 3 个 `test_stage_*` FAIL（`AttributeError: 'RAGFlowPdfParser' object has no attribute '_stage'`）。

- [ ] **Step 3: 加 imports**

在 `pdf_parser.py` 顶部 stdlib 区：
- 在 `import asyncio`（第 17 行）下一行加 `import json`
- 在 `from collections import Counter, defaultdict`（第 26 行）下一行加 `from contextlib import contextmanager`

在 openrag imports 区（`from rag.prompts.generator import vision_llm_describe_prompt` 之后，第 51 行附近）追加（带兜底，tracing 不可用时埋点降级为空 id，不影响解析）：

```python
try:
    from openrag.tracing.context import get_trace_context as _get_trace_context
except Exception:  # pragma: no cover - tracing context is optional
    def _get_trace_context():
        return {}
```

- [ ] **Step 4: 实现埋点原语**

在 `pdf_parser.py` 中 `def __call__(`（约第 2140 行）**之前**插入以下方法（作为 `RAGFlowPdfParser` 的类成员，注意缩进 4 空格）：

```python
    # ---- parse-stage instrumentation -------------------------------------
    _PARSE_PROFILE_LOGGER = "pdf.parse_profile"

    @staticmethod
    def _safe_len(obj):
        try:
            return len(obj) if obj is not None else None
        except Exception:
            return None

    def _parse_ctx_ids(self):
        """task_id/file_id from trace context + file from current parse call."""
        try:
            ctx = _get_trace_context() or {}
        except Exception:
            ctx = {}
        return {
            "task_id": ctx.get("task_id"),
            "file_id": ctx.get("file_id"),
            "file_path": getattr(self, "_parse_file_path", None),
        }

    def _stage_log_base(self, stage):
        rec = {"evt": "pdf_stage", "stage": stage}
        rec.update(self._parse_ctx_ids())
        return rec

    @contextmanager
    def _stage(self, stage):
        """Time one parse stage and emit a structured-JSON log line.

        Reads task_id/file_id from the trace context and file from
        self._parse_file_path. Records len(self.boxes) before/after. The
        instrumentation never raises on its own; if the wrapped stage raises,
        the line is logged with status="error" and the exception re-raised.
        status="ok" only means no exception propagated OUT of the stage; a
        stage that swallows its own exceptions internally (e.g. __images__)
        can still be logged as "ok".
        """
        start = timer()
        boxes_before = self._safe_len(getattr(self, "boxes", None))
        status = "ok"
        err = None
        try:
            yield
        except Exception as exc:
            status = "error"
            err = type(exc).__name__
            raise
        finally:
            duration_ms = int((timer() - start) * 1000)
            try:
                rec = self._stage_log_base(stage)
                rec["duration_ms"] = duration_ms
                rec["boxes_before"] = boxes_before
                rec["boxes_after"] = self._safe_len(getattr(self, "boxes", None))
                rec["status"] = status
                if err:
                    rec["error"] = err
                profile = getattr(self, "_stage_profile", None)
                if isinstance(profile, list):
                    profile.append({"stage": stage, "duration_ms": duration_ms})
                logging.getLogger(stage).info(
                    json.dumps(rec, ensure_ascii=False, default=str)
                )
            except Exception:  # pragma: no cover - never break parsing
                pass
```

- [ ] **Step 5: 运行测试确认通过**

Run: `python -m pytest tests/test_pdf_parse_timing.py -v`
Expected: 3 个 `test_stage_*` PASS。

- [ ] **Step 6: 提交（可选，默认跳过，仅当用户明确要求提交时执行）**

```bash
git add openrag/src/openrag/parsers/ragflow/parser/pdf_parser.py openrag/tests/test_pdf_parse_timing.py
git commit -m "feat(pdf): add _stage timing context manager for parse instrumentation"
```

---

## Task 2: 汇总行 `_emit_parse_profile()`

**Files:**
- Modify: `openrag/src/openrag/parsers/ragflow/parser/pdf_parser.py`（紧接 Task 1 新增方法之后）
- Test: `openrag/tests/test_pdf_parse_timing.py`

- [ ] **Step 1: 追加失败测试**

在 `test_pdf_parse_timing.py` 末尾追加：

```python
def test_emit_parse_profile(caplog):
    set_trace_context(task_id="t9", file_id=7)
    p = _bare_parser()
    p.page_images = [object(), object(), object()]
    p._stage_profile = [
        {"stage": "pdf.images_ocr", "duration_ms": 100},
        {"stage": "pdf.layout_recognition", "duration_ms": 50},
    ]
    with caplog.at_level(logging.INFO, logger="pdf"):
        p._emit_parse_profile(total_ms=200, n_tables=3, status="ok")
    events = _json_events(caplog, "pdf_parse_profile")
    assert len(events) == 1
    name, d = events[0]
    assert name == "pdf.parse_profile"
    assert d["task_id"] == "t9"
    assert d["file_id"] == 7
    assert d["file_path"] == "/tmp/doc_7_report.pdf"
    assert d["page_count"] == 3
    assert d["total_ms"] == 200
    assert d["n_tables"] == 3
    assert d["status"] == "ok"
    assert len(d["stages"]) == 2
    assert d["stages"][0]["stage"] == "pdf.images_ocr"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_pdf_parse_timing.py::test_emit_parse_profile -v`
Expected: FAIL（`AttributeError: ... '_emit_parse_profile'`）。

- [ ] **Step 3: 实现 `_emit_parse_profile`**

在 `pdf_parser.py` 中 Task 1 的 `_stage` 方法之后（仍在 `def __call__` 之前）插入：

```python
    def _emit_parse_profile(self, total_ms, n_tables, status="ok"):
        try:
            ids = self._parse_ctx_ids()
            rec = {
                "evt": "pdf_parse_profile",
                "task_id": ids.get("task_id"),
                "file_id": ids.get("file_id"),
                "file_path": ids.get("file_path"),
                "page_count": self._safe_len(getattr(self, "page_images", None)),
                "total_ms": total_ms,
                "n_tables": n_tables,
                "status": status,
                "stages": list(getattr(self, "_stage_profile", []) or []),
            }
            logging.getLogger(self._PARSE_PROFILE_LOGGER).info(
                json.dumps(rec, ensure_ascii=False, default=str)
            )
        except Exception:  # pragma: no cover - never break parsing
            pass
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_pdf_parse_timing.py::test_emit_parse_profile -v`
Expected: PASS。

- [ ] **Step 5: 提交（可选，默认跳过，仅当用户明确要求提交时执行）**

```bash
git add openrag/src/openrag/parsers/ragflow/parser/pdf_parser.py openrag/tests/test_pdf_parse_timing.py
git commit -m "feat(pdf): add _emit_parse_profile summary line for parse timing"
```

---

## Task 3: 在 `__call__` 接线 8 个阶段 + 汇总

**Files:**
- Modify: `openrag/src/openrag/parsers/ragflow/parser/pdf_parser.py:2163-2188`（`__call__` 主体）
- Test: `openrag/tests/test_pdf_parse_timing.py`

- [ ] **Step 1: 追加失败测试**

在 `test_pdf_parse_timing.py` 末尾追加：

```python
def _stub_all_stages(p, tbls=None):
    """Replace the 8 stage methods with no-op stubs (name-mangled dunders included)."""
    tbls = tbls if tbls is not None else []
    p._RAGFlowPdfParser__images__ = lambda *a, **k: None
    p._layouts_rec = lambda *a, **k: None
    p._table_transformer_job = lambda *a, **k: None
    p._text_merge = lambda *a, **k: None
    p._concat_downward = lambda *a, **k: None
    p._filter_forpages = lambda *a, **k: None
    p._extract_table_figure = lambda *a, **k: tbls
    p._RAGFlowPdfParser__filterout_scraps = lambda *a, **k: "BODY TEXT"


def test_call_emits_all_stage_lines_and_profile(caplog):
    set_trace_context(task_id="tc", file_id=11)
    p = _bare_parser()
    _stub_all_stages(p, tbls=[("img", "tbl")])
    with caplog.at_level(logging.INFO, logger="pdf"):
        text, tbls = RAGFlowPdfParser.__call__(p, "/tmp/doc_11_a.pdf")
    assert text == "BODY TEXT"
    assert len(tbls) == 1
    stage_events = _json_events(caplog, "pdf_stage")
    assert [d["stage"] for _, d in stage_events] == STAGE_NAMES  # order preserved
    for _, d in stage_events:
        assert d["task_id"] == "tc"
        assert d["file_id"] == 11
        assert d["file_path"] == "/tmp/doc_11_a.pdf"
        assert d["status"] == "ok"
    profile = _json_events(caplog, "pdf_parse_profile")
    assert len(profile) == 1
    _, pd = profile[0]
    assert len(pd["stages"]) == 8
    assert pd["n_tables"] == 1
    assert pd["status"] == "ok"
    assert isinstance(pd["total_ms"], int)


def test_call_emits_profile_on_failure(caplog):
    set_trace_context(task_id="tc", file_id=11)
    p = _bare_parser()
    _stub_all_stages(p)

    def _boom(*a, **k):
        raise RuntimeError("layout failed")

    p._layouts_rec = _boom
    with caplog.at_level(logging.INFO, logger="pdf"):
        with pytest.raises(RuntimeError):
            RAGFlowPdfParser.__call__(p, "/tmp/doc_11_a.pdf")
    by_stage = {d["stage"]: d for _, d in _json_events(caplog, "pdf_stage")}
    assert by_stage["pdf.images_ocr"]["status"] == "ok"
    assert by_stage["pdf.layout_recognition"]["status"] == "error"
    assert by_stage["pdf.layout_recognition"]["error"] == "RuntimeError"
    assert "pdf.text_merge" not in by_stage  # never reached after the failure
    profile = _json_events(caplog, "pdf_parse_profile")
    assert len(profile) == 1
    assert profile[0][1]["status"] == "error"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_pdf_parse_timing.py -k "call_emits" -v`
Expected: 2 个 FAIL（当前 `__call__` 不产生 `pdf_stage`/`pdf_parse_profile`，断言不满足）。

- [ ] **Step 3: 改写 `__call__` 主体**

将 `pdf_parser.py` 的第 2163-2188 行（从 `self.__images__(fnm, zoomin)` 到 `return self.__filterout_scraps(deepcopy(self.boxes), zoomin), tbls`）整段替换为：

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
            logging.info(
                "[RAGFlowPdfParser.__call__] Before _concat_downward: boxes=%s",
                len(self.boxes) if self.boxes else 0,
            )
            try:
                with self._stage("pdf.concat_downward"):
                    self._concat_downward()
            except Exception:
                logging.exception(
                    "[RAGFlowPdfParser.__call__] _concat_downward FAILED"
                )
                raise
            logging.info(
                "[RAGFlowPdfParser.__call__] After _concat_downward: boxes=%s",
                len(self.boxes) if self.boxes else 0,
            )
            with self._stage("pdf.filter_forpages"):
                self._filter_forpages()
            try:
                with self._stage("pdf.extract_table_figure"):
                    tbls = self._extract_table_figure(
                        need_image, zoomin, return_html, True
                    )
            except Exception:
                logging.exception(
                    "[RAGFlowPdfParser.__call__] _extract_table_figure FAILED"
                )
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

说明：保留了原有的两行 "Before/After _concat_downward boxes=" 日志与 `_concat_downward`/`_extract_table_figure` 的 `logging.exception` 兜底（与埋点互补）；汇总行放在 `finally`，失败也会出。`self.__images__` / `self.__filterout_scraps` 在 `__call__`（同类内）调用，名字改写正常。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_pdf_parse_timing.py -k "call_emits" -v`
Expected: 2 个 PASS。

- [ ] **Step 5: 提交（可选，默认跳过，仅当用户明确要求提交时执行）**

```bash
git add openrag/src/openrag/parsers/ragflow/parser/pdf_parser.py openrag/tests/test_pdf_parse_timing.py
git commit -m "feat(pdf): instrument __call__ with per-stage timing logs + profile"
```

---

## Task 4: 整文件回归 + spec 状态更新

**Files:**
- Modify: `docs/superpowers/specs/2026-06-24-pdf-parser-stage-timing-design.md`（状态行）

- [ ] **Step 1: 跑通整组新测试**

Run: `python -m pytest tests/test_pdf_parse_timing.py -v`
Expected: 6 passed（`test_stage_emits_json_with_context`、`test_stage_logs_error_and_reraises`、`test_stage_without_trace_context`、`test_emit_parse_profile`、`test_call_emits_all_stage_lines_and_profile`、`test_call_emits_profile_on_failure`）。

- [ ] **Step 2: 确认未破坏既有 PDF 解析回归**

Run: `python -m pytest tests/test_pdf_parser_resilience.py tests/test_pdf_position_tags.py -v`
Expected: 全部 PASS（埋点不改变解析行为）。

- [ ] **Step 3: 更新 spec 状态**

把 `docs/superpowers/specs/2026-06-24-pdf-parser-stage-timing-design.md` 的
`- 状态：待评审（Design）` 改为
`- 状态：已实现（Implemented，见 docs/superpowers/plans/2026-06-24-pdf-parser-stage-timing.md）`

- [ ] **Step 4: 提交（可选，默认跳过，仅当用户明确要求提交时执行）**

```bash
git add docs/superpowers/specs/2026-06-24-pdf-parser-stage-timing-design.md
git commit -m "docs(pdf): mark stage-timing spec as implemented"
```

---

## 验证产出（实现后日志长这样）

解析一个 PDF 后，`pdf.*` logger 会按顺序产出 8 行阶段 JSON + 1 行汇总（message 部分）：

```
{"evt":"pdf_stage","stage":"pdf.images_ocr","task_id":"123","file_id":7,"file_path":"/tmp/doc_7_report.pdf","duration_ms":4200,"boxes_before":0,"boxes_after":12,"status":"ok"}
...
{"evt":"pdf_parse_profile","task_id":"123","file_id":7,"file_path":"/tmp/doc_7_report.pdf","page_count":12,"total_ms":8421,"n_tables":3,"status":"ok","stages":[{"stage":"pdf.images_ocr","duration_ms":4200}, ...]}
```

调级：child logger 分名（`pdf.<stage>`）已就位。**但 `LOG_LEVELS` 调级只在旧版 `rag/svr/*`（调用 `init_root_logger`）生效**；当前生产 worker（`task_worker.py` 用 `logging.basicConfig`）下 `pdf.*` 的 INFO 默认输出、`LOG_LEVELS=pdf=WARNING` 不生效。worker 里要静音需运行时 `logging.getLogger("pdf").setLevel(...)`（本次不接 LOG_LEVELS，超范围）。详见文末「实现注意点」。

---

## Self-Review（计划自检结论）

- **Spec 覆盖**：§4.1 `_stage` → Task 1；§4.3 每阶段 JSON 字段 → Task 1 测试断言；汇总行 → Task 2；§4.4 `__call__` 接线 + finally 汇总 + 保留旧日志 → Task 3；§6 测试（含异常路径、无 context）→ Task 1/2/3 测试；§7 验收（调级/不影响解析）→ Task 4 + 验证产出节。无遗漏。
- **占位符**：无 TBD/TODO；每个 code step 均含完整代码。
- **类型/命名一致性**：`_safe_len`、`_parse_ctx_ids`、`_stage_log_base`、`_stage`、`_emit_parse_profile(total_ms, n_tables, status="ok")`、`_PARSE_PROFILE_LOGGER="pdf.parse_profile"`、实例属性 `_parse_file_path`/`_stage_profile`、模块级 `_get_trace_context` —— 各 Task 间一致；测试中的 stub 名（`_RAGFlowPdfParser__images__`、`_RAGFlowPdfParser__filterout_scraps`）与 `__call__` 内调用一致。

---

## Codex 评审结果（2026-06-24）

来源：Codex 对本计划、设计文档和当前代码实现做静态核对后的评审。未执行任何代码改动；本节仅追加在文件末尾。

### 结论

- **总体可行**：当前 `RAGFlowPdfParser.__call__` 确实是 8 个顺序阶段调用，方案用上下文管理器包裹这些调用，可以得到阶段级墙钟耗时，并且不需要进入各阶段内部改算法。
- **执行风险为中低**：按计划实现时，核心行为应只新增日志副作用；成功路径仍返回 `(text, tbls)`，失败路径仍 re-raise 原异常。只要保持“日志失败不影响解析”的兜底，预计不会改变 chunk、向量、artifact、DB 状态等下游功能。
- **建议可执行，但先收紧两个边界**：一是字段命名在目标里写 `file_path`、实现草案里写 `file`，需要明确统一；二是 `LOG_LEVELS=pdf=WARNING` 在当前 worker 的 `basicConfig` 路径下不一定生效，不能作为已验证的生产静音手段。

### 已确认边界

- 只覆盖 `openrag/src/openrag/parsers/ragflow/parser/pdf_parser.py` 中 `RAGFlowPdfParser.__call__` 的解析路径。
- 不覆盖 `parse_into_bboxes`，它已有 callback 计时；也不覆盖另行实现或 override `__call__` 的 PDF 变体解析入口。
- 不改变全局 logging formatter，不新增 logging filter，不把 `file_path` 注入 trace context。
- 不写 `TraceRun/TraceSpan`，不写 MinIO artifact，不改变 `DocumentBlock`、分块、embedding、Milvus、ES 或文件处理状态。
- `task_id/file_id` 只在 worker 或测试显式 `set_trace_context(...)` 后可靠；本地脚本直接调用 parser 时为 `null` 是合理行为。

### 可行性依据

- `pdf_parser.py` 当前已导入 `timer`，同文件内 `__ocr`、`parse_into_bboxes` 已有计时惯例，复用成本低。
- `DocumentProcessor.process_document()` 已有 `parse.document` 总耗时 span；本方案属于对 PDF parser 内部的日志级细分，不与现有 trace span 冲突。
- `task_worker.py` 在任务执行前设置了 `trace_id/workspace_id/user_id/file_id/task_id`，并在 finally 中 reset；同步解析路径下，`get_trace_context()` 能拿到当前任务上下文。
- 当前 `PDFParserAdapter` 通过 `self.ragflow_parser(file_path)` 触发 `RAGFlowPdfParser.__call__`，所以对普通 `.pdf` 文件有效。

### 主要风险与影响评估

- **对其它功能的行为影响：低**。方案不改阶段内部逻辑、不改返回值格式、不吞原异常；对下游解析结果理论上无影响。需要用 `tests/test_pdf_parser_resilience.py`、`tests/test_pdf_position_tags.py` 回归确认。
- **日志量与噪声：中低**。每个 PDF 至少新增 9 行 INFO JSON；失败时 `_stage` 的 error 行会与现有 `_concat_downward` / `_extract_table_figure` 的 `logging.exception` 形成重复告警。这有利于定位，但生产日志量会增加。
- **日志调级风险：中**。`openrag/common/log_utils.py` 支持 `LOG_LEVELS`，但当前 worker 使用的是 `logging.basicConfig(...)`，没有看到它调用 `init_root_logger()`。因此计划中的 `LOG_LEVELS=pdf=WARNING` 对 worker 进程是否生效需要单独验证；如果不生效，要么补显式 logger level 配置，要么把“可调级”从验收标准里降级为非本次目标。
- **字段命名风险：中低**。目标写“带 `file_path`”，JSON schema 写 `"file"`。如果后续日志采集或检索按字段名消费，应在实现前统一为 `file_path`，或明确 `"file"` 就是最终字段。
- **共享 parser 实例并发风险：低但需写明**。方案使用实例属性 `_parse_file_path` / `_stage_profile`。当前 worker 一次处理一个任务，`ParserRegistry/ParserFactory` 也在任务内创建，风险低；如果未来同一 parser 实例被多线程并发调用，这两个实例属性会互相覆盖。
- **路径暴露风险：中低**。日志会记录 worker 临时本地路径，可能包含原始文件名。若日志会进入外部系统，需要确认是否接受完整路径；更保守的做法是记录 `file_id` + basename，或把完整路径作为 debug-only 字段。
- **测试收集风险：中低**。计划说明新测试需要显式传文件路径才能被 pytest 收集，因为 `pytest.ini` 的 `python_files` 白名单不包含 `test_pdf_parse_timing.py`。如果希望默认 CI 覆盖，需要更新 pytest 配置或在 CI 命令中显式运行该文件。
- **git 操作边界：中低**。计划每个 Task 都带 `git add/commit`。真正执行时如果用户没有要求提交，建议只完成代码和测试，不自动提交；提交前应再次确认工作树中是否有无关改动。

### 建议的执行顺序

1. 先统一 JSON 字段名：建议采用 `file_path`，或者在评审后明确保留 `file`。
2. 实现 `_stage()` / `_emit_parse_profile()` 并跑新增单测，确保日志异常不会影响解析。
3. 接线 `__call__`，保留原有异常日志和返回值。
4. 运行 `python -m pytest tests/test_pdf_parse_timing.py -v`、`python -m pytest tests/test_pdf_parser_resilience.py tests/test_pdf_position_tags.py -v`。
5. 额外做一次轻量 adapter 级验证：mock `RAGFlowPdfParser` 阶段方法后，从 `PDFParserAdapter.parse()` 或 `RAGFlowPdfParser.__call__` 入口确认生产路径能产出 8 条阶段日志 + 1 条汇总日志。

### 最终意见

可以按该方案推进，但把它定义为“PDF RAGFlow parser 生产 `__call__` 路径的日志级阶段耗时观测”，不要扩大为完整 tracing 改造。只要执行时不顺手改 logging 全局配置、不改 parser 阶段内部算法、不自动清理既有日志，这次改动对其它功能的影响应可控。

---

## 实现注意点（基于 Codex 评审的调整，2026-06-24）

针对上节 Codex 评审，已对本计划与 spec 做如下调整/说明（核对：worker 用 `basicConfig` 未调 `init_root_logger`；`ParserFactory` 按扩展名缓存 parser，[factory.py:91-97](../../../openrag/src/openrag/parsers/factory.py)）：

1. **字段统一为 `file_path`（已改）**：JSON 字段由 `file` 改为 `file_path`，与目标描述一致。已同步：`_parse_ctx_ids` 返回键、`_emit_parse_profile`、各测试断言、示例与 spec。
2. **`LOG_LEVELS` 调级仅旧版 svr 生效（已改验收口径）**：生产 worker（[task_worker.py:35](../../../openrag/src/openrag/worker/task_worker.py)）用 `logging.basicConfig` 且未调用 `init_root_logger()`，故 `LOG_LEVELS` 不被解析；`pdf.*` 的 INFO 默认输出，但 `LOG_LEVELS=pdf=WARNING` **对 worker 不生效**。本次不把 LOG_LEVELS 接进 worker（超范围）；child logger 分名保留，worker 内要调级可运行时 `logging.getLogger("pdf").setLevel(...)`。"可调级"已从硬性验收降级为说明项。
3. **parser 实例会被复用、且本就单文档有状态**：`RAGFlowPdfParser` 跨文件复用；但该类本就用 `self.boxes`/`self.page_images`/`self.mean_height` 等实例属性承载单次解析状态、每次 `__call__` 入口重置。新增的 `self._parse_file_path`/`self._stage_profile` 同样在 `__call__` 入口重置，与既有设计一致。当前"一进程一任务、顺序解析"下安全；**不支持对同一实例并发 `__call__`（既有限制，非本次引入）**——实现/未来改造时勿在多线程里共享同一 parser 实例。
4. **失败时的重复告警（可接受，保留）**：阶段抛错时 `_stage` 出一条 `status="error"` JSON，同时既有 `_concat_downward`/`_extract_table_figure` 的 `logging.exception` 仍出栈回溯。二者互补（结构化指标 + 堆栈），有利定位，保留不动。
5. **日志量与路径暴露（保留默认，记为风险）**：每个 PDF 至少多 9 行 INFO JSON；`file_path` 含 worker 临时路径（带原始 basename）。用户明确要 `file_path`，默认保留完整路径；若日志进外部系统且介意，可改记 `file_id`+basename 或把完整路径降为 debug 字段（本次不做）。
6. **测试收集（保持显式路径）**：`test_pdf_parse_timing.py` 不在 `pytest.ini` 的 `python_files` 白名单内（与既有 `test_pdf_parser_resilience.py` 一致），按计划用**显式文件路径**运行。**待你确认**：是否要把该文件名加入 `python_files` 以纳入 CI 默认收集（默认不加，遵循现有约定）。
7. **提交边界（commit 默认跳过）**：各 Task 的「提交」步骤已标注「可选、默认跳过，仅当你明确要求提交时执行」，避免按 checklist 误执行。未经你同意不自动提交。**当前工作区分支为 `fix/upload-filename-too-long`（非 `main`）**；若要提交，先与你确认目标分支/分支策略，并确认工作树无无关改动。
8. **（可选）adapter 级冒烟**：`__call__` 级测试已覆盖 8+1 条日志产出；如需更贴近生产，可另跑一次经 `PDFParserAdapter.parse()` 入口、stub 掉 8 个阶段方法的冒烟验证（非必需）。

**结论**：方案主体不变（上下文管理器包裹 8 阶段 + JSON + 分名 logger + finally 汇总），仅做字段统一、验收口径修正与风险说明；范围保持"仅 8 个阶段计时埋点"。

---

## Codex 二次评审结果（2026-06-24）

来源：Codex 对 Claude Code 修改后的当前方案、设计文档和相关代码事实进行第二轮静态审查。本节仅追加在文件末尾，未修改前文和已有代码。

### 二次结论

- **方案可继续推进**：Claude 已把上一轮指出的两个核心问题纳入计划：JSON 字段统一倾向 `file_path`，并明确 `LOG_LEVELS` 在当前 worker 的 `basicConfig` 路径下不生效。这两个调整降低了实现偏差和生产预期风险。
- **执行风险仍为中低**：实现仍限定在 `RAGFlowPdfParser.__call__` 的 8 个阶段外层包裹计时，不改解析算法、不改返回值、不写 trace/artifact/DB；对其它功能的主要影响仍是新增 INFO 日志。
- **执行前仍建议修正少量文档不一致**：当前计划正文基本已改为 `file_path`，但 spec 中仍残留 `file` 字段描述；计划里还保留多处 `git commit` 命令，虽然顶部说明可选，但 agentic worker 可能误执行。

### 已收敛的问题

- `file_path` 已在计划里的测试断言、实现草案和日志示例中统一，符合用户最初“带 file_path”的目标。
- `LOG_LEVELS` 已从硬性验收降级为说明项；这与当前 `openrag/src/openrag/worker/task_worker.py` 使用 `logging.basicConfig(...)` 的事实一致。
- parser 实例状态的边界被补充说明：新增 `_parse_file_path` / `_stage_profile` 属于单次解析状态，和既有 `self.boxes`、`self.page_images` 类似，只要不并发复用同一个 parser 实例，风险可控。
- 路径暴露、日志量、重复异常日志、测试显式路径运行等风险已被写入“实现注意点”，可作为后续执行约束。

### 新发现/仍需注意

- **spec 仍有字段名残留**：`docs/superpowers/specs/2026-06-24-pdf-parser-stage-timing-design.md` 中仍有 “JSON 里的 `file` / `task_id` / `file_id`”、测试断言 “带 `file`”、验收 “含 `file`” 等描述。计划末尾写“已同步 spec”，但当前文件事实并未完全同步。执行前应统一为 `file_path`，否则实现者可能按 plan 和 spec 读出两套字段名。
- **当前分支不是计划中写的 main**：本次审查时 `git branch --show-current` 返回 `fix/upload-filename-too-long`。计划里“当前在 main 分支，若提交先从 main 切特性分支”的表述不符合当前工作区状态。后续如果涉及提交或切分支，需要先确认分支策略，不能按该句直接操作。
- **commit 步骤仍容易被自动执行**：各 Task 的 Step 6/5/4 仍包含 `git add` / `git commit` 命令。虽然顶部和注意点已写“可选、未经同意不自动提交”，但对严格按 checklist 执行的 agent 来说仍有误导。更稳妥的做法是把这些步骤改成“如用户要求提交，再执行”。
- **`status="ok"` 的语义需要明确**：`_stage()` 只能捕获向外抛出的异常。像 `__images__` 内部已有 `try/except logging.exception(...)` 后继续执行的情况，阶段日志仍可能是 `ok`。这不是实现 bug，但应理解为“该阶段没有向外失败”，不是“阶段内部没有任何异常”。
- **adapter 级验证仍建议保留为非必需但有价值**：`PDFParserAdapter.parse()` 确实经 `self.ragflow_parser(file_path)` 进入 `RAGFlowPdfParser.__call__`。单测覆盖 `__call__` 已足够验证埋点主逻辑；若要降低集成偏差，追加一个 adapter 入口冒烟测试更稳。

### 边界确认

- 本方案仍不应扩大到 `parse_into_bboxes`、Docling/MinerU/PaddleOCR 等其它 PDF 入口，也不应顺手改全局 logging 初始化。
- 本方案不替代现有 `parse.document` trace span；它只是日志层面的阶段耗时细分。
- 本方案不保证日志字段被结构化日志系统自动索引；JSON 只是写入 message 字段，后续采集端是否解析是另一个问题。
- 本方案不解决日志降噪开关；当前 worker 默认会输出 `pdf.*` INFO，静音能力需另开任务处理。

### 最终意见

Claude 修改后的方案比上一版更可执行，主体设计可以接受。执行前建议至少修正 spec 中残留的 `file` 字段描述，并把 git 提交步骤改成明确的可选操作；否则最可能出问题的不是解析功能本身，而是文档字段不一致或执行者误提交。

---

## 二次评审的处理（2026-06-24）

针对上节 Codex 二次评审，已逐条落实（均已核对当前文件事实）：

1. **spec 字段名残留已清（已改）**：spec §3/§6/§7 残留的 `file` 全部改为 `file_path`（原第 52/190/198 行）。现 plan 与 spec 的 JSON 字段名完全一致，均为 `file_path`。（plan 正文中仍出现的 `file` 字样只在「Codex 评审结果」「二次评审结果」两节内——那是评审引用的历史记录，非字段定义，保留。）
2. **分支假设已修正（已改）**：顶部「执行前必读」与「实现注意点 #7」不再假设 `main`；写明当前工作区分支为 `fix/upload-filename-too-long`，提交前需与用户确认目标分支/分支策略。
3. **commit 步骤改为默认跳过（已改）**：各 Task 的「提交」步骤标题统一改为「（可选，默认跳过，仅当用户明确要求提交时执行）」，避免 agentic worker 按 checklist 误执行 `git commit`。
4. **`status="ok"` 语义已写明（已改）**：`_stage` docstring 与 spec §4.3 `status` 字段说明补充——`status="ok"` 仅表示「该阶段没有向外抛异常」，**不代表阶段内部没有被 try/except 吞掉的异常**（如 `__images__` 内部已有 `try/except logging.exception(...)` 后继续执行的情况，阶段仍记 `ok`）。
5. **adapter 级冒烟**：维持「非必需但有价值」，已在「实现注意点 #8」记录；`__call__` 级单测足以验证埋点主逻辑。

方案主体不变：上下文管理器包裹 `__call__` 的 8 个阶段 + 结构化 JSON（字段 `file_path`）+ 分名 `pdf.<stage>` logger + `finally` 汇总；范围仍限「仅 8 个阶段计时埋点」，不扩到 `parse_into_bboxes` / 其它 PDF 入口 / 全局 logging。

---

## Codex 三次评审结果（2026-06-24）

来源：Codex 对 Claude Code 再次修改后的当前 plan、spec 与工作区状态进行第三轮静态审查。本节仅追加在文件末尾，未修改前文、spec 或代码。

### 三次结论

- **本轮修改已处理二次评审的主要问题**：spec 中的 `file` 字段残留已改为 `file_path`；提交步骤已标成“可选、默认跳过”；当前分支不再被误写为 `main`；`status="ok"` 的语义也补充为“未向外抛异常”。
- **方案当前无新的阻断项**：作为“PDF RAGFlow parser 的 `__call__` 8 阶段日志级耗时观测”可以进入实现阶段。
- **剩余风险主要在执行阶段**：后续真正改代码时，应继续保持只包裹 8 个阶段、不改阶段内部逻辑、不改全局 logging、不自动提交。

### 当前已确认的边界

- plan 与 spec 的最终字段口径已统一为 `file_path`。
- `LOG_LEVELS` 仍明确为当前 worker 不生效；本方案不负责实现 worker 日志静音。
- `status="ok"` 已明确不是“阶段内部没有任何异常”，只表示该阶段没有向外抛异常。
- commit 命令仍保留在代码块里，但标题和顶部说明已写明默认跳过；执行者必须按“用户明确要求提交才提交”处理。
- spec 状态仍是 `待评审（Design）`，这与尚未实现的现状一致；Task 4 再更新为 `已实现` 是合理顺序。

### 仍需留意

- **历史评审段落存在过期结论**：前面的“Codex 评审结果”和“Codex 二次评审结果”是历史记录，里面仍会提到当时未修正的问题。后续执行时应以最新的“二次评审的处理”和本节为准。
- **当前文档已处于 staged 状态**：本轮审查开始时，plan 与 spec 已经是 `A` 状态。若此后继续追加内容而不重新 `git add`，会出现 index 与工作区内容不一致；这不是方案风险，但后续提交前必须重新核对 staged/unstaged 差异。
- **测试仍需显式路径运行**：`test_pdf_parse_timing.py` 不在 `pytest.ini` 默认白名单内，计划依赖显式文件路径运行，这是可以接受的，但 CI 默认是否覆盖仍取决于后续命令配置。
- **adapter 冒烟仍是加分项，不是阻断项**：`__call__` 单测足够覆盖核心埋点；如果后续实现者愿意增加 `PDFParserAdapter.parse()` 入口冒烟，可以降低集成路径偏差，但不应为了它扩大改动范围。

### 最终意见

Claude 最新修改后的方案已经具备可执行性。建议进入实现时按当前 plan 小步执行：先新增 `_stage` / `_emit_parse_profile` 测试与实现，再包裹 `__call__`，最后跑显式测试与 PDF 既有回归；全程不要改全局 logging、不要扩到其它 parser、不要在未经用户确认时提交。
