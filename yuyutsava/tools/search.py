"""
Web search tools for YUYUTSAVA agents.

Three tools exposed under the ws_* naming prefix (web search):
  ws_tavily_search     — full-text + synthesized-answer search via Tavily
  ws_exa_search        — neural/keyword search via Exa
  ws_exa_get_contents  — fetch full page text by URL list via Exa

Public factory:
  make_search_tools(config: SearchConfig) -> list[BaseTool]
    Returns only the tools whose provider API key is configured.
    Returns an empty list if no keys are present — safe to call unconditionally.

Tool naming follows the ws_* prefix convention so agents can discover them via:
  tool_search('ws_*')
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from langchain_core.tools import BaseTool, tool

from yuyutsava.core.config import SearchConfig
from yuyutsava.core.exceptions import ExaSearchError, TavilySearchError

logger = logging.getLogger("yuyutsava.tools.search")


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


def make_search_tools(config: SearchConfig) -> list[BaseTool]:
    """Build ws_* tools for every provider that has a configured API key.

    Only creates tools whose API key is present in *config*.
    Returns an empty list if no search providers are configured.
    """
    available = config.is_available()
    tools: list[BaseTool] = []

    if available["tavily"]:
        tools.append(_make_tavily_search(config.tavily_api_key))
        logger.debug("search: ws_tavily_search registered")

    if available["exa"]:
        tools.extend(_make_exa_tools(config.exa_api_key))
        logger.debug("search: ws_exa_search + ws_exa_get_contents registered")

    if not tools:
        logger.debug("search: no providers configured — ws_* tools not loaded")

    return tools


# ---------------------------------------------------------------------------
# Tavily
# ---------------------------------------------------------------------------


def _make_tavily_search(api_key: str) -> BaseTool:

    @tool
    async def ws_tavily_search(
        query: str,
        max_results: int = 5,
        search_depth: str = "basic",
        include_answer: bool = True,
        topic: str = "general",
    ) -> str:
        """Search the web using Tavily and return results as JSON.

        Use tool_search('ws_*') to confirm this tool is available before calling.

        Args:
            query:          The search query.
            max_results:    Number of results to return (1–10, default 5).
            search_depth:   "basic" (faster, cheaper) or "advanced" (deeper crawl).
            include_answer: Include a synthesized answer above the results.
            topic:          "general" or "news" (news biases toward recent content).

        Returns:
            JSON string: {answer: str|null, results: [{title, url, content, score}]}
        """
        try:
            from tavily import AsyncTavilyClient
        except ImportError as exc:
            raise TavilySearchError(
                "tavily-python is not installed. Run: pip install tavily-python"
            ) from exc

        client = AsyncTavilyClient(api_key=api_key)
        try:
            response = await client.search(
                query=query,
                max_results=max(1, min(max_results, 10)),
                search_depth=search_depth,
                include_answer=include_answer,
                topic=topic,
            )
        except Exception as exc:
            logger.warning("ws_tavily_search(%r) failed: %s", query, exc)
            raise TavilySearchError(f"Tavily search error: {exc}") from exc

        results = response.get("results", [])
        logger.debug("ws_tavily_search(%r) → %d results", query, len(results))
        return json.dumps({
            "answer": response.get("answer"),
            "results": [
                {
                    "title": r.get("title"),
                    "url": r.get("url"),
                    "content": r.get("content"),
                    "score": r.get("score"),
                }
                for r in results
            ],
        })

    return ws_tavily_search


# ---------------------------------------------------------------------------
# Exa
# ---------------------------------------------------------------------------


def _make_exa_tools(api_key: str) -> list[BaseTool]:

    @tool
    async def ws_exa_search(
        query: str,
        num_results: int = 5,
        search_type: str = "neural",
        start_published_date: str | None = None,
        end_published_date: str | None = None,
    ) -> str:
        """Search the web using Exa's neural or keyword search.

        Use tool_search('ws_*') to confirm this tool is available before calling.
        Follow with ws_exa_get_contents to fetch full page text for top results.

        Args:
            query:                The search query.
            num_results:          Number of results (1–10, default 5).
            search_type:          "neural" (semantic) or "keyword" (exact match).
            start_published_date: ISO date filter, e.g. "2024-01-01".
            end_published_date:   ISO date filter, e.g. "2024-12-31".

        Returns:
            JSON string: {results: [{title, url, published_date, id}]}
        """
        exa = _get_exa_client(api_key)

        kwargs: dict[str, Any] = {
            "num_results": max(1, min(num_results, 10)),
            "type": search_type,
        }
        if start_published_date:
            kwargs["start_published_date"] = start_published_date
        if end_published_date:
            kwargs["end_published_date"] = end_published_date

        try:
            response = await _exa_search(exa, query, **kwargs)
        except Exception as exc:
            logger.warning("ws_exa_search(%r) failed: %s", query, exc)
            raise ExaSearchError(f"Exa search error: {exc}") from exc

        results = getattr(response, "results", [])
        logger.debug("ws_exa_search(%r) → %d results", query, len(results))
        return json.dumps({
            "results": [
                {
                    "title": getattr(r, "title", None),
                    "url": getattr(r, "url", None),
                    "published_date": getattr(r, "published_date", None),
                    "id": getattr(r, "id", None),
                }
                for r in results
            ]
        })

    @tool
    async def ws_exa_get_contents(
        urls: list[str],
        include_text: bool = True,
    ) -> str:
        """Fetch full page text for a list of URLs via Exa.

        Use after ws_exa_search to read the actual content of the top results.
        Limit to 3 URLs at a time to stay within response size limits.

        Args:
            urls:         List of page URLs to fetch (max 3 recommended).
            include_text: Include page text in the response (default True).

        Returns:
            JSON string: {results: [{url, title, text}]}
        """
        if not urls:
            return json.dumps({"results": []})

        exa = _get_exa_client(api_key)

        try:
            response = await _exa_get_contents(exa, urls[:10], text=include_text)
        except Exception as exc:
            logger.warning("ws_exa_get_contents(%d urls) failed: %s", len(urls), exc)
            raise ExaSearchError(f"Exa get_contents error: {exc}") from exc

        results = getattr(response, "results", [])
        logger.debug("ws_exa_get_contents(%d urls) → %d results", len(urls), len(results))
        return json.dumps({
            "results": [
                {
                    "url": getattr(r, "url", None),
                    "title": getattr(r, "title", None),
                    "text": getattr(r, "text", None),
                }
                for r in results
            ]
        })

    return [ws_exa_search, ws_exa_get_contents]


# ---------------------------------------------------------------------------
# Exa client helpers (async + sync fallback)
# ---------------------------------------------------------------------------


def _get_exa_client(api_key: str) -> Any:
    """Return an Exa client instance (AsyncExa if available, else Exa)."""
    try:
        from exa_py import AsyncExa
        return AsyncExa(api_key=api_key)
    except ImportError:
        pass
    try:
        from exa_py import Exa
        return Exa(api_key=api_key)
    except ImportError as exc:
        raise ExaSearchError(
            "exa-py is not installed. Run: pip install exa-py"
        ) from exc


async def _exa_search(client: Any, query: str, **kwargs: Any) -> Any:
    """Call client.search(), handling both async and sync Exa clients."""
    if hasattr(client, "__class__") and "Async" in type(client).__name__:
        return await client.search(query, **kwargs)
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: client.search(query, **kwargs))


async def _exa_get_contents(client: Any, urls: list[str], text: bool = True) -> Any:
    """Call client.get_contents(), handling both async and sync Exa clients."""
    if hasattr(client, "__class__") and "Async" in type(client).__name__:
        return await client.get_contents(urls, text=text)
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: client.get_contents(urls, text=text))
