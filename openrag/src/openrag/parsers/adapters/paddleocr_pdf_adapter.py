"""PaddleOCR PDF parser adapter."""

import json
import logging
import math
import os
import time
from contextlib import contextmanager

import requests

from openrag.parsers.base import DocumentBlock, DocumentParser
from openrag.ragflow_core.compat import to_document_blocks
from openrag.tracing.context import get_trace_context


DONE_STATUS = "done"
FAILED_STATUS = "failed"
NOT_FOUND_STATUS = "not_found"
PAGE_ASPECT_RATIO_REL_TOLERANCE = 0.01


class PaddleOCRPDFParserAdapter(DocumentParser):
    supported_extensions = (".pdf",)

    def __init__(self):
        self.last_parse_stats = {
            "invalid_bbox_count": 0,
            "scaled_bbox_count": 0,
            "unmapped_bbox_count": 0,
        }
        self.last_parse_profile = None
        self._parse_file_path = None
        self._stage_profile = []
        self._poll_count = 0

    def supports(self, file_path: str) -> bool:
        return file_path.lower().endswith(self.supported_extensions)

    def parse(self, file_path: str) -> list[DocumentBlock]:
        self._parse_file_path = file_path
        self._stage_profile = []
        self._poll_count = 0
        self.last_parse_profile = None
        self.last_parse_stats = {
            "invalid_bbox_count": 0,
            "scaled_bbox_count": 0,
            "unmapped_bbox_count": 0,
        }
        started_at = time.monotonic()
        page_count = None
        block_count = None
        n_tables = None

        server_url = os.getenv("PADDLEOCR_SERVER_URL")
        try:
            _assert_pdf_magic_file(file_path)
            if not server_url:
                raise RuntimeError(
                    "PADDLEOCR_SERVER_URL is required when using PaddleOCR PDF parser"
                )

            request_timeout = _float_env("PADDLEOCR_REQUEST_TIMEOUT", 120.0)
            task_id = self._submit(file_path, server_url, request_timeout)
            payload = self._poll(server_url, task_id, request_timeout)
            result = self._fetch_result(payload)
            page_count = _safe_len(result.get("pages") or [])
            with self._stage("paddleocr.convert_blocks"):
                pdf_page_sizes = _read_pdf_page_sizes(file_path)
                raw_rows, stats = _result_to_rows(result, pdf_page_sizes)
                blocks = to_document_blocks(raw_rows, source="paddleocr")
            block_count = len(blocks)
            n_tables = sum(1 for block in blocks if block.block_type == "table")
            self.last_parse_stats = stats
            self._emit_parse_profile(
                total_ms=_duration_ms(started_at),
                page_count=page_count,
                block_count=block_count,
                n_tables=n_tables,
                status="ok",
            )
            return blocks
        except Exception:
            self._emit_parse_profile(
                total_ms=_duration_ms(started_at),
                page_count=page_count,
                block_count=block_count,
                n_tables=n_tables,
                status="error",
            )
            raise

    def _submit(self, file_path: str, server_url: str, request_timeout: float) -> str:
        url = f"{server_url.rstrip('/')}/paddleocr/async/ocr"
        filename = os.path.basename(file_path)
        with self._stage("paddleocr.submit"):
            with open(file_path, "rb") as file_obj:
                response = requests.post(
                    url,
                    files={"image": (filename, file_obj)},
                    data={"doc_orientation": "True"},
                    timeout=request_timeout,
                )
            response.raise_for_status()
            payload = response.json()
            task_id = payload.get("task_id")
            if not task_id:
                raise RuntimeError("PaddleOCR submit response missing task_id")
            return str(task_id)

    def _poll(self, server_url: str, task_id: str, request_timeout: float) -> dict:
        url = f"{server_url.rstrip('/')}/paddleocr/async/task/{task_id}"
        poll_timeout = _float_env("PADDLEOCR_POLL_TIMEOUT", 600.0)
        poll_interval = _float_env("PADDLEOCR_POLL_INTERVAL", 3.0)
        deadline = time.monotonic() + poll_timeout

        with self._stage("paddleocr.poll"):
            while time.monotonic() <= deadline:
                self._poll_count += 1
                response = requests.get(url, timeout=request_timeout)
                response.raise_for_status()
                payload = response.json()
                status = str(payload.get("status", "")).lower()
                if status == DONE_STATUS:
                    return payload
                if status in {FAILED_STATUS, NOT_FOUND_STATUS}:
                    safe_summary = f"PaddleOCR task {status}"
                    raise _paddleocr_error(
                        safe_summary,
                        safe_summary,
                    )
                time.sleep(poll_interval)

            raise _paddleocr_error(
                "PaddleOCR task timed out",
                "PaddleOCR task timed out",
            )

    def _fetch_result(self, payload: dict) -> dict:
        with self._stage("paddleocr.fetch_result"):
            return payload.get("result") or {}

    def _trace_fields(self) -> dict:
        try:
            ctx = get_trace_context() or {}
        except Exception:
            ctx = {}
        return {
            "task_id": ctx.get("task_id"),
            "file_id": ctx.get("file_id"),
            "file_path": self._parse_file_path,
        }

    @contextmanager
    def _stage(self, stage: str):
        started_at = time.monotonic()
        status = "ok"
        error = None
        try:
            yield
        except Exception as exc:
            status = "error"
            error = exc
            raise
        finally:
            item = {
                "stage": stage,
                "duration_ms": _duration_ms(started_at),
                "status": status,
            }
            record = {"evt": stage, **self._trace_fields(), **item}
            if error is not None:
                record["error_type"] = type(error).__name__
                record["error_summary"] = _error_summary(error)
                item["error_type"] = record["error_type"]
                item["error_summary"] = record["error_summary"]
            self._stage_profile.append(item)
            logging.getLogger(stage).info(
                json.dumps(record, ensure_ascii=False, default=str)
            )

    def _emit_parse_profile(
        self,
        *,
        total_ms: int,
        page_count: int | None,
        block_count: int | None,
        n_tables: int | None,
        status: str,
    ) -> None:
        self.last_parse_profile = {
            **self._trace_fields(),
            "total_ms": total_ms,
            "page_count": page_count,
            "block_count": block_count,
            "n_tables": n_tables,
            "poll_count": self._poll_count,
            **self.last_parse_stats,
            "status": status,
            "stages": list(self._stage_profile),
        }
        log_record = {
            "evt": "paddleocr.parse_profile",
            "total_ms": total_ms,
            "page_count": page_count,
            "block_count": block_count,
            "n_tables": n_tables,
            "poll_count": self._poll_count,
            "status": status,
            "stages": list(self._stage_profile),
        }
        logging.getLogger("paddleocr.parse_profile").info(
            json.dumps(log_record, ensure_ascii=False, default=str)
        )


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    return float(raw)


