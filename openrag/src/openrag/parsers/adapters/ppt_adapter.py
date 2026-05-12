"""PowerPoint parser adapter.

Handles both modern .pptx (OOXML) and legacy .ppt (OLE2) formats.
Legacy .ppt files are automatically converted to .pptx before parsing:
  1. Windows + PowerPoint → subprocess-isolated COM automation
  2. Any OS + LibreOffice → headless CLI conversion
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

logger = logging.getLogger(__name__)

_PPT_COM_SCRIPT = r'''
import json, os, sys

ppt_path, out_dir = sys.argv[1], sys.argv[2]

try:
    import win32api
    ppt_path = win32api.GetLongPathName(ppt_path)
    out_dir  = win32api.GetLongPathName(out_dir)
except Exception:
    pass

ppt_app = None
try:
    import pythoncom, win32com.client
    pythoncom.CoInitialize()
    ppt_app = win32com.client.Dispatch("PowerPoint.Application")

    abs_in = os.path.abspath(ppt_path)
    base = os.path.splitext(os.path.basename(abs_in))[0]
    abs_out = os.path.join(out_dir, base + ".pptx")

    presentation = ppt_app.Presentations.Open(abs_in, WithWindow=False)
    presentation.SaveAs(abs_out, FileFormat=24)  # ppSaveAsOpenXMLPresentation
    presentation.Close()

    if os.path.isfile(abs_out):
        print(json.dumps({"ok": True, "path": abs_out}))
    else:
        print(json.dumps({"ok": False, "error": "output file not found after SaveAs"}))
except Exception as exc:
    print(json.dumps({"ok": False, "error": str(exc)}))
finally:
    if ppt_app is not None:
        try:
            ppt_app.Quit()
        except Exception:
            pass
    try:
        pythoncom.CoUninitialize()
    except Exception:
        pass
'''


def _convert_ppt_via_subprocess(ppt_path: str, out_dir: str) -> str | None:
    """Convert .ppt → .pptx by running PowerPoint COM in a clean subprocess."""
    if os.name != "nt":
        return None
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _PPT_COM_SCRIPT, ppt_path, out_dir],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            data = json.loads(proc.stdout.strip().split("\n")[-1])
            if data.get("ok") and os.path.isfile(data["path"]):
                logger.info("Converted .ppt → .pptx via PowerPoint COM subprocess")
                return data["path"]
            else:
                logger.warning("PowerPoint COM subprocess reported failure: %s", data.get("error"))
        else:
            stderr = (proc.stderr or "").strip()
            logger.warning("PowerPoint COM subprocess exited %d: %s", proc.returncode, stderr[:500])
    except FileNotFoundError:
        pass
    except subprocess.TimeoutExpired:
        logger.warning("PowerPoint COM subprocess timed out (120s)")
    except Exception as exc:
        logger.warning("PowerPoint COM subprocess error: %s", exc)
    return None


def _convert_ppt_via_libreoffice(ppt_path: str, out_dir: str) -> str | None:
    """Try converting .ppt → .pptx via LibreOffice headless CLI."""
    lo_names = ["libreoffice", "soffice"]
    if os.name == "nt":
        lo_names += [
            r"C:\Program Files\LibreOffice\program\soffice.exe",
            r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        ]
    for lo in lo_names:
        try:
            result = subprocess.run(
                [lo, "--headless", "--convert-to", "pptx", "--outdir", out_dir, ppt_path],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                base = os.path.splitext(os.path.basename(ppt_path))[0]
                converted = os.path.join(out_dir, base + ".pptx")
                if os.path.isfile(converted):
                    logger.info("Converted .ppt → .pptx via LibreOffice: %s", base)
                    return converted
        except FileNotFoundError:
            continue
        except Exception as exc:
            logger.warning("LibreOffice ppt conversion failed (%s): %s", lo, exc)
    return None


def _convert_ppt_to_pptx(ppt_path: str) -> str:
    out_dir = tempfile.mkdtemp(prefix="ppt2pptx_")

    result = _convert_ppt_via_subprocess(ppt_path, out_dir)
    if result:
        return result

    result = _convert_ppt_via_libreoffice(ppt_path, out_dir)
    if result:
        return result

    shutil.rmtree(out_dir, ignore_errors=True)
    raise RuntimeError(
        f"无法解析旧版 .ppt 文件 '{os.path.basename(ppt_path)}'。"
        f"python-pptx 仅支持 .pptx 格式。"
        f"请确保安装了 Microsoft PowerPoint 或 LibreOffice，"
        f"或将文件另存为 .pptx 后重新上传。"
    )


class PptParserAdapter(RAGFlowParserAdapter):
    """PowerPoint 解析器适配器（.pptx + .ppt）"""

    supported_extensions = ('.pptx', '.ppt')

    def __init__(self):
        super().__init__()
        from openrag.parsers.ragflow.parser.ppt_parser import RAGFlowPptParser
        self.ragflow_parser = RAGFlowPptParser()

    def parse(self, file_path: str) -> list[DocumentBlock]:
        actual_path = file_path
        converted_dir = None
        try:
            if file_path.lower().endswith('.ppt') and not file_path.lower().endswith('.pptx'):
                logger.info("Legacy .ppt detected, converting: %s", os.path.basename(file_path))
                actual_path = _convert_ppt_to_pptx(file_path)
                converted_dir = os.path.dirname(actual_path)

            # RAGFlowPptParser.__call__ requires (fnm, from_page, to_page)
            # and returns a list of strings (one per slide)
            slide_texts = self.ragflow_parser(actual_path, from_page=0, to_page=100000)

            blocks: list[DocumentBlock] = []
            stream_cursor = 0
            for idx, text in enumerate(slide_texts):
                if not text.strip():
                    continue
                blocks.append(DocumentBlock(
                    text=text,
                    page=idx + 1,
                    offset=idx,
                    block_type="text",
                    level=0,
                    layout_type="text",
                    block_id=f"pptx:slide:{idx + 1}",
                    char_start=stream_cursor,
                    char_end=stream_cursor + len(text),
                ))
                stream_cursor += len(text) + 2
            return blocks
        finally:
            if converted_dir:
                shutil.rmtree(converted_dir, ignore_errors=True)
