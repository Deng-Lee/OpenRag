"""Token-bounded chunking for structured Excel ``DocumentBlock`` objects."""

from __future__ import annotations

from dataclasses import dataclass
import uuid

from openpyxl.utils.cell import get_column_letter

from openrag.chunking.chunk_models import Chunk
from openrag.parsers.base import DocumentBlock


class ExcelChunkingError(RuntimeError):
    """Non-retryable validation or token-budget failure for Excel content."""

    retryable = False

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.public_message = message


@dataclass(frozen=True)
class _ExcelRow:
    row_index: int
    values: tuple[str | None, ...]


def _get_encoder():
    try:
        import tiktoken

        return tiktoken.get_encoding("cl100k_base")
    except Exception as exc:
        raise ExcelChunkingError(
            "EXCEL_TOKENIZER_UNAVAILABLE",
            "Excel token chunking requires the cl100k_base tokenizer",
        ) from exc


def _token_count(encoder, text: str) -> int:
    return len(encoder.encode(text))


def _invalid(message: str) -> ExcelChunkingError:
    return ExcelChunkingError("EXCEL_TABLE_DATA_INVALID", message)


def _validate_block(
    block: DocumentBlock,
) -> tuple[str, int, int, list[str], list[_ExcelRow]]:
    table_data = block.table_data
    if not isinstance(table_data, dict):
        raise _invalid("Excel block is missing table_data")
    if table_data.get("schema_version") != "excel-table-v1":
        raise _invalid("Unsupported Excel table_data schema")

    sheet_name = table_data.get("sheet_name")
    sheet_index = table_data.get("sheet_index")
    header_row_index = table_data.get("header_row_index")
    header = table_data.get("header")
    raw_rows = table_data.get("rows")
    if not isinstance(sheet_name, str) or not sheet_name:
        raise _invalid("Excel table_data has no sheet name")
    if not isinstance(sheet_index, int) or sheet_index < 0:
        raise _invalid("Excel table_data has an invalid sheet index")
    if not isinstance(header_row_index, int) or header_row_index < 1:
        raise _invalid("Excel table_data has an invalid header row index")
    if not isinstance(header, list) or not header:
        raise _invalid("Excel table_data has no header")
    if not isinstance(raw_rows, list):
        raise _invalid("Excel table_data rows must be a list")

    headers: list[str] = []
    for expected_index, item in enumerate(header, 1):
        if not isinstance(item, dict):
            raise _invalid("Excel table_data header item must be an object")
        if item.get("column_index") != expected_index:
            raise _invalid("Excel table_data column indices must be contiguous")
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise _invalid("Excel table_data column name must be non-empty")
        headers.append(name)

    rows: list[_ExcelRow] = []
    previous_row_index = header_row_index
    for item in raw_rows:
        if not isinstance(item, dict):
            raise _invalid("Excel table_data row must be an object")
        row_index = item.get("row_index")
        values = item.get("values")
        if not isinstance(row_index, int) or row_index <= previous_row_index:
            raise _invalid("Excel table_data row indices must be increasing")
        if not isinstance(values, list) or len(values) != len(headers):
            raise _invalid("Excel table_data row width does not match the header")
        if any(value is not None and not isinstance(value, str) for value in values):
            raise _invalid("Excel table_data values must be strings or null")
        rows.append(_ExcelRow(row_index, tuple(values)))
        previous_row_index = row_index

    return sheet_name, sheet_index, header_row_index, headers, rows


def _render_rows(sheet_name: str, headers: list[str], rows: list[_ExcelRow]) -> str:
    return "\n".join(
        [
            f"Sheet: {sheet_name}",
            f"Rows: {rows[0].row_index}-{rows[-1].row_index}",
            " | ".join(headers),
            *(" | ".join(value or "" for value in row.values) for row in rows),
        ]
    )


def _render_columns(
    sheet_name: str,
    row: _ExcelRow,
    fields: list[tuple[int, str, str | None]],
    total_columns: int,
) -> str:
    return "\n".join(
        [
            f"Sheet: {sheet_name}",
            f"Row: {row.row_index}",
            f"Columns: {fields[0][0]}-{fields[-1][0]}/{total_columns}",
            *(f"{header}: {value or ''}" for _, header, value in fields),
        ]
    )


def _base_metadata(
    *,
    sheet_name: str,
    sheet_index: int,
    row_start: int,
    row_end: int,
    column_start: int,
    column_end: int,
) -> dict:
    return {
        "source_format": "excel",
        "excel_chunk_strategy": "excel_table_token_v1",
        "sheet_name": sheet_name,
        "sheet_index": sheet_index,
        "row_start": row_start,
        "row_end": row_end,
        "column_start": column_start,
        "column_end": column_end,
        "fragment_index": None,
        "fragment_total": None,
    }


