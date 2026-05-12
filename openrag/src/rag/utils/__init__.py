"""Compatibility package for RAGFlow ``rag.utils`` imports."""

from __future__ import annotations

from common.file_utils import get_project_base_directory
from common.misc_utils import pip_install_torch, thread_pool_exec

__all__ = [
    "get_project_base_directory",
    "pip_install_torch",
    "thread_pool_exec",
]
