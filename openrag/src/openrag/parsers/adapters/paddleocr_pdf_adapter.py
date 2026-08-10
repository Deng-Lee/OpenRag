"""PaddleOCR PDF parser adapter."""

import json
import logging
import math
import os
import re
import time
import unicodedata
from contextlib import contextmanager
from typing import Any

import requests

from openrag.parsers.base import DocumentBlock, DocumentParser
from openrag.parsers.pdf_heading_hierarchy import (
    PdfHeadingHierarchyResolver,
    extract_pdf_outlines,
)
from openrag.ragflow_core.compat import to_document_blocks
from openrag.tracing.context import get_trace_context


DONE_STATUS = "done"
FAILED_STATUS = "failed"
NOT_FOUND_STATUS = "not_found"
PAGE_ASPECT_RATIO_REL_TOLERANCE = 0.01
LAYOUT_MATCH_THRESHOLD = 0.5
TITLE_LABELS = {"doc_title", "paragraph_title", "title"}
NON_CONTENT_LABELS = {
    "aside_text",
    "footer",
    "footer_image",
    "footnote",
    "header",
    "header_image",
    "number",
}


class PaddleOCRPDFParserAdapter(DocumentParser):
    supported_extensions = (".pdf",)

    def __init__(self):
        self.last_parse_stats = {
            "invalid_bbox_count": 0,
            "scaled_bbox_count": 0,
            "unmapped_bbox_count": 0,
            "filtered_block_count": 0,
            "filtered_labels": {},
            "layout_matched_count": 0,
            "markdown_heading_matched_count": 0,
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
            "filtered_block_count": 0,
            "filtered_labels": {},
            "layout_matched_count": 0,
            "markdown_heading_matched_count": 0,
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
                blocks = PdfHeadingHierarchyResolver().resolve(
                    blocks,
                    extract_pdf_outlines(file_path),
                )
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
) -> tuple[list[dict], dict[str, Any]]:
    rows: list[dict] = []
    stream_cursor = 0
    invalid_bbox_count = 0
    scaled_bbox_count = 0
    unmapped_bbox_count = 0
    filtered_labels: dict[str, int] = {}
    layout_matched_count = 0
    markdown_heading_matched_count = 0

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
        parsing_items = page.get("parsing_res_list") or []
        layout_evidence = _match_layout_evidence(page, parsing_items)
        markdown_evidence = _match_markdown_headings(page, parsing_items)
        for block_index, item in enumerate(parsing_items):
            label = _item_label(item)
            if label in NON_CONTENT_LABELS:
                filtered_labels[label] = filtered_labels.get(label, 0) + 1
                continue

            text = item.get("content", item.get("block_content", ""))
            text = str(text).strip()
            if not text:
                continue

            block_type, layout_type = _paddle_block_types(label)
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
            metadata: dict[str, Any] = {
                "ocr_label": label,
                "paddleocr_order": block_index,
                "parser_backend": "paddleocr",
                "structured_pdf": True,
            }
            if label in TITLE_LABELS:
                metadata["heading_candidate"] = True
            if label == "doc_title":
                metadata["heading_role"] = "document_title"
                metadata["hard_boundary"] = False
            evidence = layout_evidence.get(block_index)
            if evidence:
                metadata.update(evidence)
                layout_matched_count += 1
            markdown = markdown_evidence.get(block_index)
            if markdown:
                metadata.update(markdown)
                markdown_heading_matched_count += 1
            polygon_points = item.get("polygon_points")
            if isinstance(polygon_points, list):
                metadata["polygon_points"] = polygon_points
            group_id = item.get("group_id")
            if group_id is not None:
                metadata["paddleocr_group_id"] = group_id
            if bbox is not None and pdf_page_size is not None:
                page_width, page_height = pdf_page_size
                x0, y0, x1, y1 = bbox
                metadata.update(
                    {
                        "style_source": "paddle_bbox",
                        "geometry_height_ratio": (y1 - y0) / page_height,
                        "geometry_left_ratio": x0 / page_width,
                        "geometry_width_ratio": (x1 - x0) / page_width,
                        "geometry_center_offset_ratio": abs(
                            ((x0 + x1) / 2) - (page_width / 2)
                        )
                        / page_width,
                    }
                )
            rows.append(
                {
                    "text": text,
                    "page": page_index,
                    "offset": len(rows),
                    "bbox": bbox,
                    "block_type": block_type,
                    "level": 0,
                    "layout_type": layout_type,
                    "block_id": f"paddleocr:page:{page_index}:block:{len(rows)}",
                    "char_start": stream_cursor,
                    "char_end": stream_cursor + len(text),
                    "table_data": (
                        {"html": text}
                        if block_type == "table"
                        and text.lstrip().lower().startswith("<table")
                        else None
                    ),
                    "metadata": metadata,
                }
            )
            stream_cursor += len(text) + 2

    _normalize_markdown_levels(rows)
    return rows, {
        "invalid_bbox_count": invalid_bbox_count,
        "scaled_bbox_count": scaled_bbox_count,
        "unmapped_bbox_count": unmapped_bbox_count,
        "filtered_block_count": sum(filtered_labels.values()),
        "filtered_labels": dict(sorted(filtered_labels.items())),
        "layout_matched_count": layout_matched_count,
        "markdown_heading_matched_count": markdown_heading_matched_count,
    }