def _make_chunk(
    *,
    encoder,
    block: DocumentBlock,
    text: str,
    chunk_size: int,
    metadata: dict,
) -> Chunk:
    token_count = _token_count(encoder, text)
    if token_count > chunk_size:
        raise ExcelChunkingError(
            "EXCEL_CHUNK_TOKEN_LIMIT_EXCEEDED",
            f"Excel chunk has {token_count} tokens, limit is {chunk_size}",
        )
    metadata = {**metadata, "token_count": token_count, "token_limit": chunk_size}
    source_start = block.char_start if block.char_start is not None else block.offset
    source_end = (
        block.char_end
        if block.char_end is not None
        else source_start + len(block.text)
    )
    return Chunk(
        text=text,
        chunk_id=str(uuid.uuid4()),
        page=block.page,
        start_offset=source_start,
        end_offset=source_end,
        bbox=block.bbox,
        level=block.level,
        block_type="table",
        source_block_id=block.block_id,
        source_char_start=block.char_start,
        source_char_end=block.char_end,
        metadata=metadata,
    )


def _largest_fitting_prefix(payload: str, render, encoder, chunk_size: int) -> str:
    low = 1
    high = len(payload)
    best = ""
    while low <= high:
        middle = (low + high) // 2
        candidate = payload[:middle]
        if _token_count(encoder, render(candidate)) <= chunk_size:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return best


def _fragment_payload(payload: str, render, encoder, chunk_size: int) -> list[str]:
    total_guess = 1
    for _ in range(20):
        pieces: list[str] = []
        remaining = payload
        while remaining:
            fragment_index = len(pieces) + 1
            piece = _largest_fitting_prefix(
                remaining,
                lambda value: render(value, fragment_index, total_guess),
                encoder,
                chunk_size,
            )
            if not piece:
                raise ExcelChunkingError(
                    "EXCEL_CHUNK_TOKEN_LIMIT_EXCEEDED",
                    "Excel chunk context leaves no room for cell content",
                )
            pieces.append(piece)
            remaining = remaining[len(piece):]
        if len(pieces) == total_guess:
            return pieces
        total_guess = len(pieces)
    raise ExcelChunkingError(
        "EXCEL_CHUNK_TOKEN_LIMIT_EXCEEDED",
        "Excel cell fragment count did not stabilize",
    )


def _cell_chunks(
    *,
    encoder,
    block: DocumentBlock,
    sheet_name: str,
    sheet_index: int,
    row: _ExcelRow,
    column_index: int,
    header: str,
    value: str | None,
    chunk_size: int,
) -> list[Chunk]:
    coordinate = f"{get_column_letter(column_index)}{row.row_index}"
    common_metadata = _base_metadata(
        sheet_name=sheet_name,
        sheet_index=sheet_index,
        row_start=row.row_index,
        row_end=row.row_index,
        column_start=column_index,
        column_end=column_index,
    )

    def render_value(piece: str, index: int, total: int) -> str:
        return (
            f"Sheet: {sheet_name}\n"
            f"Cell: {coordinate}\n"
            f"Column: {header}\n"
            f"Fragment: {index}/{total}\n\n{piece}"
        )

    chunks: list[Chunk] = []
    if _token_count(encoder, render_value("", 1, 1)) > chunk_size:
        def render_header(piece: str, index: int, total: int) -> str:
            return (
                f"Sheet: {sheet_name}\n"
                f"Cell: {coordinate}\n"
                f"Header fragment: {index}/{total}\n\n{piece}"
            )

        header_pieces = _fragment_payload(
            header, render_header, encoder, chunk_size
        )
        for index, piece in enumerate(header_pieces, 1):
            chunks.append(
                _make_chunk(
                    encoder=encoder,
                    block=block,
                    text=render_header(piece, index, len(header_pieces)),
                    chunk_size=chunk_size,
                    metadata={
                        **common_metadata,
                        "fragment_index": index,
                        "fragment_total": len(header_pieces),
                        "fragment_kind": "header",
                    },
                )
            )

        def render_value(piece: str, index: int, total: int) -> str:
            return (
                f"Sheet: {sheet_name}\n"
                f"Cell: {coordinate}\n"
                f"Column index: {column_index}\n"
                f"Value fragment: {index}/{total}\n\n{piece}"
            )

    payload = value or ""
    if payload:
        pieces = _fragment_payload(payload, render_value, encoder, chunk_size)
        for index, piece in enumerate(pieces, 1):
            chunks.append(
                _make_chunk(
                    encoder=encoder,
                    block=block,
                    text=render_value(piece, index, len(pieces)),
                    chunk_size=chunk_size,
                    metadata={
                        **common_metadata,
                        "fragment_index": index,
                        "fragment_total": len(pieces),
                        "fragment_kind": "value",
                    },
                )
            )
    return chunks


