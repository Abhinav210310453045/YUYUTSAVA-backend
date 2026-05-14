---
name: tavily-research
description: |
  Research a topic using Tavily web search. Use when a task requires
  current or factual information not available in the workspace files.
  Best for broad questions, recent events, documentation lookups.
requires_tools:
  - ws_tavily_search
---

## When to use

Call ws_tavily_search when:
- The task needs facts or data from the internet (not the local filesystem).
- The user asks "what is", "how does", "find me", or "look up" something.
- You need a quick synthesized answer + sources without reading full pages.

## Pattern

1. Call tool_search('ws_*') to confirm ws_tavily_search is available.
2. Call ws_tavily_search(query=..., max_results=5, include_answer=True).
3. Use response.answer for a synthesized answer (when include_answer=True).
4. Use response.results[].content for source details and citations.
5. If you need to persist the findings, write them with tr_write_file.

## Tools used

ws_tavily_search, tr_write_file (optional)

## Gotchas

- search_depth="advanced" costs more API credits; use "basic" for simple queries.
- topic="news" is better for recent events; "general" for evergreen information.
- Always cite the source URL from response.results[].url in written output.
- If ws_tavily_search is not in tool_search('ws_*') output, the API key is not set.
