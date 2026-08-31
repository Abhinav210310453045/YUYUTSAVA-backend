---
name: exa-deep-fetch
description: |
  Deep research combining Exa neural search with full content fetching.
  Use when Tavily snippets are insufficient and full page text is needed
  (e.g. reading full articles, academic papers, documentation pages).
requires_tools:
  - ws_exa_search
  - ws_exa_get_contents
---

## When to use

Use this pattern over ws_tavily_search when:
- You need the complete body of an article or page, not just a snippet.
- You need to filter results by publication date (e.g. last 30 days).
- The query is semantic (conceptual match) rather than keyword-based.

## Pattern

1. Call tool_search('ws_*') to confirm ws_exa_search is available.
2. Call ws_exa_search(query=..., num_results=5, search_type="neural").
   - Use start_published_date / end_published_date for recency filtering (ISO dates).
3. Extract the top 2–3 URLs from response.results[].url.
4. Call ws_exa_get_contents(urls=[...], include_text=True).
5. Parse response.results[].text for the full page body.
6. Synthesize findings and write output with tr_write_file if needed.

## Tools used

ws_exa_search, ws_exa_get_contents, tr_write_file (optional)

## Gotchas

- Limit ws_exa_get_contents to 3 URLs at a time to avoid large responses.
- Some pages block scraping — fall back to ws_tavily_search for those.
- search_type="keyword" for exact phrase matching; "neural" for semantic concepts.
- If ws_exa_search is not in tool_search('ws_*') output, the API key is not set.

## Agent skill tip

After completing a novel search pattern (e.g. filtering arxiv papers by date),
save it as a personal skill with sk_write_skill so future tasks can reuse it.