def _split_wide_row(
    *,
    encoder,
    block: DocumentBlock,
    sheet_name: str,
    sheet_index: int,
    headers: list[str],
    row: _ExcelRow,
    chunk_size: int,
) -> list[Chunk]:
    fields = [
        (column_index, header, row.values[column_index - 1])
        for column_index, header in enumerate(headers, 1)
    ]
    chunks: list[Chunk] = []
    current: list[tuple[int, str, str | None]] = []

    def append_group(group: list[tuple[int, str, str | None]]) -> None:
        text = _render_columns(sheet_name, row, group, len(headers))
        chunks.append(
            _make_chunk(
                encoder=encoder,
                block=block,
                text=text,
                chunk_size=chunk_size,
                metadata=_base_metadata(
                    sheet_name=sheet_name,
                    sheet_index=sheet_index,
                    row_start=row.row_index,
                    row_end=row.row_index,
                    column_start=group[0][0],
                    column_end=group[-1][0],
                ),
            )
        )

    for field in fields:
        candidate = [*current, field]
        if _token_count(
            encoder, _render_columns(sheet_name, row, candidate, len(headers))
        ) <= chunk_size:
            current = candidate
            continue
        if current:
            append_group(current)
            current = []
        if _token_count(
            encoder, _render_columns(sheet_name, row, [field], len(headers))
        ) <= chunk_size:
            current = [field]
        else:
            chunks.extend(
                _cell_chunks(
                    encoder=encoder,
                    block=block,
                    sheet_name=sheet_name,
                    sheet_index=sheet_index,
                    row=row,
                    column_index=field[0],
                    header=field[1],
                    value=field[2],
                    chunk_size=chunk_size,
                )
            )
    if current:
        append_group(current)
    return chunks


def _split_block(block: DocumentBlock, encoder, chunk_size: int) -> list[Chunk]:
    sheet_name, sheet_index, header_row_index, headers, rows = _validate_block(block)
    if not rows:
        header_row = _ExcelRow(header_row_index, tuple(None for _ in headers))
        return _split_wide_row(
            encoder=encoder,
            block=block,
            sheet_name=sheet_name,
            sheet_index=sheet_index,
            headers=headers,
            row=header_row,
            chunk_size=chunk_size,
        )
    chunks: list[Chunk] = []
    current: list[_ExcelRow] = []

    def append_rows(group: list[_ExcelRow]) -> None:
        chunks.append(
            _make_chunk(
                encoder=encoder,
                block=block,
                text=_render_rows(sheet_name, headers, group),
                chunk_size=chunk_size,
                metadata=_base_metadata(
                    sheet_name=sheet_name,
                    sheet_index=sheet_index,
                    row_start=group[0].row_index,
                    row_end=group[-1].row_index,
                    column_start=1,
                    column_end=len(headers),
                ),
            )
        )

    for row in rows:
        candidate = [*current, row]
        if _token_count(encoder, _render_rows(sheet_name, headers, candidate)) <= chunk_size:
            current = candidate
            continue
        if current:
            append_rows(current)
            current = []
        if _token_count(encoder, _render_rows(sheet_name, headers, [row])) <= chunk_size:
            current = [row]
        else:
            chunks.extend(
                _split_wide_row(
                    encoder=encoder,
                    block=block,
                    sheet_name=sheet_name,
                    sheet_index=sheet_index,
                    headers=headers,
                    row=row,
                    chunk_size=chunk_size,
                )
            )
    if current:
        append_rows(current)
    return chunks


def split_excel_blocks(
    blocks: list[DocumentBlock],
    *,
    chunk_size: int,
) -> list[Chunk]:
    """Split structured Excel blocks while enforcing the final token limit."""
    if chunk_size <= 0:
        raise ValueError(f"chunk_size must be greater than 0, got {chunk_size}")
    encoder = _get_encoder()
    chunks: list[Chunk] = []
    for block in blocks:
        chunks.extend(_split_block(block, encoder, chunk_size))
    return chunks
