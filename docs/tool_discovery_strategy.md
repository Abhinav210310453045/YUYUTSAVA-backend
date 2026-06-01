# Tool Discovery Strategy — Improving Lazy `tool_search`

**Status:** Proposal. Not implemented. For review before any code changes are scoped.
**Scope:** The `ToolRegistry` lazy-discovery mechanism — how tool schemas are served to the LLM on demand. This is about *which tool definitions enter the context*, NOT about file/shell search behavior (that is a separate concern).
**Related code:**
- [yuyutsava/core/tool_registry.py](../yuyutsava/core/tool_registry.py) — `ToolRegistry`, `search()`, `schema_block()`, `make_tool_search_tool()`
- [yuyutsava/core/tool_filter_middleware.py](../yuyutsava/core/tool_filter_middleware.py) — prefix/name suppression of tool schemas
- [yuyutsava/core/prompts.py](../yuyutsava/core/prompts.py) — `_TOOL_DISCOVERY_SECTION`
- [yuyutsava/core/engine.py](../yuyutsava/core/engine.py) — `_build_tool_registry_and_tools()`
- [yuyutsava/agents/base_sub_agent.py](../yuyutsava/agents/base_sub_agent.py) — same pattern for sub-agents

---

## 1. The problem in one paragraph

Tools are hidden from the LLM upfront and discovered on demand via `tool_search(pattern)`. But discovery retrieves by **fnmatch on the name prefix** and returns the **full schema of every match**. So the retrieval unit is the whole tool family: an agent that needs to read one file calls `tool_search('tr_*')` and receives the complete JSON schema of every `tr_*` tool — read, write, delete, execute, grep, ls, glob, ask_user, execute_in_sandbox. Context cost scales with **family size**, not with **what the task actually needs**. Today that's tolerable because each family is small. The day TaskRunner has 20 tools, "read one file" pays for 20 schemas — and the cost is paid on every agent and every sub-agent that shares the pattern. This is a scaling bug baked into the retrieval granularity, not a tuning issue.

## 2. What the current mechanism does (precise mental model)

Two stages, today:

1. **Before search** — `ToolFilterMiddleware` strips every `tr_* / ws_* / sk_* / fo_* / ev_* / db_*` tool and the deepagents built-ins from the model's tool list on every LLM call. The model sees only `tool_search` plus the prose hints in `_TOOL_DISCOVERY_SECTION` (prefix patterns + a comma-separated list of bare verb names — no descriptions, no parameters).
2. **After `tool_search(pattern)`** — `ToolRegistry.search()` does `fnmatch.fnmatchcase(name, pattern)` over the registry and `schema_block()` renders `{name, description, full JSON parameter schema}` for **all** matches into the result.

