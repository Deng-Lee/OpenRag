#!/usr/bin/env python3
"""构建/发布前校验：与生产 OPENRAG_PDF_BACKEND=ragflow 相同的 PDF 后端导入链。

仅 ``import openrag.worker.task_worker`` 不会加载 RAGFlow；历史上因此多次出现
「镜像构建成功、部署后解析才报错」。本脚本在 docker build 阶段强制失败。
"""

from __future__ import annotations

import sys


def main() -> int:
    # 与 pdf_adapter 顶层 try 一致：拉齐 pdf_parser → vision → onnx / lazy_image 等
    from openrag.parsers.adapters import pdf_adapter

    if pdf_adapter.RAGFlowPdfParser is None:
        err = pdf_adapter._RAGFLOW_IMPORT_ERROR
        print("RAGFlow PDF backend import failed:", err, file=sys.stderr)
        return 1

    # Verify rag.nlp exports — previously missing find_codec caused
    # "RagTokenizer object has no attribute fine_grained_tokenize" in offline k8s.
    from rag.nlp import rag_tokenizer, find_codec, append_context2table_image4pdf

    assert hasattr(rag_tokenizer, "fine_grained_tokenize"), \
        "rag_tokenizer missing fine_grained_tokenize (offline infinity issue)"
    assert callable(find_codec), "find_codec not callable"
    assert callable(append_context2table_image4pdf), "append_context2table_image4pdf not callable"

    # Quick smoke test: fine_grained_tokenize should return input unchanged (pure-Python shim)
    tks = rag_tokenizer.tokenize("hello world")
    assert rag_tokenizer.fine_grained_tokenize(tks) == tks, \
        f"fine_grained_tokenize mismatch: got '{rag_tokenizer.fine_grained_tokenize(tks)}', expected '{tks}'"

    # 曾出现过的残缺 shim：模块能加载但缺少符号
    from rag.utils.lazy_image import (
        LazyImage,
        ensure_pil_image,
        is_image_like,
        open_image_for_processing,
    )
    del LazyImage, ensure_pil_image, is_image_like, open_image_for_processing

    print("verify_worker_ragflow_bundle: OK (RAGFlowPdfParser + rag.utils.lazy_image + rag.nlp exports)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
