"""Test bootstrap: ensure `openrag` and `common` are importable."""

import os
import sys
from pathlib import Path

# Set the default to "true" if not already set in the environment. config.py loads
# docker/.env (which sets this to "false") on import of search_api; this keeps unit
# tests aligned with the code's own default (true) without overriding an explicit env.
os.environ.setdefault("OPENRAG_RETRIEVAL_USE_L0_L1", "true")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"

# Repo root: `common.*` (e.g. common.token_utils) lives beside `src/`.
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