The defect lives entirely in step 2: `search()` returns the whole match set ([tool_registry.py:57-62](../yuyutsava/core/tool_registry.py#L57-L62)) and `schema_block()` serializes all of them ([tool_registry.py:64-77](../yuyutsava/core/tool_registry.py#L64-L77)). Grouping (the `tr_`/`ws_` prefix convention) is being used as the retrieval key, when it should only be a namespace.

## 3. What the industry actually does (the target behavior)

This is a solved problem with a clear convergent answer: **return the top 3-5 relevant tools per request, never the whole family.**

1. **Anthropic Tool Search Tool** (server-side, GA Nov 2025). Tools are marked `defer_loading: true`; the model sees only the search tool + a few non-deferred "hot" tools. Search runs over **names + descriptions + argument names + argument descriptions** and **returns the 3-5 most relevant tools**. Two backends: `bm25` (natural-language query, lexical ranking) and `regex` (Python `re.search` patterns — the closest cousin to today's fnmatch, but still capped at 3-5 results). Reported: ~85% token reduction; tool-selection accuracy jumps once you exceed ~30-50 tools.
2. **MCP / Cursor optimizers** sit in front of large multi-server tool sets and use **semantic (embedding) retrieval** to surface a handful of tools per request instead of dumping every server's catalog.
3. **Research** (RAG-MCP, Tool-to-Agent Retrieval, vector-based MCP tool selection) embeds tool descriptions in a vector store, embeds the task, returns **top-k by cosine similarity**. They name the current failure mode explicitly: "prompt bloat" / "context dilution" from collapsing many tools into one coarse retrieval bucket.

The principle is just-in-time retrieval: discovery should return tools proportional to the *need*, not the *namespace*.

## 4. Design pillars (directions, not implementation steps)

Each pillar is a direction. They compose; they are not mutually exclusive.

### Pillar 1 — Top-k retrieval, not whole-family

The single highest-leverage change. `ToolRegistry.search()` must rank matches and return the top-k (default ~5), not every match. This one edit decouples context cost from family size and is the property we actually want: "read one file" loads ~1 tool whether TaskRunner has 8 tools or 80.

- **Lexical first (BM25 / TF-IDF):** score each tool's `name + description + parameter names` against a query, return top-k. This is Anthropic's `bm25` variant. `rank_bm25` is a tiny pure-Python dep, or hand-roll TF-IDF over the already-rendered fields. Best effort-to-payoff ratio; no model calls.
- **Semantic later (embeddings):** embed each tool description once at registry build, embed the agent's natural-language need at query time, return top-k by cosine. This is the RAG-MCP / Cursor approach; it matches "I need to read a file" → `tr_read_file` even when wording diverges. Cache embeddings on disk keyed by a hash of name+description so the build cost is paid once.

Keep `fnmatch` as an optional exact/glob mode (`tool_search('tr_read*')` is still useful for a model that knows the name), but make ranked top-k the default path.

### Pillar 2 — Two-tier schema serving (browse cheap, pay on commit)

Split discovery into a cheap browse and an expensive fetch:

- `tool_search(query)` returns only `name + one-line description` (+ optionally required-arg names) for top-k matches — cheap enough that returning a few extra candidates is harmless.
- A second tool, `tool_inspect(names)`, returns the **full JSON parameter schema** only for the specific tools the model commits to calling.

This decouples *browse cost* from *family size entirely* and mirrors how Claude Code's own deferred-tool list works (you see names in a system-reminder, fetch full schemas via a search step). The model browses for pennies and pays full schema cost only for the 1-2 tools it will actually call.

### Pillar 3 — Promote hot tools to non-deferred

Orthogonal and complementary. Anthropic recommends keeping the **3-5 most-used tools loaded upfront** (`defer_loading: false`). `tr_read_file`, `tr_ls`, `tr_glob`, `tr_grep` are called in nearly every task — if they're always visible, the common case needs *zero* discovery round-trips (saving both tokens and a turn of latency). `ToolFilterMiddleware` already has the suppression machinery; this is an allowlist that bypasses suppression for a named hot set, plus including those tools in `startup_tools`.

**Open question:** which tools belong in the hot set, and should it be per-agent (the orchestrator's hot set differs from TaskRunner's)?

### Pillar 4 — Pluggable retrieval backend behind one interface

`search()` should dispatch to a strategy (`fnmatch` | `bm25` | `embedding`) chosen by config, so we can ship lexical now and swap in embeddings later without touching `make_tool_search_tool()`, the middleware, or the prompt. The `tool_search` tool's contract to the LLM stays identical regardless of backend.

### Pillar 5 — Prompt + docstring alignment

`_TOOL_DISCOVERY_SECTION` currently teaches prefix-pattern discovery (`tool_search('tr_*')`). If discovery becomes query-driven top-k, the prompt should teach **describe-what-you-need** ("call `tool_search` with a short description of the operation, e.g. `tool_search('read a file')`") rather than memorizing prefixes. Tool descriptions should carry semantic keywords matching how tasks are phrased, since both lexical and embedding backends search the description text. Principles age well across model upgrades; prefix incantations don't.

## 5. Recommended sequencing

1. **Pillar 1 (lexical top-k) + Pillar 3 (hot set).** Self-contained, at most one tiny dependency, biggest immediate win. Wire through `ToolRegistry.search()`, the middleware allowlist, the `startup_tools` list, and the prompt in one pass. After this, "read one file" loads ~0-1 extra tools regardless of family size.
2. **Pillar 2 (two-tier `tool_inspect`).** Kills browse cost even for large candidate sets.
3. **Pillar 4 + embedding backend.** Higher recall behind the same interface, only if lexical recall proves insufficient in practice.

## 6. Acceptance criteria

- A task that needs exactly one tool loads at most that tool's schema (plus the hot set), independent of how many tools share its prefix.
- Adding a new `tr_*` tool does not increase the context cost of unrelated tasks.
- The `tool_search` contract to the LLM is backend-agnostic; switching `fnmatch → bm25 → embedding` is a config change.
- Common file ops (`read`/`ls`/`grep`/`glob`) require no `tool_search` round-trip at all.
- The change applies uniformly to the CLI agent and every sub-agent (both build registries the same way).

## 7. Open questions

- Default `k` for top-k retrieval (Anthropic uses 3-5).
- Hot set membership, and whether it is global or per-agent.
- Lexical vs. embedding for v1 — does the project already have a cheap embeddings provider wired (search infra exists for Tavily/Exa), or do we stay pure-Python for now?
- Should `fnmatch` exact patterns remain a supported power-user path, or be removed once ranked search lands?

## References

- [Anthropic — Tool search tool (API docs)](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool)
- [Anthropic — Introducing advanced tool use](https://www.anthropic.com/engineering/advanced-tool-use)
- [RAG-MCP: Mitigating Prompt Bloat in LLM Tool Selection](https://arxiv.org/html/2505.03275v1)
- [Tool-to-Agent Retrieval](https://arxiv.org/html/2511.01854)
- [Stacklok MCP Optimizer vs Anthropic's Tool Search Tool](https://stacklok.com/blog/stackloks-mcp-optimizer-vs-anthropics-tool-search-tool-a-head-to-head-comparison/)
