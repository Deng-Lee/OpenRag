"""Test bootstrap: ensure `openrag` and `common` are importable."""

import os
import sys
from pathlib import Path

# Ensure L0/L1 retrieval is enabled by default in tests.
# docker/.env sets OPENRAG_RETRIEVAL_USE_L0_L1=false (for non-Milvus deployments)
# but config.py loads it via load_dotenv (no override), so setting it here first
# (before config.py is imported) pins it to "true" for tests that rely on it.
os.environ.setdefault("OPENRAG_RETRIEVAL_USE_L0_L1", "true")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"

# Repo root: `common.*` (e.g. common.token_utils) lives beside `src/`.
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
