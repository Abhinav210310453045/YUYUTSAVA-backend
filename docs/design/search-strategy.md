# TaskRunner Search Strategy — Design Proposal

**Status:** Proposal. Not implemented. For review before any code changes are scoped.
**Scope:** Bug 2 from the `the-tr-read-velvet-clarke` plan — agent operational discipline for search.
**Related:** Bug 1 (path-resolution heuristic) is fixed separately at [yuyutsava/agents/task_runner/tools.py](../../yuyutsava/agents/task_runner/tools.py) — see the `_resolve_path` change. This proposal assumes that fix has landed.

---

## 1. The problem in one paragraph

The TaskRunner agent has no operational discipline for search. Given "find my resume on Desktop," it runs `ls -R /Desktop`, gets a multi-MB result, tries to `tr_read_file` it, hits the 100K-char ceiling, retries with `tr_grep`, hits the ceiling again, and only stumbles onto piping through `grep -i` after several turns and multiple user approvals. The same prompt on a bigger model produces the same failure pattern. This is not a model intelligence problem — it's a tool-design and protocol problem. The runtime offers expensive operations as defaults, the prompt offers no principles, and the docstrings document mechanics rather than intent.

## 2. What Claude Code actually does (the precise mental model)

I want to be specific about Claude Code's design because the obvious-looking fix ("blocklist `node_modules` and `.venv`") is *not* what Claude Code actually does. They are different ideas with different implications.

1. **Search exclusion is policy-driven, not name-driven.** Claude Code's Grep/Glob run on ripgrep, which honors:
    - `.gitignore` at every directory level encountered during the walk
    - `.git/info/exclude` and the global gitignore
    - `.ignore` and `.rgignore` files (rg-specific)
    - Hidden files (skipped by default; `--hidden` to include)
    - Binary file detection (skipped automatically)

   This is fundamentally different from "skip `node_modules`". A project that wants to grep its `node_modules` adds an `.rgignore` exception. A project with a vendored dir that's checked in works correctly. A project that doesn't use git falls back to the small built-in defaults (hidden + binary). The policy is the project's, not the tool's.

2. **Results are bounded by counts, not by directory names.** Grep has `head_limit`. Glob caps at a known max. When results exceed the cap, the response says so and tells the agent what to do next.

3. **Tools have narrow purposes and the system prompt enforces choice.** Grep is for content. Glob is for filenames. Read is for known files. Bash is for composition. The prompt is short and principle-stating: "Use Grep, not `bash grep`. Use Glob, not `bash find`."

4. **Large tool outputs become navigable artifacts.** When a tool result exceeds the inline budget, it's stored as a file the agent can re-enter. The agent's next move is `tr_read_file(file, offset=N, limit=M)` or `tr_grep(pattern, file)` — same tools, same vocabulary, just pointed at the artifact instead of the original target.

5. **Docstrings open with intent, not mechanics.** "Use this when X" before "takes parameters Y." The mechanics part is what the model already infers from the schema; the intent is what guides selection.

6. **The prompt does not quote token budgets.** It says "prefer narrow over broad," "filenames before contents," "known directory before unknown directory." Principles age well across model upgrades; numbers don't.

## 3. Five design pillars (directions, not implementation steps)

Each pillar is a direction. Implementation is a separate exercise after the pillars are agreed.

### Pillar 1 — Policy-driven exclusion (not a hardcoded blocklist)

Use a gitignore-aware walker for `tr_glob` and `tr_grep` (recursive mode). Concretely: Python has `pathspec` (used by black, mypy, ruff) or `gitignore-parser`. The walk respects `.gitignore` files at every level. When the search target is outside a git repo (e.g., `~/Desktop`), fall back to a tiny set of universal defaults (skip hidden, skip files > N MB, skip known binary extensions). The agent can override with `respect_ignore=False`.

This trades one piece of clever ignore code for a battle-tested library. It generalizes: every project's conventions are honored automatically. It's also self-documenting — the agent doesn't need a prompt rule saying "skip `node_modules`" because the user's own `.gitignore` already says so.

**Open question:** do you want `respect_ignore=True` (Claude Code's model, changes today's behavior) or `False` (opt-in)?

### Pillar 2 — Cost-bounded results with structured follow-up

Replace any notion of "result too big, suppressed" with "result is bounded, here's the handle to navigate it." Two-part contract:

