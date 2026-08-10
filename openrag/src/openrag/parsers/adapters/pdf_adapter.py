"""PDF parser adapter (RAGFlow deepdoc only)."""

from io import BytesIO
import logging
import re

from openrag.parsers.adapters.base_adapter import RAGFlowParserAdapter
from openrag.parsers.base import DocumentBlock
from openrag.parsers.pdf_heading_hierarchy import PdfHeadingHierarchyResolver
from openrag.ragflow_core.compat import to_document_blocks

logger = logging.getLogger(__name__)

_RAGFLOW_IMPORT_ERROR: Exception | None = None
try:
    from openrag.parsers.ragflow.parser.pdf_parser import RAGFlowPdfParser  # type: ignore
except Exception as exc:  # pragma: no cover - depends on optional ragflow deps
    RAGFlowPdfParser = None  # type: ignore[assignment]
    _RAGFLOW_IMPORT_ERROR = exc

# RAGFlow _line_tag 理论上用 \\t，但 JSON/日志/部分管线会把制表符变成空格，
# 导致 extract_positions（只认 \\t）匹配失败、remove_tag 也去不掉标签。
_PDF_POS_TAG_SP = re.compile(
    r"@@(?P<page>[0-9]+(?:-[0-9]+)?)\s+"
    r"(?P<x0>[0-9.]+)\s+"
    r"(?P<x1>[0-9.]+)\s+"
    r"(?P<top>[0-9.]+)\s+"
    r"(?P<bottom>[0-9.]+)##"
)


def _extract_pdf_position_tags(line: str) -> list[dict[str, float | int]]:
    tags: list[dict[str, float | int]] = []
    for match in _PDF_POS_TAG_SP.finditer(line):
        tags.append(
            {
                "page": int(match.group("page").split("-")[0]),
                "x0": float(match.group("x0")),
                "x1": float(match.group("x1")),
                "top": float(match.group("top")),
                "bottom": float(match.group("bottom")),
            }
        )
    return tags


def _plain_line_from_tagged_line(line: str) -> str:
    plain = _PDF_POS_TAG_SP.sub("", line)
    if RAGFlowPdfParser is not None:
        plain = RAGFlowPdfParser.remove_tag(plain)
    return plain.strip()


def _tagged_paragraph_to_plain_bbox_and_lines(
    para: str,
    abs_start: int = 0,
) -> tuple[
    str,
    int,
    tuple[float, float, float, float] | None,
    list[dict[str, float | int | str]],
]:
    plain_lines: list[str] = []
    line_positions: list[dict[str, float | int | str]] = []
    paragraph_tags: list[dict[str, float | int]] = []
    char_cursor = abs_start

    for raw_line in para.splitlines() or [para]:
        plain_line = _plain_line_from_tagged_line(raw_line)
        line_tags = _extract_pdf_position_tags(raw_line)
        plain_lines.append(plain_line)
        paragraph_tags.extend(line_tags)

        if plain_line and line_tags:
            line_page = int(line_tags[0]["page"])
            same_page_tags = [tag for tag in line_tags if tag["page"] == line_page]
            line_positions.append(
                {
                    "page": line_page,
                    "x0": min(float(tag["x0"]) for tag in same_page_tags),
                    "x1": max(float(tag["x1"]) for tag in same_page_tags),
                    "top": min(float(tag["top"]) for tag in same_page_tags),
                    "bottom": max(float(tag["bottom"]) for tag in same_page_tags),
                    "char_start": char_cursor,
                    "char_end": char_cursor + len(plain_line),
                    "text": plain_line,
                }
            )
        char_cursor += len(plain_line) + 1

    plain = "\n".join(plain_lines).strip()
    if not paragraph_tags:
        return plain, 1, None, []

    primary_1based = int(paragraph_tags[0]["page"])
    same_page_tags = [tag for tag in paragraph_tags if tag["page"] == primary_1based]
    bbox = (
        min(float(tag["x0"]) for tag in same_page_tags),
        min(float(tag["top"]) for tag in same_page_tags),
        max(float(tag["x1"]) for tag in same_page_tags),
        max(float(tag["bottom"]) for tag in same_page_tags),
    )
    return plain, primary_1based, bbox, line_positions