def _item_label(item: dict) -> str:
    value = item.get("label", item.get("block_label", "text"))
    return str(value or "text").strip().lower()


def _paddle_block_types(label: str) -> tuple[str, str]:
    if label in TITLE_LABELS:
        return "text", "title"
    if label == "table":
        return "table", "table"
    if label in {"image", "figure", "chart"}:
        return "image", label
    return "text", label or "text"


def _match_layout_evidence(page: dict, parsing_items: list[dict]) -> dict[int, dict]:
    layout_result = page.get("layout_det_res") or {}
    boxes = layout_result.get("boxes") or []
    normalized_boxes: list[tuple[int, str, tuple[float, float, float, float], dict]] = []
    for box_index, box in enumerate(boxes):
        bbox = _normalize_bbox(box.get("coordinate", box.get("bbox")))
        if bbox is None:
            continue
        normalized_boxes.append((box_index, _item_label(box), bbox, box))

    matches: dict[int, dict] = {}
    used: set[int] = set()
    for item_index, item in enumerate(parsing_items):
        item_bbox = _normalize_bbox(item.get("bbox", item.get("block_bbox")))
        if item_bbox is None:
            continue
        label_group = _layout_label_group(_item_label(item))
        candidates: list[tuple[float, int, dict]] = []
        for box_index, box_label, box_bbox, box in normalized_boxes:
            if box_index in used or _layout_label_group(box_label) != label_group:
                continue
            match_score = _bbox_overlap_score(item_bbox, box_bbox)
            if match_score >= LAYOUT_MATCH_THRESHOLD:
                candidates.append((match_score, box_index, box))
        if not candidates:
            continue

        best_match, _best_index, best_box = max(
            candidates,
            key=lambda value: (value[0], _safe_float(value[2].get("score")) or 0.0),
        )
        used.update(box_index for _score, box_index, _box in candidates)
        scores = [
            value
            for _match, _index, box in candidates
            if (value := _safe_float(box.get("score"))) is not None
        ]
        evidence: dict[str, Any] = {
            "layout_match_iou": round(best_match, 4),
            "layout_match_count": len(candidates),
        }
        if scores:
            evidence["layout_score"] = max(scores)
        if best_box.get("order") is not None:
            evidence["paddleocr_layout_order"] = best_box["order"]
        matches[item_index] = evidence
    return matches


def _layout_label_group(label: str) -> str:
    if label in {"paragraph_title", "title"}:
        return "title"
    return label


def _bbox_overlap_score(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    x0 = max(left[0], right[0])
    y0 = max(left[1], right[1])
    x1 = min(left[2], right[2])
    y1 = min(left[3], right[3])
    if x0 >= x1 or y0 >= y1:
        return 0.0
    intersection = (x1 - x0) * (y1 - y0)
    left_area = (left[2] - left[0]) * (left[3] - left[1])
    right_area = (right[2] - right[0]) * (right[3] - right[1])
    return intersection / min(left_area, right_area)


def _match_markdown_headings(page: dict, parsing_items: list[dict]) -> dict[int, dict]:
    markdown = page.get("markdown") or {}
    text = str(markdown.get("markdown_texts") or "")
    headings: list[tuple[int, str]] = []
    for line in text.splitlines():
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match:
            headings.append((len(match.group(1)), match.group(2)))

    matches: dict[int, dict] = {}
    next_item = 0
    for level, heading_text in headings:
        normalized_heading = _normalize_match_text(heading_text)
        for item_index in range(next_item, len(parsing_items)):
            item = parsing_items[item_index]
            if _item_label(item) not in TITLE_LABELS:
                continue
            content = item.get("content", item.get("block_content", ""))
            if _normalize_match_text(str(content)) != normalized_heading:
                continue
            matches[item_index] = {
                "paddle_markdown_level_raw": level,
                "paddle_markdown_match_score": 1.0,
            }
            next_item = item_index + 1
            break
    return matches


def _normalize_markdown_levels(rows: list[dict]) -> None:
    section_levels = [
        int(metadata["paddle_markdown_level_raw"])
        for row in rows
        if (metadata := row.get("metadata") or {}).get("paddle_markdown_level_raw")
        and metadata.get("heading_role") != "document_title"
    ]
    offset = min(section_levels) - 1 if section_levels else 0
    for row in rows:
        metadata = row.get("metadata") or {}
        raw_level = metadata.get("paddle_markdown_level_raw")
        if raw_level is None:
            continue
        if metadata.get("heading_role") == "document_title":
            metadata["paddle_markdown_level"] = 0
        else:
            metadata["paddle_markdown_level"] = max(1, int(raw_level) - offset)


def _normalize_match_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", normalized)


def _safe_float(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


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
