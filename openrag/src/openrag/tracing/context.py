"""Request-local trace context backed by contextvars."""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from typing import Any, Dict, Optional, Tuple


TRACE_CONTEXT_FIELDS = (
    "trace_id",
    "span_id",
    "trace_type",
    "workspace_id",
    "user_id",
    "file_id",
    "task_id",
    "eval_run_id",
    "eval_query_id",
    "sampling_reason",
)

_trace_id: ContextVar[Optional[str]] = ContextVar("openrag_trace_id", default=None)
_span_id: ContextVar[Optional[str]] = ContextVar("openrag_span_id", default=None)
_trace_type: ContextVar[Optional[str]] = ContextVar("openrag_trace_type", default=None)
_workspace_id: ContextVar[Optional[int]] = ContextVar("openrag_workspace_id", default=None)
_user_id: ContextVar[Optional[int]] = ContextVar("openrag_user_id", default=None)
_file_id: ContextVar[Optional[int]] = ContextVar("openrag_file_id", default=None)
_task_id: ContextVar[Optional[str]] = ContextVar("openrag_task_id", default=None)
_eval_run_id: ContextVar[Optional[int]] = ContextVar("openrag_eval_run_id", default=None)
_eval_query_id: ContextVar[Optional[int]] = ContextVar("openrag_eval_query_id", default=None)
_sampling_reason: ContextVar[Optional[str]] = ContextVar("openrag_sampling_reason", default=None)
_span_stack: ContextVar[Tuple[Optional[str], ...]] = ContextVar(
    "openrag_span_stack",
    default=(),
)

_VARS = {
    "trace_id": _trace_id,
    "span_id": _span_id,
    "trace_type": _trace_type,
    "workspace_id": _workspace_id,
    "user_id": _user_id,
    "file_id": _file_id,
    "task_id": _task_id,
    "eval_run_id": _eval_run_id,
    "eval_query_id": _eval_query_id,
    "sampling_reason": _sampling_reason,
}

_MISSING = object()


def set_trace_context(
    *,
    trace_id: Any = _MISSING,
    span_id: Any = _MISSING,
    trace_type: Any = _MISSING,
    workspace_id: Any = _MISSING,
    user_id: Any = _MISSING,
    file_id: Any = _MISSING,
    task_id: Any = _MISSING,
    eval_run_id: Any = _MISSING,
    eval_query_id: Any = _MISSING,
    sampling_reason: Any = _MISSING,
) -> Dict[str, Any]:
    """Set provided trace context fields and return the current context."""

    values = {
        "trace_id": trace_id,
        "span_id": span_id,
        "trace_type": trace_type,
        "workspace_id": workspace_id,
        "user_id": user_id,
        "file_id": file_id,
        "task_id": task_id,
        "eval_run_id": eval_run_id,
        "eval_query_id": eval_query_id,
        "sampling_reason": sampling_reason,
    }
    for key, value in values.items():
        if value is not _MISSING:
            _VARS[key].set(value)
            if key == "span_id":
                _span_stack.set(())
    return get_trace_context()


def get_trace_context() -> Dict[str, Any]:
    """Return the current trace context as a plain dict."""

    return {key: var.get() for key, var in _VARS.items()}


def reset_trace_context() -> None:
    """Clear all trace context fields for the current execution context."""

    for var in _VARS.values():
        var.set(None)
    _span_stack.set(())


def push_span(span_id: Optional[str] = None) -> str:
    """Push a span id and make it the active span."""

    next_span_id = span_id or uuid.uuid4().hex
    _span_stack.set(_span_stack.get() + (_span_id.get(),))
    _span_id.set(next_span_id)
    return next_span_id


def pop_span() -> Optional[str]:
    """Pop the active span id and restore its parent span."""

    current_span_id = _span_id.get()
    stack = _span_stack.get()
    if stack:
        previous_span_id = stack[-1]
        _span_stack.set(stack[:-1])
        _span_id.set(previous_span_id)
    else:
        _span_id.set(None)
    return current_span_id