def _page_bbox_from_ragflow_table_positions(
    poss: list[tuple[float, ...]],
) -> tuple[int, tuple[float, float, float, float] | None, dict[int, tuple[float, float, float, float]]]:
    """RAGFlow ``_extract_table_figure(..., need_position=True)`` 中 cropout 写入的 poss。

    每项为 ``(pn_abs, left, right, top, bottom)``：``pn_abs`` 为整份 PDF 内从 0 起的页下标；
    坐标为版面坐标，与正文 ``@@`` 标签一致（原点左上，x 右、y 下）。

    跨页表格每页有独立的页内坐标，不能跨页混合 min/max。
    返回 ``(primary_page_1based, primary_bbox, per_page_bboxes)``：
    - ``primary_bbox`` 仅基于首页（``p_min``）的矩形并集；
    - ``per_page_bboxes`` 为 ``{page_1based: (x0, y0, x1, y1)}``，供多页高亮使用。
    """
    if not poss:
        return 1, None, {}
    by_page: dict[int, list[tuple[float, float, float, float]]] = {}
    for raw in poss:
        if len(raw) < 5:
            continue
        pn_abs = int(raw[0])
        left, right, top, bott = (
            float(raw[1]),
            float(raw[2]),
            float(raw[3]),
            float(raw[4]),
        )
        by_page.setdefault(pn_abs, []).append((left, top, right, bott))
    if not by_page:
        return 1, None, {}
    p_min = min(by_page)

    per_page_bboxes: dict[int, tuple[float, float, float, float]] = {}
    for pg, rects in by_page.items():
        per_page_bboxes[pg + 1] = (
            min(r[0] for r in rects),
            min(r[1] for r in rects),
            max(r[2] for r in rects),
            max(r[3] for r in rects),
        )

    primary_bbox = per_page_bboxes[p_min + 1]
    return p_min + 1, primary_bbox, per_page_bboxes


def _unpack_ragflow_table_item(
    tbl_item: tuple,
) -> tuple[object, object, list[tuple[float, ...]]]:
    """兼容旧格式 ``(img, table_data)`` 与新格式 ``((img, table_data), poss)``。"""
    if (
        len(tbl_item) == 2
        and isinstance(tbl_item[0], tuple)
        and len(tbl_item[0]) == 2
        and isinstance(tbl_item[1], list)
    ):
        img, table_data = tbl_item[0]
        poss_raw = tbl_item[1]
        poss: list[tuple[float, ...]] = []
        for row in poss_raw:
            if isinstance(row, (tuple, list)) and len(row) >= 5:
                poss.append(tuple(float(x) for x in row[:5]))
        return img, table_data, poss
    return tbl_item[0], tbl_item[1], []


def _structured_box_page_bbox(
    box: dict,
) -> tuple[
    int,
    tuple[float, float, float, float] | None,
    dict[int, tuple[float, float, float, float]],
]:
    """Return 1-based page and page-local bbox from ``parse_into_bboxes`` output."""
    by_page: dict[int, list[tuple[float, float, float, float]]] = {}
    for raw in box.get("positions") or []:
        if not isinstance(raw, (tuple, list)) or len(raw) < 5:
            continue
        try:
            page = int(raw[0])
            left, right, top, bottom = map(float, raw[1:5])
        except (TypeError, ValueError):
            continue
        by_page.setdefault(page, []).append((left, top, right, bottom))

    primary_page = int(box.get("page_number") or 1)
    if not by_page:
        try:
            bbox = (
                float(box["x0"]),
                float(box["top"]),
                float(box["x1"]),
                float(box["bottom"]),
            )
        except (KeyError, TypeError, ValueError):
            bbox = None
        return primary_page, bbox, {}

    per_page = {
        page: (
            min(rect[0] for rect in rects),
            min(rect[1] for rect in rects),
            max(rect[2] for rect in rects),
            max(rect[3] for rect in rects),
        )
        for page, rects in by_page.items()
    }
    if primary_page not in per_page:
        primary_page = min(per_page)
    return primary_page, per_page[primary_page], per_page


def _structured_image_bytes(image: object) -> bytes | None:
    if isinstance(image, bytes):
        return image
    save = getattr(image, "save", None)
    if not callable(save):
        return None
    buffer = BytesIO()
    try:
        save(buffer, format="PNG")
    except Exception:
        logger.debug("Failed to serialize DeepDoc block image", exc_info=True)
        return None
    return buffer.getvalue()


