"""Compatibility shims for vendored RAGFlow rag.nlp imports."""

from __future__ import annotations

import chardet
import re
from collections import defaultdict

from rag.nlp.rag_tokenizer import RagTokenizer, rag_tokenizer

__all__ = ["RagTokenizer", "rag_tokenizer", "find_codec", "append_context2table_image4pdf"]

# ---------- find_codec ----------

_ALL_CODECS = [
    "utf-8", "gb2312", "gbk", "utf_16", "ascii", "big5", "big5hkscs",
    "cp037", "cp273", "cp424", "cp437",
    "cp500", "cp720", "cp737", "cp775", "cp850", "cp852", "cp855", "cp856", "cp857",
    "cp858", "cp860", "cp861", "cp862", "cp863", "cp864", "cp865", "cp866", "cp869",
    "cp874", "cp875", "cp932", "cp949", "cp950", "cp1006", "cp1026", "cp1125",
    "cp1140", "cp1250", "cp1251", "cp1252", "cp1253", "cp1254", "cp1255", "cp1256",
    "cp1257", "cp1258", "euc_jp", "euc_jis_2004", "euc_jisx0213", "euc_kr",
    "gb18030", "hz", "iso2022_jp", "iso2022_jp_1", "iso2022_jp_2",
    "iso2022_jp_2004", "iso2022_jp_3", "iso2022_jp_ext", "iso2022_kr", "latin_1",
    "iso8859_2", "iso8859_3", "iso8859_4", "iso8859_5", "iso8859_6", "iso8859_7",
    "iso8859_8", "iso8859_9", "iso8859_10", "iso8859_11", "iso8859_13",
    "iso8859_14", "iso8859_15", "iso8859_16", "johab", "koi8_r", "koi8_t", "koi8_u",
    "kz1048", "mac_cyrillic", "mac_greek", "mac_iceland", "mac_latin2", "mac_roman",
    "mac_turkish", "ptcp154", "shift_jis", "shift_jis_2004", "shift_jisx0213",
    "utf_32", "utf_32_be", "utf_32_le", "utf_16_be", "utf_16_le", "utf_7",
    "windows-1250", "windows-1251", "windows-1252", "windows-1253",
    "windows-1254", "windows-1255", "windows-1256", "windows-1257", "windows-1258",
    "latin-2",
]


def find_codec(blob: bytes) -> str:
    """Detect encoding of a byte blob; fall back to utf-8."""
    detected = chardet.detect(blob[:1024])
    if detected["confidence"] > 0.5:
        if detected["encoding"] == "ascii":
            return "utf-8"

    for c in _ALL_CODECS:
        try:
            blob[:1024].decode(c)
            return c
        except Exception:
            pass
        try:
            blob.decode(c)
            return c
        except Exception:
            pass

    return "utf-8"


# ---------- append_context2table_image4pdf ----------

def append_context2table_image4pdf(
    sections: list,
    tabls: list,
    table_context_size: int = 0,
    return_context: bool = False,
):
    """Extract context around table/figure images in a PDF.

    Simplified version that works without the full deepdoc dependency.
    When deepdoc is available, delegates to the original implementation.
    """
    if table_context_size <= 0:
        return [] if return_context else tabls

    try:
        from common.token_utils import num_tokens_from_string
    except ImportError:
        # Minimal fallback: count words
        def num_tokens_from_string(s: str) -> int:
            return len(s.split()) if s else 0

    # Try to use the original deepdoc-based implementation if available
    try:
        from deepdoc.parser import PdfParser

        # Original implementation path — delegate entirely
        return _append_context_deepdoc(
            PdfParser, sections, tabls, table_context_size, return_context, num_tokens_from_string
        )
    except ImportError:
        pass

    # Simplified path: build page_bucket from sections, add basic context
    page_bucket: dict[int, list] = defaultdict(list)
    for i, item in enumerate(sections):
        if isinstance(item, (tuple, list)):
            txt = item[0] if item else ""
            # Poss may be a list of (page, left, right, top, bottom) or a position string
            poss_raw = item[2] if len(item) > 2 else (item[1] if len(item) > 1 else "")
        else:
            txt = item
            poss_raw = ""

        poss: list = []
        if isinstance(poss_raw, list):
            poss = poss_raw
        elif isinstance(poss_raw, str) and "@@" in poss_raw:
            # Extract page numbers from @@...## tags
            for m in re.finditer(r"@@(\d+)-?\d*\t[\d.]+\t[\d.]+\t[\d.]+\t[\d.]+##", poss_raw):
                poss.append((int(m.group(1)), 0, 0, 0, 0))
        elif isinstance(txt, str) and "@@" in txt:
            for m in re.finditer(r"@@(\d+)-?\d*\t[\d.]+\t[\d.]+\t[\d.]+\t[\d.]+##", txt):
                poss.append((int(m.group(1)), 0, 0, 0, 0))

        clean_txt = txt
        if isinstance(txt, str) and "@@" in txt:
            clean_txt = re.sub(r"@@[0-9-]+\t[0-9.\t]+##", "", txt).strip()

        for page, *_ in poss:
            page_bucket[page].append(((0, 0, 0, 0), clean_txt))

    # Collect context around each table
    res = []
    contexts = []
    for tbl_item in tabls:
        # tbl_item may be ((img, tb), poss) or (img, tb)
        if isinstance(tbl_item, tuple) and len(tbl_item) == 2:
            if isinstance(tbl_item[0], tuple):
                (img, tb), poss = tbl_item
            else:
                img, tb = tbl_item
                poss = []
        else:
            continue

        if not poss:
            res.append((img, tb))
            if return_context:
                contexts.append(("", ""))
            continue

        page = int(poss[0][0]) if poss[0] else 0
        if isinstance(tb, list):
            tb = "\n".join(tb)

        # Build simple context from surrounding sections on same/adjacent pages
        upper = ""
        lower = ""
        blks = page_bucket.get(page, [])
        for (_, ctx_txt) in blks:
            if num_tokens_from_string(upper) < table_context_size:
                upper += ctx_txt + " "
        next_blks = page_bucket.get(page + 1, [])
        for (_, ctx_txt) in next_blks:
            if num_tokens_from_string(lower) < table_context_size:
                lower += ctx_txt + " "

        tb_with_ctx = (upper.strip() + " " + tb + " " + lower.strip()).strip()
        res.append((img, tb_with_ctx))
        if return_context:
            contexts.append((upper.strip(), lower.strip()))

    return contexts if return_context else res


