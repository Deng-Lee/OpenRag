"""Retrieval module for OpenRag - integrates OpenViking with permission filtering"""

from openrag.retrieval.filters import PermissionFilter
from openrag.retrieval.retrieval_service import RetrievalService

__all__ = ["PermissionFilter", "RetrievalService"]