def _deepdoc_boxes_to_document_blocks(boxes: list[dict]) -> list[DocumentBlock]:
    raw_rows: list[dict] = []
    stream_cursor = 0
    for index, box in enumerate(boxes or []):
        text = str(box.get("text") or "").strip()
        layout_type = str(box.get("layout_type") or "text").lower()
        if not text and layout_type not in {"figure", "image"}:
            continue

        page, bbox, per_page_bboxes = _structured_box_page_bbox(box)
        if layout_type == "table":
            block_type = "table"
        elif layout_type in {"figure", "image"}:
            block_type = "image"
        else:
            block_type = "text"

        metadata: dict = {
            "structured_pdf": True,
            "deepdoc_order": index,
        }
        if box.get("layoutno") is not None:
            metadata["deepdoc_layoutno"] = str(box["layoutno"])
        layout_score = box.get("layout_score", box.get("score"))
        if layout_score is not None:
            metadata["deepdoc_layout_score"] = float(layout_score)
        if box.get("positions"):
            metadata["positions"] = [list(pos) for pos in box["positions"]]
        if len(per_page_bboxes) > 1:
            metadata["page_bboxes"] = {
                str(pg): list(value) for pg, value in sorted(per_page_bboxes.items())
            }
        for key in (
            "font_size_median",
            "font_name_mode",
            "bold_ratio",
            "char_height_median",
            "char_count",
            "style_source",
        ):
            if box.get(key) is not None:
                metadata[key] = box[key]

        raw_rows.append(
            {
                "text": text,
                "page": page,
                "offset": len(raw_rows),
                "bbox": bbox,
                "block_type": block_type,
                "level": 0,
                "layout_type": layout_type,
                "image": _structured_image_bytes(box.get("image")),
                "table_data": (
                    {"html": text}
                    if layout_type == "table" and text.lstrip().lower().startswith("<table")
                    else None
                ),
                "block_id": f"ragflow:{layout_type}:{index}",
                "char_start": stream_cursor,
                "char_end": stream_cursor + len(text),
                "metadata": metadata,
            }
        )
        stream_cursor += len(text) + 2
    return to_document_blocks(raw_rows, source="pdf")


def _plain_page_bbox_from_tagged_paragraph(
    para: str,
) -> tuple[str, int, tuple[float, float, float, float] | None]:
    """从 RAGFlow 段落中剥离 @@页 + x0/x1/top/bottom + ##，得到正文、页码（1-based）、页面内 bbox。

    支持分隔符为 **空格或制表符**（与 RAGFlow 原始 \\t 输出兼容）。
    bbox：原点左上，x 向右、y 向下，与 react-pdf 视口一致。
    """
    plain, page, bbox, _line_positions = _tagged_paragraph_to_plain_bbox_and_lines(
        para
    )
    if bbox is not None:
        return plain, page, bbox

    if RAGFlowPdfParser is None:
        return plain, 1, None
    positions = RAGFlowPdfParser.extract_positions(para)
    if not positions:
        return plain, 1, None
    primary = positions[0][0][0] if positions[0][0] else 0
    page_1based = primary + 1
    xs0 = []
    xs1 = []
    ys0 = []
    ys1 = []
    for pns, left, right, top, bottom in positions:
        if not pns or pns[0] != primary:
            continue
        xs0.append(float(left))
        xs1.append(float(right))
        ys0.append(float(top))
        ys1.append(float(bottom))
    if not xs0:
        left, right, top, bottom = (
            positions[0][1],
            positions[0][2],
            positions[0][3],
            positions[0][4],
        )
        bbox = (float(left), float(top), float(right), float(bottom))
    else:
        bbox = (min(xs0), min(ys0), max(xs1), max(ys1))
    return plain, page_1based, bbox


