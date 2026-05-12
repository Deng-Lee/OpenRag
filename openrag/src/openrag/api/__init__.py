"""API module for OpenRag"""

from openrag.api.main import app
from openrag.api.search_api import router

__all__ = ["app", "router"]