def _duration_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)


def _safe_len(value) -> int | None:
    try:
        return len(value)
    except TypeError:
        return None


def _error_summary(exc: Exception) -> str:
    return getattr(exc, "safe_summary", None) or str(exc)[:200]


def _paddleocr_error(message: str, safe_summary: str) -> RuntimeError:
    exc = RuntimeError(message)
    exc.safe_summary = safe_summary
    return exc


def _assert_pdf_magic_file(file_path: str) -> None:
    try:
        with open(file_path, "rb") as file_obj:
            header = file_obj.read(5)
    except OSError as exc:
        raise RuntimeError("PaddleOCR PDF parser could not read input file") from exc
    if header != b"%PDF-":
        raise RuntimeError("PaddleOCR PDF parser only supports PDF files")


def _read_pdf_page_sizes(
    file_path: str,
) -> list[tuple[float, float] | None]:
    try:
        from pypdf import PdfReader

        reader = PdfReader(file_path)
        page_count = len(reader.pages)
    except Exception:
        return []

    page_sizes: list[tuple[float, float] | None] = []
    for page_index in range(page_count):
        try:
            page = reader.pages[page_index]
            width = float(page.cropbox.width)
            height = float(page.cropbox.height)
            rotation = int(page.get("/Rotate", 0) or 0) % 360
        except Exception:
            page_sizes.append(None)
            continue
        if rotation in {90, 270}:
            width, height = height, width
        if not _valid_page_size(width, height):
            page_sizes.append(None)
            continue
        page_sizes.append((width, height))
    return page_sizes


