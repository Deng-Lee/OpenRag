"""Minimal compatibility helpers for optional RAGFlow runtime."""

from __future__ import annotations

import asyncio
from typing import Any, Callable


def pip_install_torch() -> None:
    """No-op in container runtime; torch installation is optional."""
    return None


async def thread_pool_exec(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run blocking callable in a worker thread and await result."""
    return await asyncio.to_thread(func, *args, **kwargs)

