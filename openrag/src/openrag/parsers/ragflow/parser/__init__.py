#
#  Copyright 2025 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

__all__ = [
    "PdfParser",
    "PlainParser",
    "DocxParser",
    "EpubParser",
    "ExcelParser",
    "PptParser",
    "HtmlParser",
    "JsonParser",
    "MarkdownParser",
    "TxtParser",
    "MarkdownElementExtractor",
]

# Lazy imports – each parser is loaded only when first accessed, so importing
# e.g. pdf_parser alone does NOT force every other parser (and its transitive
# dependencies) to be loaded at package-import time.
_LAZY_MAP = {
    "DocxParser":              (".docx_parser",    "RAGFlowDocxParser"),
    "EpubParser":              (".epub_parser",    "RAGFlowEpubParser"),
    "ExcelParser":             (".excel_parser",   "RAGFlowExcelParser"),
    "HtmlParser":              (".html_parser",    "RAGFlowHtmlParser"),
    "JsonParser":              (".json_parser",    "RAGFlowJsonParser"),
    "MarkdownParser":          (".markdown_parser","RAGFlowMarkdownParser"),
    "MarkdownElementExtractor":(".markdown_parser","MarkdownElementExtractor"),
    "PdfParser":               (".pdf_parser",     "RAGFlowPdfParser"),
    "PlainParser":             (".pdf_parser",     "PlainParser"),
    "PptParser":               (".ppt_parser",     "RAGFlowPptParser"),
    "TxtParser":               (".txt_parser",     "RAGFlowTxtParser"),
}


def __getattr__(name: str):
    if name in _LAZY_MAP:
        module_rel, attr = _LAZY_MAP[name]
        import importlib
        mod = importlib.import_module(module_rel, __name__)
        val = getattr(mod, attr)
        globals()[name] = val          # cache for subsequent access
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