- Every search tool has a `max_results` (and for grep, `max_matches`) with a hard ceiling enforced silently. The response always includes `was_capped: bool` and the count seen vs returned.
- When the actual data exceeds the inline budget, the tool persists the full result to a file under a dedicated, documented path (call it `_tool_artifacts/<request_id>/...` or similar) and the response returns `{summary, artifact_path, navigation_hints}`. The artifact is a first-class navigable object — the same `tr_grep` / `tr_read_file` work on it.

Large results are not garbage to discard; they are intermediate artifacts to query.

### Pillar 3 — Intent in tool docstrings, not mechanics

Docstrings today document parameters. They should also state purpose in one sentence. Format:

```python
"""Use this when <specific situation>. <one-sentence return summary>.

Args:
    ...
"""
```

Example for `tr_glob`:

```python
"""Use this when you know what filename pattern to look for in a known directory.
Returns matching files (no content). For content search, use tr_grep.

Args: ...
"""
```

No "RECURSION RULES" wall of text. No prompt-section duplication. One sentence of intent, then the schema.

The current docstrings ([tools.py:111-443](../../yuyutsava/agents/task_runner/tools.py#L111-L443)) already cover mechanics correctly; the change is additive — one opening sentence per tool, plus removing the now-stale "convert virtual ls/glob paths first" lines once Bug 1 lands.

### Pillar 4 — Principle-based prompt, not rule-based

Replace any "decision tree" or "cost ladder" in the system prompt with a short principle list. Something like:

> When you need to find a file, name it before reading it. When you need to find content, narrow the file set first. When you don't know the directory, ls one level. When you don't know what to look for, ask.

No numbers. No "you have N tokens." No "`tr_glob` costs M." Principles travel well across models; specifics rot.

A long "TOOL SELECTION STRATEGY" prompt section is the anti-pattern — it's a rule manual. Better: a four-line principle paragraph, plus the intent-sentence on each tool's docstring (Pillar 3), plus the runtime defaults (Pillar 1) doing the heavy lifting.

### Pillar 5 — Failure pedagogy in responses, not in prompt

When a tool fails or returns no useful result, the response is the teaching moment, not the prompt. Examples:

- `FileNotFoundError`: include `parent_exists: bool` and `nearest_existing_parent: str` so the agent's next move is obvious.
- Zero results from a search: include a `hint` field suggesting a broader pattern or a different tool.
- Capped results: include `was_capped: true` plus `try_next: "narrow the pattern" | "scope to subdirectory"`.

Same principle as Pillar 4 but at the data layer: don't make the prompt teach the agent everything; let failures teach lessons in context. The agent reads tool errors more carefully than it reads system prompts at turn 30 of a session.

## 4. What this proposal explicitly is NOT

- **Not** a hardcoded `BLOAT_DIRS` set. Pillar 1 supersedes it.
- **Not** quoting token budgets in the system prompt. Pillar 4 supersedes it.
- **Not** blacklisting `/large_tool_results/*`. Pillar 2 supersedes it — make the artifacts queryable instead.
- **Not** a long "TOOL SELECTION STRATEGY" prompt section. Pillar 4 supersedes it.
- **Not** changing `tr_grep`'s recursive default in this proposal. That's a question (below) — I want a read before deciding.

## 5. Open questions

Before any Bug 2 code lands, I'd want answers to:

1. **Pillar 1 default**: `respect_ignore=True` (Claude Code-style, changes today's behavior) or `False` (opt-in)?
2. **Pillar 2 storage**: is `_tool_artifacts/` under the workspace acceptable, or should artifacts live in the sandbox / a separate dir managed by the daemon? Where are they cleaned up?
3. **`tr_grep` recursive default**: stay recursive (today), or flip to non-recursive with `recursive=True` opt-in (Claude Code's `find . -type f | xargs grep` is explicit recursion)?
4. **Library choice for gitignore parsing**: `pathspec` (well-known, used by black/ruff/mypy) or shell out to `git check-ignore` when in a repo? Lean `pathspec` — pure Python, no subprocess, no `git` dependency.

## 6. Suggested implementation order (after pillars are approved)

A separate PR per pillar, in this order:

1. **Pillar 3 (docstrings)** — lowest risk, cheapest tokens. Immediate small wins.
2. **Pillar 5 (failure pedagogy in responses)** — code change, no behavior surprise.
3. **Pillar 1 (gitignore-aware exclusion)** — biggest behavior change, biggest payoff.
4. **Pillar 2 (artifact navigation)** — depends on Pillar 1 to keep result sizes sensible.
5. **Pillar 4 (prompt rewrite)** — last, because by this point most discipline is enforced by the runtime, so the prompt can be terse.

No PR until the pillars are reacted to. This document is the gate.
