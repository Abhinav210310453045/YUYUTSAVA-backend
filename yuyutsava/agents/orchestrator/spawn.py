"""Orchestrator-side ``spawn_subagent`` tool.

.. warning::

   **ABANDONED DESIGN — DO NOT REGISTER THIS TOOL.**

   ``make_spawn_subagent_tool`` is deliberately never wired into any agent. See
   the explicit non-registration at ``core/engine.py`` in ``build_orchestrator``
   ("spawn_subagent is intentionally NOT registered"). The orchestrator delegates
   dynamic work to the ``general-purpose`` subagent instead, which is
   checkpointed, resume-able, and budget-governed — none of which the design
   below achieves.

   The module is retained for reference because the depth-cap and audit-log
   mechanics are worth reading, not because the tool is pending re-enablement.
   **If you are here because you found an unused 167-line tool and wondered
   whether to wire it up: don't.** Use the ``task('general-purpose', …)``
   delegation path.

   Recorded here because this constraint previously existed only outside the
   repository, which is exactly how an abandoned design gets accidentally
   revived. See docs/architecture-review/03-findings-dry-kiss.md (``F-K04``).

Lets the orchestrator build a fresh, throwaway ReAct agent at call time with an
explicit tool subset. The child is run synchronously inline; its final
assistant message is returned as the tool result.

## v1 scope and limitations

This is the **first** version of spawn_subagent. To keep the resume semantics
sane, **tools that emit user-facing interrupts are rejected upfront**:

  - ``tr_write_*`` / ``tr_delete_*`` / ``tr_execute`` / ``tr_ask_user``

Those would suspend the child mid-run, and the parent's LangGraph resume
mechanism would re-call ``spawn_subagent`` from scratch on the user's reply —
losing the child's state. A future v2 may support resume-able children by
attaching a checkpointer scoped to the child thread; v1 keeps the contract
"if the orchestrator wants to spawn, give it read/research-only tools".

The orchestrator is still free to call ``ask_user`` itself BEFORE spawning, to
collect any decisions the child would otherwise have asked for.

## What you get

  - Depth cap: a spawned agent cannot itself spawn beyond ``max_depth`` levels
    of ``agent_path`` nesting (default 2).
  - Audit log: every successful spawn is written to ``decisions`` with
    ``outcome="spawn_subagent"`` so the timeline shows what happened.
  - agent_path inheritance: the child sees
    ``agent_path = "<parent>/spawned:<name>"``, so any (non-rejected) tool that
    *would* have emitted an interrupt would still attribute correctly.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import uuid
from pathlib import Path

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent

from yuyutsava.agents.db_tools import make_db_tools
from yuyutsava.agents.task_runner.tools import bind_tools
from yuyutsava.core.agent_context import current_context
from yuyutsava.core.config import SearchConfig
from yuyutsava.core.streaming import flatten_content
from yuyutsava.core.tool_registry import ToolRegistry
from yuyutsava.storage.events import Store
from yuyutsava.storage.events.roles import DecisionWriter
from yuyutsava.skills.registry import SkillRegistry
from yuyutsava.skills.tools import make_read_skill_tool
from yuyutsava.tools.search import make_search_tools

logger = logging.getLogger("yuyutsava.agents.orchestrator.spawn")


# Tools whose execution can call interrupt() — disallowed in spawn v1 because
# the parent re-runs the tool on resume, which would restart the child.
_INTERRUPTING_PREFIXES: tuple[str, ...] = (
    "tr_write_", "tr_delete_", "tr_execute",
)
_INTERRUPTING_EXACT: frozenset[str] = frozenset({"tr_ask_user"})


_SPAWN_PROMPT = """\
You are a one-shot spawned subagent. The orchestrator launched you with a
specific task and an explicit, narrow toolset.

## TOOLS

You have a fixed toolset (no more, no less), listed below:
{tool_catalog}
Use ``tool_search('select:<name>')`` to load a tool's full schema before
calling it (do NOT guess parameters). Then execute.

## CONTRACT

  - Do not ask the user questions — your toolset cannot produce interrupts.
  - Do not invent capabilities you don't have. If your tools cannot complete
    the task, return a clear message saying what's missing.
  - When done, return ONE concise final message. Intermediate tool traces are
    invisible to the orchestrator.

## ON FAILURE