class PDFParserAdapter(RAGFlowParserAdapter):
    supported_extensions = (".pdf",)

    def __init__(self):
        super().__init__()
        self.ragflow_parser = (
            RAGFlowPdfParser() if RAGFlowPdfParser is not None else None
        )
        # Per-stage timing profile from the most recent parse (read by the
        # pipeline to persist into the parse.document trace span). None until
        # a parse runs; None for non-PDF parsers.
        self.last_parse_profile = None

    def parse(self, file_path: str) -> list[DocumentBlock]:
        if self.ragflow_parser is None:
            raise RuntimeError(
                f"RAGFlow deepdoc backend unavailable: {_RAGFLOW_IMPORT_ERROR}"
            )
        return self._parse_ragflow_structured(file_path)

    def _parse_ragflow_structured(self, file_path: str) -> list[DocumentBlock]:
        if self.ragflow_parser is None:
            raise RuntimeError(f"RAGFlow backend unavailable: {_RAGFLOW_IMPORT_ERROR}")
        parse_into_bboxes = getattr(self.ragflow_parser, "parse_into_bboxes", None)
        if not callable(parse_into_bboxes):
            return self._parse_ragflow_with_tables(file_path)

        logger.info("[pdf_adapter] calling structured ragflow parser for file=%s", file_path)
        try:
            boxes = parse_into_bboxes(file_path)
        except Exception:
            logger.exception(
                "[pdf_adapter] structured ragflow parser FAILED for file=%s",
                file_path,
            )
            raise
        finally:
            self.last_parse_profile = getattr(
                self.ragflow_parser, "_last_parse_profile", None
            )

        blocks = _deepdoc_boxes_to_document_blocks(boxes)
        outlines = getattr(self.ragflow_parser, "outlines", [])
        return PdfHeadingHierarchyResolver().resolve(blocks, outlines)

    def _parse_ragflow_with_tables(self, file_path: str) -> list[DocumentBlock]:
        if self.ragflow_parser is None:
            raise RuntimeError(f"RAGFlow backend unavailable: {_RAGFLOW_IMPORT_ERROR}")
        logger.info("[pdf_adapter] calling ragflow_parser for file=%s", file_path)
        try:
            text_body, tbls = self.ragflow_parser(file_path)
        except Exception as exc:
            logger.exception(
                "[pdf_adapter] ragflow_parser FAILED for file=%s", file_path
            )
            raise
        finally:
            # Copy the profile on BOTH success and failure: __call__ writes
            # _last_parse_profile in its own finally (status="error" on
            # failure), so failed parses stay queryable via the trace span.
            self.last_parse_profile = getattr(
                self.ragflow_parser, "_last_parse_profile", None
            )
        logger.info(
            "[pdf_adapter] ragflow_parser returned: text_body_len=%s, tbls=%s",
            len(text_body) if text_body else 0,
            len(tbls) if tbls else 0,
        )

        raw_rows: list[dict] = []
        idx = 0
        stream_cursor = 0

        if text_body and text_body.strip():
            for para in text_body.split("\n\n"):
                raw_para = para.strip()
                if not raw_para:
                    continue
                text_plain, page_n, bbox, line_positions = (
                    _tagged_paragraph_to_plain_bbox_and_lines(
                        raw_para,
                        abs_start=stream_cursor,
                    )
                )
                if not text_plain:
                    continue
                text_meta = (
                    {"line_positions": line_positions} if line_positions else None
                )
                raw_rows.append(
                    {
                        "text": text_plain,
                        "page": page_n,
                        "offset": idx,
                        "bbox": bbox,
                        "block_type": "text",
                        "level": 0,
                        "layout_type": "text",
                        "block_id": f"ragflow:text:{idx}",
                        "char_start": stream_cursor,
                        "char_end": stream_cursor + len(text_plain),
                        "metadata": text_meta,
                    }
                )
                stream_cursor += len(text_plain) + 2
                idx += 1

        for ti, tbl_item in enumerate(tbls or []):
            img, table_data, table_poss = _unpack_ragflow_table_item(tbl_item)
            logger.info(
                "[pdf_adapter] table[%s]: img_type=%s, data_type=%s, poss_len=%s, poss=%s",
                ti,
                type(img).__name__,
                type(table_data).__name__,
                len(table_poss),
                table_poss[:3] if table_poss else "[]",
            )
            if isinstance(table_data, list):
                table_text = "\n".join(str(c) for c in table_data if c)
            else:
                table_text = str(table_data) if table_data else ""
            if not table_text.strip():
                continue
            tt = table_text.strip()
            page_n, tbl_bbox, per_page_bboxes = (
                _page_bbox_from_ragflow_table_positions(table_poss)
            )
            tbl_meta: dict = {}
            if len(per_page_bboxes) > 1:
                tbl_meta["page_bboxes"] = {
                    str(pg): list(bb) for pg, bb in sorted(per_page_bboxes.items())
                }
            raw_rows.append(
                {
                    "text": table_text,
                    "page": page_n,
                    "offset": idx,
                    "bbox": tbl_bbox,
                    "block_type": "table",
                    "level": 0,
                    "layout_type": "table",
                    "image": img if isinstance(img, bytes) else None,
                    "block_id": f"ragflow:table:{ti}",
                    "char_start": stream_cursor,
                    "char_end": stream_cursor + len(tt),
                    "metadata": tbl_meta if tbl_meta else None,
                }
            )
            stream_cursor += len(tt) + 2
            idx += 1

        return to_document_blocks(raw_rows, source="pdf")
