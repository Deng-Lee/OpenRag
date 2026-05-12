"""Worker module for task execution (lazy exports avoid -m runpy warning)."""

__all__ = ["TaskWorker", "run_workers", "main"]


def __getattr__(name: str):
    if name in __all__:
        from . import task_worker as _task_worker

        return getattr(_task_worker, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
