"""Regression test for Excel workbooks containing empty fill styles."""

import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

from openpyxl import Workbook

from openrag.parsers.adapters.excel_adapter import ExcelParserAdapter


_SPREADSHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _append_empty_fill(path: str) -> None:
    with ZipFile(path, "r") as source:
        entries = [(info, source.read(info.filename)) for info in source.infolist()]

    with ZipFile(path, "w", ZIP_DEFLATED) as target:
        for info, content in entries:
            if info.filename == "xl/styles.xml":
                root = ET.fromstring(content)
                fills = root.find(f"{{{_SPREADSHEET_NS}}}fills")
                assert fills is not None
                ET.SubElement(fills, f"{{{_SPREADSHEET_NS}}}fill")
                fills.set("count", str(len(fills)))
                content = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            target.writestr(info, content)


class ExcelEmptyFillFallbackTest(unittest.TestCase):
    def test_excel_with_empty_fill_style_is_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "empty-fill.xlsx")
            workbook = Workbook()
            worksheet = workbook.active
            worksheet.append(["Name", "Status"])
            worksheet.append(["Treasury", "Ready"])
            workbook.save(path)
            _append_empty_fill(path)

            blocks = ExcelParserAdapter().parse(path)

            self.assertTrue(any("Treasury" in block.text for block in blocks))


if __name__ == "__main__":
    unittest.main()