def _result_to_rows(
    result: dict,
    pdf_page_sizes: list[tuple[float, float] | None],
) -> tuple[list[dict], dict[str, int]]:
    rows: list[dict] = []
    stream_cursor = 0
    invalid_bbox_count = 0
    scaled_bbox_count = 0
    unmapped_bbox_count = 0

    for page_index, page in enumerate(result.get("pages") or [], start=1):
        pdf_page_size = (
            pdf_page_sizes[page_index - 1]
            if page_index <= len(pdf_page_sizes)
            else None
        )
        ocr_width = page.get("width")
        ocr_height = page.get("height")
        preprocessor = page.get("doc_preprocessor_res") or {}
        angle = preprocessor.get("angle", 0)
        for block_index, item in enumerate(page.get("parsing_res_list") or []):
            text = item.get("content", item.get("block_content", ""))
            text = str(text).strip()
            if not text:
                continue

            label = item.get("label", item.get("block_label", "text"))
            label = str(label or "text")
            block_type = "table" if label == "table" else "text"
            raw_bbox = item.get("bbox", item.get("block_bbox"))
            bbox = _normalize_bbox(raw_bbox)
            if raw_bbox is not None and bbox is None:
                invalid_bbox_count += 1
            elif bbox is not None:
                bbox = _scale_bbox_to_pdf(
                    bbox,
                    ocr_width=ocr_width,
                    ocr_height=ocr_height,
                    pdf_page_size=pdf_page_size,
                    angle=angle,
                )
                if bbox is None:
                    unmapped_bbox_count += 1
                else:
                    scaled_bbox_count += 1
            rows.append(
                {
                    "text": text,
                    "page": page_index,
                    "offset": len(rows),
                    "bbox": bbox,
                    "block_type": block_type,
                    "level": 0,
                    "layout_type": block_type,
                    "block_id": f"paddleocr:page:{page_index}:block:{len(rows)}",
                    "char_start": stream_cursor,
                    "char_end": stream_cursor + len(text),
                    "metadata": {"ocr_label": label},
                }
            )
            stream_cursor += len(text) + 2

    return rows, {
        "invalid_bbox_count": invalid_bbox_count,
        "scaled_bbox_count": scaled_bbox_count,
        "unmapped_bbox_count": unmapped_bbox_count,
    }


def _scale_bbox_to_pdf(
    bbox: tuple[float, float, float, float],
    *,
    ocr_width,
    ocr_height,
    pdf_page_size: tuple[float, float] | None,
    angle,
) -> tuple[float, float, float, float] | None:
    if pdf_page_size is None or not _zero_angle(angle):
        return None
    try:
        source_width = float(ocr_width)
        source_height = float(ocr_height)
    except (TypeError, ValueError):
        return None
    target_width, target_height = pdf_page_size
    if not _valid_page_size(source_width, source_height):
        return None
    if not math.isclose(
        source_width / source_height,
        target_width / target_height,
        rel_tol=PAGE_ASPECT_RATIO_REL_TOLERANCE,
    ):
        return None

    scale_x = target_width / source_width
    scale_y = target_height / source_height
    x0, y0, x1, y1 = bbox
    return _clamp_bbox_to_page(
        (x0 * scale_x, y0 * scale_y, x1 * scale_x, y1 * scale_y),
        target_width,
        target_height,
    )


def _clamp_bbox_to_page(
    bbox: tuple[float, float, float, float],
    page_width: float,
    page_height: float,
) -> tuple[float, float, float, float] | None:
    x0, y0, x1, y1 = bbox
    x0 = min(max(x0, 0.0), page_width)
    x1 = min(max(x1, 0.0), page_width)
    y0 = min(max(y0, 0.0), page_height)
    y1 = min(max(y1, 0.0), page_height)
    if x0 >= x1 or y0 >= y1:
        return None
    return (x0, y0, x1, y1)


def _valid_page_size(width: float, height: float) -> bool:
    return (
        math.isfinite(width)
        and math.isfinite(height)
        and width > 0
        and height > 0
    )


def _zero_angle(value) -> bool:
    try:
        angle = float(value or 0) % 360
    except (TypeError, ValueError):
        return False
    return math.isfinite(angle) and angle == 0


def _normalize_bbox(value) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        bbox = (float(value[0]), float(value[1]), float(value[2]), float(value[3]))
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(item) for item in bbox):
        return None
    x0, y0, x1, y1 = bbox
    if x0 >= x1 or y0 >= y1:
        return None
    return bbox
