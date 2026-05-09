"""
Custom exceptions for the YUYUTSAVA agent framework.

Hierarchy:
    YuyutsavaError
      └── SearchError
            ├── TavilySearchError
            └── ExaSearchError
"""

from __future__ import annotations


class YuyutsavaError(Exception):
    """Base exception for all YUYUTSAVA errors."""


class SearchError(YuyutsavaError):
    """Raised when a web search tool encounters an unrecoverable error."""


class TavilySearchError(SearchError):
    """Raised when the Tavily API call fails or is misconfigured."""


class ExaSearchError(SearchError):
    """Raised when the Exa API call fails or is misconfigured."""