def _append_context_deepdoc(PdfParser, sections, tabls, table_context_size, return_context, num_tokens_from_string):
    """Full deepdoc-based append_context2table_image4pdf (from ragflow)."""
    page_bucket: dict[int, list] = defaultdict(list)
    for i, item in enumerate(sections):
        if isinstance(item, (tuple, list)):
            if len(item) > 2:
                txt, _sec_id, poss = item[0], item[1], item[2]
            else:
                txt = item[0] if item else ""
                poss = item[1] if len(item) > 1 else ""
        else:
            txt = item
            poss = ""
        if isinstance(poss, list):
            poss = poss
        elif isinstance(poss, str):
            if "@@" not in poss and isinstance(txt, str) and "@@" in txt:
                poss = txt
            poss = PdfParser.extract_positions(poss)
        else:
            if isinstance(txt, str) and "@@" in txt:
                poss = PdfParser.extract_positions(txt)
            else:
                poss = []
        if isinstance(txt, str) and "@@" in txt:
            txt = re.sub(r"@@[0-9-]+\t[0-9.\t]+##", "", txt).strip()
        for page, left, right, top, bottom in poss:
            if isinstance(page, list):
                page = page[0] if page else 0
            page_bucket[page].append(((left, right, top, bottom), txt))

    def upper_context(page, i):
        txt = ""
        if page not in page_bucket:
            i = -1
        while num_tokens_from_string(txt) < table_context_size:
            if i < 0:
                page -= 1
                if page < 0 or page not in page_bucket:
                    break
                i = len(page_bucket[page]) - 1
            blks = page_bucket[page]
            (_, _, _, _), cnt = blks[i]
            txts = re.split(r"([。!?？；！\n]|\. )", cnt, flags=re.DOTALL)[::-1]
            for j in range(0, len(txts), 2):
                txt = (txts[j + 1] if j + 1 < len(txts) else "") + txts[j] + txt
                if num_tokens_from_string(txt) > table_context_size:
                    break
            i -= 1
        return txt

    def lower_context(page, i):
        txt = ""
        if page not in page_bucket:
            return txt
        while num_tokens_from_string(txt) < table_context_size:
            if i >= len(page_bucket[page]):
                page += 1
                if page not in page_bucket:
                    break
                i = 0
            blks = page_bucket[page]
            (_, _, _, _), cnt = blks[i]
            txts = re.split(r"([。!?？；！\n]|\. )", cnt, flags=re.DOTALL)
            for j in range(0, len(txts), 2):
                txt += txts[j] + (txts[j + 1] if j + 1 < len(txts) else "")
                if num_tokens_from_string(txt) > table_context_size:
                    break
            i += 1
        return txt

    res = []
    contexts = []
    for (img, tb), poss in tabls:
        page, left, right, top, bott = poss[0]
        _page, _left, _right, _top, _bott = poss[-1]
        if isinstance(tb, list):
            tb = "\n".join(tb)

        i = 0
        blks = page_bucket.get(page, [])
        _tb = tb
        while i < len(blks):
            if i + 1 >= len(blks):
                if _page > page:
                    page += 1
                    i = 0
                    blks = page_bucket.get(page, [])
                    continue
                upper = upper_context(page, i)
                lower = lower_context(page + 1, 0)
                tb = upper + tb + lower
                contexts.append((upper.strip(), lower.strip()))
                break
            ((l, r, t, b), cnt) = blks[i]
            if l <= left and r >= right and t <= top and b >= bott:
                upper = upper_context(page, i - 1)
                lower = lower_context(page, i + 1)
                tb = upper + tb + lower
                contexts.append((upper.strip(), lower.strip()))
                break
            i += 1

        res.append((img, tb))

    return contexts if return_context else res