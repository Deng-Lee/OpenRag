"""DOCX/DOC parser adapter.

Handles both modern .docx (OOXML) and legacy .doc (OLE2 binary) formats.
Legacy .doc files are automatically converted to .docx before parsing:
  1. Windows + Word → subprocess-isolated COM automation (most reliable)
  2. Any OS + LibreOffice → headless CLI conversion
  3. Neither available → clear error message
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile

from openrag.parsers.adapters.base_adapter import RAGFlowParserAdapter
from openrag.parsers.base import DocumentBlock
from openrag.ragflow_core.compat import to_document_blocks

logger = logging.getLogger(__name__)

# Inline script executed in a *separate* Python process so Word COM gets a
# clean STA apartment – avoids conflicts with the worker's own COM state.
_WORD_COM_SCRIPT = r'''
import json, os, sys

doc_path, out_dir = sys.argv[1], sys.argv[2]

# Resolve 8.3 short paths (e.g. YANGDO~1) to long paths for Word COM
try:
    import win32api
    doc_path = win32api.GetLongPathName(doc_path)
    out_dir  = win32api.GetLongPathName(out_dir)
except Exception:
    pass

word = None
try:
    import pythoncom, win32com.client
    pythoncom.CoInitialize()
    word = win32com.client.Dispatch("Word.Application")
    word.Visible = False
    word.DisplayAlerts = False

    abs_in = os.path.abspath(doc_path)
    base = os.path.splitext(os.path.basename(abs_in))[0]
    abs_out = os.path.join(out_dir, base + ".docx")

    doc = word.Documents.Open(abs_in, ReadOnly=True)
    doc.SaveAs2(abs_out, FileFormat=16)  # wdFormatXMLDocument
    doc.Close(False)

    if os.path.isfile(abs_out):
        print(json.dumps({"ok": True, "path": abs_out}))
    else:
        print(json.dumps({"ok": False, "error": "output file not found after SaveAs2"}))
except Exception as exc:
    print(json.dumps({"ok": False, "error": str(exc)}))
finally:
    if word is not None:
        try:
            word.Quit()
        except Exception:
            pass
    try:
        pythoncom.CoUninitialize()
    except Exception:
        pass
'''


def _convert_via_word_subprocess(doc_path: str, out_dir: str) -> str | None:
    """Convert .doc → .docx by running Word COM in a clean subprocess."""
    if os.name != "nt":
        return None
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _WORD_COM_SCRIPT, doc_path, out_dir],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            data = json.loads(proc.stdout.strip().split("\n")[-1])
            if data.get("ok") and os.path.isfile(data["path"]):
                logger.info("Converted .doc → .docx via Word COM subprocess")
                return data["path"]
            else:
                logger.warning("Word COM subprocess reported failure: %s", data.get("error"))
        else:
            stderr = (proc.stderr or "").strip()
            logger.warning("Word COM subprocess exited %d: %s", proc.returncode, stderr[:500])
    except FileNotFoundError:
        pass
    except subprocess.TimeoutExpired:
        logger.warning("Word COM subprocess timed out (120s)")
    except Exception as exc:
        logger.warning("Word COM subprocess error: %s", exc)
    return None


def _convert_via_libreoffice(doc_path: str, out_dir: str) -> str | None:
    """Try converting .doc → .docx via LibreOffice headless CLI."""
    lo_names = ["libreoffice", "soffice"]
    if os.name == "nt":
        lo_names += [
            r"C:\Program Files\LibreOffice\program\soffice.exe",
            r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        ]

    for lo in lo_names:
        try:
            result = subprocess.run(
                [lo, "--headless", "--convert-to", "docx", "--outdir", out_dir, doc_path],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                base = os.path.splitext(os.path.basename(doc_path))[0]
                converted = os.path.join(out_dir, base + ".docx")
                if os.path.isfile(converted):
                    logger.info("Converted .doc → .docx via LibreOffice: %s", base)
                    return converted
        except FileNotFoundError:
            continue
        except Exception as exc:
            logger.warning("LibreOffice conversion attempt failed (%s): %s", lo, exc)
    return None


def _convert_doc_to_docx(doc_path: str) -> str:
    """Convert a legacy .doc file to .docx.

    Tries Word COM (via subprocess) first, then LibreOffice.
    Returns path to the converted .docx (in a temp directory).
    """
    out_dir = tempfile.mkdtemp(prefix="doc2docx_")

    result = _convert_via_word_subprocess(doc_path, out_dir)
    if result:
        return result

    result = _convert_via_libreoffice(doc_path, out_dir)
    if result:
        return result

    shutil.rmtree(out_dir, ignore_errors=True)
    raise RuntimeError(
        f"无法解析旧版 .doc 文件 '{os.path.basename(doc_path)}'。"
        f"python-docx 仅支持 .docx 格式。"
        f"请确保安装了 Microsoft Word 或 LibreOffice，"
        f"或将文件另存为 .docx 后重新上传。"
    )


class DocxParserAdapter(RAGFlowParserAdapter):
    """Word 文档解析器适配器（.docx + .doc）"""

    supported_extensions = ('.docx', '.doc')

    def __init__(self):
        super().__init__()
        from openrag.parsers.ragflow.parser.docx_parser import RAGFlowDocxParser
        self.ragflow_parser = RAGFlowDocxParser()

    def parse(self, file_path: str) -> list[DocumentBlock]:
        actual_path = file_path
        converted_dir = None

        try:
            if file_path.lower().endswith('.doc') and not file_path.lower().endswith('.docx'):
                logger.info("Legacy .doc detected, converting: %s", os.path.basename(file_path))
                actual_path = _convert_doc_to_docx(file_path)
                converted_dir = os.path.dirname(actual_path)

            # RAGFlowDocxParser returns (secs, tbls):
            #   secs = [(text, style_name), ...]
            #   tbls = [[line, ...], ...]
            secs, tbls = self.ragflow_parser(actual_path)

            raw_rows: list[dict] = []
            idx = 0
            stream_cursor = 0

            for pi, (text, style_name) in enumerate(secs):
                if not text.strip():
                    continue
                level = self._heading_level_from_style(style_name)
                raw_rows.append(
                    {
                        "text": text,
                        "page": 1,
                        "offset": idx,
                        "block_type": "heading" if level > 0 else "text",
                        "level": level,
                        "layout_type": "title" if level > 0 else "text",
                        "block_id": f"docx:para:{pi}",
                        "char_start": stream_cursor,
                        "char_end": stream_cursor + len(text),
                    }
                )
                stream_cursor += len(text) + 1
                idx += 1

            for tj, tbl_lines in enumerate(tbls):
                if not tbl_lines:
                    continue
                table_text = "\n".join(tbl_lines) if isinstance(tbl_lines, list) else str(tbl_lines)
                if not table_text.strip():
                    continue
                raw_rows.append(
                    {
                        "text": table_text,
                        "page": 1,
                        "offset": idx,
                        "block_type": "table",
                        "level": 0,
                        "layout_type": "table",
                        "block_id": f"docx:table:{tj}",
                        "char_start": stream_cursor,
                        "char_end": stream_cursor + len(table_text),
                    }
                )
                stream_cursor += len(table_text) + 1
                idx += 1

            return to_document_blocks(raw_rows, source="docx")
        finally:
            if converted_dir:
                shutil.rmtree(converted_dir, ignore_errors=True)

    @staticmethod
    def _heading_level_from_style(style_name: str) -> int:
        if not style_name:
            return 0
        s = style_name.lower().strip()
        # "Heading 1" … "Heading 6" or "标题 1" … "标题 6"
        for prefix in ("heading ", "标题 ", "heading"):
            if s.startswith(prefix):
                rest = s[len(prefix):].strip()
                if rest.isdigit():
                    return min(int(rest), 6)
        return 0
