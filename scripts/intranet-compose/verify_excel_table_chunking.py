"""Runtime smoke test for the deployed Excel token chunking path."""

from pathlib import Path
import tempfile

from openpyxl import Workbook

from common.token_utils import num_tokens_from_string
from openrag.chunking.chunk_engine import ChunkEngine
from openrag.parsers.adapters.excel_adapter import StructuredExcelParserAdapter


with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as handle:
    workbook_path = Path(handle.name)

try:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "LargeTable"
    sheet.append([f"Column_{index}" for index in range(1, 41)])
    sheet.append([f"value-{index}-" + "业务说明" * 30 for index in range(1, 41)])
    sheet.append(["超长单元格" * 2000, *("" for _ in range(39))])
    workbook.save(workbook_path)

    blocks = StructuredExcelParserAdapter().parse(str(workbook_path))
    chunks = ChunkEngine().chunk(
        blocks,
        chunk_size=300,
        chunk_overlap=40,
        chunk_method="excel_table_token_v1",
    )
    token_counts = [num_tokens_from_string(chunk.text) for chunk in chunks]
    assert blocks, "structured Excel parser returned no blocks"
    assert chunks, "Excel token chunker returned no chunks"
    assert max(token_counts) <= 300, max(token_counts)
    assert all(chunk.metadata.get("source_format") == "excel" for chunk in chunks)
    assert any(chunk.metadata.get("fragment_kind") == "value" for chunk in chunks)
    print(
        "excel-table-token-smoke-ok",
        f"blocks={len(blocks)}",
        f"chunks={len(chunks)}",
        f"token_max={max(token_counts)}",
    )
finally:
    workbook_path.unlink(missing_ok=True)