If a tool returns ``status="denied"`` or ``status="error"``, do NOT pretend it
succeeded. Return a final message stating exactly what was blocked and why.
"""


def _candidate_tools(
    *,
    workspace_root: Path,
    search_config: SearchConfig | None,
    skill_registry: SkillRegistry | None,
    cap_enforcer: object | None,
    agent_name: str,
) -> list[BaseTool]:
    """Assemble the universe of tools a spawned child could potentially use.

    Filtering by ``tool_globs`` happens after this — we just produce the
    candidate set so a glob can match. The caller is responsible for the
    ``_INTERRUPTING_*`` reject pass and for ensuring the model has the right
    permissions to use whatever ends up selected.
    """
    tools: list[BaseTool] = list(
        bind_tools(workspace_root, agent_name=f"spawned:{agent_name}")
    )
    if search_config is not None:
        tools.extend(make_search_tools(search_config, cap_enforcer=cap_enforcer))
    if skill_registry is not None:
        # Spawned agents get read-only skill access. Writing skills is reserved
        # for long-lived subagents that learn over time.
        tools.append(make_read_skill_tool(skill_registry))
    # db_* tools are read-only by construction; always available.
    tools.extend(make_db_tools())
    return tools


def make_spawn_subagent_tool(
    *,
    model: BaseChatModel,
    workspace_root: Path,
    store: DecisionWriter,
    search_config: SearchConfig | None = None,
    skill_registry: SkillRegistry | None = None,
    cap_enforcer: object | None = None,
    max_depth: int = 2,
    recursion_limit: int = 40,
) -> BaseTool:
    """Build the ``spawn_subagent`` orchestrator tool.

    The tool builds a fresh ReAct agent on each call with the exact subset of
    tools matching ``tool_globs``. Tools that emit ``interrupt()`` (writes,
    deletes, host shell, ask_user) are rejected — see the module docstring for
    why.
    """

    @tool
    async def spawn_subagent(
        task: str,
        tool_globs: list[str],
        name: str = "anon",
        recursion: int | None = None,
    ) -> str:
        """Launch a one-shot child agent with an explicit tool subset.

        Use when no specialised subagent fits and you want fine-grained control
        over what the child can see. The child is **read/research-only** in
        this version — write/delete/execute/ask_user tools are rejected.

        Args:
            task: A detailed task description. The child sees only this and
                its tools; nothing else from your conversation context.
            tool_globs: List of fnmatch patterns selecting which tools the
                child gets (e.g. ``["tr_read_*", "ws_*"]``). At least one
                tool must match.
            name: Short identifier used in the child's agent_path
                (``orchestrator/spawned:<name>``). Default ``"anon"``.
            recursion: Optional recursion limit for the child (default 40).
                Lower this for tightly-scoped lookups; raise for multi-step
                research.

        Returns:
            JSON envelope: ``{"status": "success", "result": {"text": ...}}``
            on success, or ``{"status": "error", "error": ..., "hint": ...}``.
        """
        ctx = current_context()
        parent_path = ctx.get("agent_path") or "orchestrator"
        session_id = ctx.get("session_id")

        # Depth cap — count '/' separators in the parent's path.
        depth = parent_path.count("/")
        if depth >= max_depth:
            return json.dumps({
                "status": "error",
                "error": f"spawn depth limit reached (depth={depth}, max={max_depth})",
                "hint": "Reorganise so the work is delegated by the top-level orchestrator instead of a nested spawn.",
            })

        if not tool_globs:
            return json.dumps({
                "status": "error",
                "error": "tool_globs must be non-empty",
                "hint": "Pass an explicit list, e.g. ['tr_read_*', 'ws_*'].",
            })

        # Resolve candidate tools and filter by globs.
        candidates = _candidate_tools(
            workspace_root=workspace_root,
            search_config=search_config,
            skill_registry=skill_registry,
            cap_enforcer=cap_enforcer,
            agent_name=name,
        )
        selected: list[BaseTool] = []
        for t in candidates:
            if any(fnmatch.fnmatchcase(t.name, g) for g in tool_globs):
                selected.append(t)
        if not selected:
            available = ", ".join(sorted({t.name for t in candidates})[:30])
            return json.dumps({
                "status": "error",
                "error": f"no tools matched globs {tool_globs}",
                "hint": f"Available (sample): {available}",
            })

        # Reject interrupting tools — see module docstring.
        bad = [
            t.name for t in selected
            if t.name in _INTERRUPTING_EXACT
            or any(t.name.startswith(p) for p in _INTERRUPTING_PREFIXES)
        ]
        if bad:
            return json.dumps({
                "status": "error",
                "error": f"these tools may emit user prompts and are not allowed in spawn_subagent v1: {sorted(set(bad))}",
                "hint": "Use ask_user yourself before spawning, or narrow tool_globs to read/research-only patterns (e.g. ['tr_read_*', 'tr_grep', 'tr_execute_in_sandbox', 'ws_*', 'sk_read*', 'db_*']).",
            })

        # Build the child. Each spawned agent gets its own ToolRegistry so the
        # tool_search inside the child sees ONLY what the orchestrator selected.
        registry = ToolRegistry()
        registry.register_many(selected)
        child_tools = [registry.make_tool_search_tool()] + selected
        child = create_react_agent(
            model=model,
            tools=child_tools,
            prompt=_SPAWN_PROMPT.format(tool_catalog=registry.catalog_block()),
            checkpointer=MemorySaver(),
        )

        child_tid = f"{session_id or 'sess'}:spawn-{uuid.uuid4().hex[:8]}"
        child_path = f"{parent_path}/spawned:{name}"
        cfg = {
            "recursion_limit": recursion or recursion_limit,
            "configurable": {"thread_id": child_tid, "agent_path": child_path},
        }

        logger.info(
            "spawn_subagent name=%s globs=%s selected=%d depth=%d",
            name, tool_globs, len(selected), depth,
        )

        try:
            result = await child.ainvoke(
                {"messages": [HumanMessage(content=task)]}, config=cfg,
            )
        except Exception as exc:  # noqa: BLE001 — surface as structured error
            logger.exception("spawn_subagent failed name=%s", name)
            return json.dumps({
                "status": "error",
                "error": f"child agent crashed: {type(exc).__name__}: {exc}",
            })

        msgs = result.get("messages", []) if isinstance(result, dict) else []
        final_text = ""
        for m in reversed(msgs):
            if isinstance(m, AIMessage):
                joined = flatten_content(m.content).strip()
                if joined:
                    final_text = joined
                    break

        # Audit log — even on empty final_text we log the call.
        try:
            await store.put_decision(
                proposal_id=None,
                event_id=session_id or "spawn",
                outcome="spawn_subagent",
                action_summary=(
                    f"name={name} tools={[t.name for t in selected]} "
                    f"final_chars={len(final_text)}"
                )[:300],
                session_id=session_id,
                agent_path=child_path,
            )
        except Exception:  # noqa: BLE001 — audit is best-effort
            logger.exception("spawn audit log failed name=%s", name)

        return json.dumps({
            "status": "success",
            "result": {"text": final_text[:8000], "agent_path": child_path},
        })

    return spawn_subagent
