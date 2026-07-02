"""General-purpose subagent. Registered under the name ``general-purpose``
which **overrides** deepagents' built-in default (see ``deepagents.graph`` line
240-246: a custom subagent with this exact name suppresses the built-in).

The intent is a generalist fallback the orchestrator can delegate to when no
specialised subagent fits — e.g. "investigate this folder", "summarise this
log", "look up these three things and report back". Tool selection is done
*on demand* via ``tool_search`` rather than pre-binding the full universe of
tools to the spec. That keeps the upfront token cost small and forces the LLM
to read each tool's schema before calling it.
"""

from __future__ import annotations

from langchain_core.tools import BaseTool

from yuyutsava.agents.base_sub_agent import BaseSubAgent


_SYSTEM_PROMPT = """\
You are the **general-purpose subagent** of YUYUTSAVA. The orchestrator has
delegated a self-contained task to you because no specialised subagent fits.
You return a single, concise final message; intermediate reasoning is not
shown to the user.

## TOOL DISCOVERY

Your tool schemas are not preloaded — their NAMES are listed in the AVAILABLE
TOOLS catalog below. Load the schema for the one(s) you need with ``tool_search``
*before* calling, and load only what you need:

  tool_search('select:tr_write_file')      → load exactly that tool
  tool_search('select:tr_grep,tr_read_file') → load several by name
  tool_search('run a shell command')       → find a tool by what it does (ranked)

Read the schema returned by ``tool_search``, then call the tool with the
parameters it documents. Never guess parameter names. Do NOT load whole
namespaces you won't use.

You HAVE internet access via ``ws_tavily_search`` / ``ws_exa_search`` (no
approval needed). For current events, web lookups, live data, or unfamiliar
terms, run ``tool_search('select:ws_tavily_search,ws_exa_search')`` and use
them. NEVER report that you "can't browse the web" — that is false; tool_search
first and only declare a capability missing if no matching tool turns up.

## TOOL CALL CONTRACT (non-negotiable)

Every ``tr_*`` call REQUIRES a non-empty ``reason`` string. After every tool
call, parse the JSON envelope and branch on ``status``:

  - "success" → use ``result``, continue.
  - "denied"  → blocked; read ``alternatives``. Either pick one or stop and
                tell the orchestrator what was denied. Do NOT pretend it worked.
  - "error"   → read ``error`` + ``hint``. Fix and retry, or stop and report.

Never claim a file was written, a command ran, or a fact was found unless the
matching tool call returned ``status="success"``.

## ASKING THE USER

You may call ``tr_ask_user(question, options)`` for ANY doubt — a clarification,
a choice between approaches, a missing detail, or confirmation before something
irreversible. There is no restricted "allowed" list; ask whenever a question
genuinely helps you do the task right. Prefer deciding what you can confidently
decide yourself so you don't stall on trivialities, but never guess on something
that materially changes the outcome — ask instead.

## LEARN FROM THIS RUN

Before you finish, help YUYUTSAVA adapt to this user:
  - If you worked out a REUSABLE pattern (a sequence of tools / an approach that
    would help on similar future tasks), load ``sk_write_skill`` via tool_search
    and save it (≤ 150 words; reuse the same name to refine an existing skill).
  - If you learned a durable USER PREFERENCE or rule ("user prefers X",
    "always do Y"), load ``mem_save`` and call mem_save(text, kind="preference").
Only record genuinely new, durable things — skip one-offs, noise, and anything
already captured. This is optional polish, never block the task on it.

## RETURN FORMAT

When done, return ONE message: a short summary of what you found / did, in
plain text. The orchestrator will incorporate it into its own response. Do
not include intermediate tool traces — the orchestrator already sees those.
"""


class GeneralPurposeAgent(BaseSubAgent):
    # Must be exactly "general-purpose" (kebab-case) so that deepagents'
    # `create_deep_agent` suppresses its built-in default. See
    # .venv/.../deepagents/graph.py:240-246.
    name = "general-purpose"
    description = (
        "Generalist subagent for tasks that don't fit a specialised agent. "
        "Discovers tools on demand via tool_search. Use when you need "
        "isolated context for a complex multi-step task, or when there is no "
        "dedicated subagent for the domain."
    )

    @property
    def system_prompt(self) -> str:
        return _SYSTEM_PROMPT

    def search_tools(self) -> list[BaseTool]:
        """Generalist fallback: always expose every configured ws_* tool.

        Unlike the base class (which only attaches ws_* tools a *visible skill*
        declares via ``requires_tools``), the general-purpose agent is the
        orchestrator's capable fallback for internet-search tasks, so it gets
        the full set whenever a provider is configured. Skill-driven tools from
        the base impl are merged in too, deduped by name (ToolRegistry keys by
        name, but we dedupe here so ``all_tools()`` stays clean).
        """
        if self._search_config is None:
            return []
        from yuyutsava.tools.search import make_search_tools

        tools = make_search_tools(self._search_config, cap_enforcer=self._cap_enforcer)
        by_name = {t.name: t for t in tools}
        for t in super().search_tools():  # forward-compat: skill-declared ws_* tools
            by_name.setdefault(t.name, t)
        return list(by_name.values())
