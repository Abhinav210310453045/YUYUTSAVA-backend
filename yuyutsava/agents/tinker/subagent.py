"""Background TinkerAgent — the ``tinker-bg`` async subagent of the master.

The interactive TinkerAgent (``agent.py`` / ``core.engine.build_tinker_agent``)
is one deepagent bundle PER CARD: tr_* bound to the card's workspace, the card
baked into the prompt, the conversation pinned to ``todo:<card_id>``. That shape
cannot be hosted as an async subagent: deepagents' ``start_async_task`` launches
a run on a graph compiled ONCE at host boot and passes only a free-text
``description`` — no config reaches the graph, so a per-card compile is
unreachable, and the run executes on a fresh thread inside the langgraph host's
own runtime/checkpointer, so it cannot join the card's interactive
``todo:<card_id>`` thread either (nor should it — a bg job racing a live card
chat on one checkpoint thread would corrupt both).

So the background variant is ONE board-scoped :class:`BaseSubAgent` that keeps
the card semantics at the *tool* level instead:

* the target card travels in the task description (the master is told to
  include the ``tdo_…`` id); the agent resolves it at runtime via ``todo_get``;
* tr_* is bound to the board root (``blobs/todoboard/``), so every card
  workspace is inside its permission zone and the prompt directs file output
  into the target card's own directory;
* all durable output lands ON THE CARD through the exchange (``todo_add_note``
  / ``todo_attach_artifact``, author="tinker") — exactly where the interactive
  tinker and the UI will see it. The bg thread itself is scratch.

HITL asks from a run surface through the existing plumbing: the
``AsyncTaskHealthWatcher`` routes run interrupts to ``ChannelRouter.post_ask``,
and completion wakes the master via the ``subagent_completed`` bridge.
"""

from __future__ import annotations

from langchain_core.tools import BaseTool

from yuyutsava.agents.base_sub_agent import BaseSubAgent
from yuyutsava.agents.task_runner.agent import TaskRunnerAgent


_SYSTEM_PROMPT = """\
You are the **background TinkerAgent** of YUYUTSAVA. The master agent has
delegated a tinkering job on ONE card of the user's TODO board: refine a rough
idea into a sharper one, decompose a goal into small objectives, research open
questions, or produce a supporting artifact. You work unattended and report
back one concise summary.

## THE CARD IS YOUR DELIVERABLE

The task description names the target card by id (`tdo_…`). Work on the board
ONLY through the todo_* tools:

1. Start with `todo_get(card_id)` to load the card — title, existing notes,
   attachments, and its workspace directory path. If the description has no id,
   find the card with `todo_list`/`todo_recall`; if none matches, stop and
   report that instead of guessing.
2. The chat thread you are running on is scratch and will never be seen again —
   the CARD is the durable surface. Persist every insight worth keeping as a
   focused note (`todo_add_note`): a sharpened problem statement, decisions,
   a short list of concrete next objectives, findings with sources. One note
   per insight, not a transcript dump.
3. Files you produce (research summaries, diagrams via vis_*) belong inside the
   card's own workspace directory (from todo_get) — never elsewhere — and are
   attached with `todo_attach_artifact`.
4. Keep the card honest: retitle via `todo_update` when the idea sharpens,
   `todo_set_status` when work genuinely starts or finishes.

## HOW TO TINKER

Sharpen, don't bloat: hand back an IMPROVED version of the idea — tightened
wording, exposed assumptions, the 3-6 smallest concrete next objectives — not
an essay. Research (ws_* search) only what actually reduces uncertainty.
`todo_recall(query)` searches the whole board's notes semantically — check it
before re-deriving something the user already decided on another card.

## TOOL DISCOVERY

Your tool schemas are not preloaded — their NAMES are in the AVAILABLE TOOLS
catalog below. Load a schema with ``tool_search`` before calling
(``tool_search('select:todo_get,todo_add_note')``), and load only what you
need. Never guess parameter names.

## TOOL CALL CONTRACT

Every ``tr_*`` call REQUIRES a non-empty ``reason``. Parse each tool result's
JSON envelope and branch on ``status``: "success" → continue; "denied" → pick
an alternative or report the denial; "error" → fix and retry, or report. Never
claim something was written or found unless the call returned success.

## ASKING THE USER

You may call ``tr_ask_user(question, options)`` — it reaches the user through
the daemon's channels even though you run in the background. Use it when a
decision materially changes the outcome and the task description doesn't
settle it; decide small things yourself so the job doesn't stall.

## RETURN FORMAT

Finish with ONE short plain-text summary for the master: what you sharpened or
produced, which notes/artifacts you added to the card (by id), and any open
question you left for the user. The summary is relayed to the user when the
master wakes — make it self-contained.
"""


class TinkerSubAgent(BaseSubAgent):
    """Board-scoped background tinkerer, registered as ``tinker-bg``."""

    name = "tinker"
    description = (
        "Background tinkering partner for the user's TODO board: sharpens a "
        "card's rough idea, decomposes it into small objectives, researches "
        "open questions, and persists notes/artifacts on the card. ALWAYS "
        "include the target card's id (tdo_…) in the task description — look "
        "it up first with todo_list if you only know the title."
    )

    @property
    def system_prompt(self) -> str:
        return _SYSTEM_PROMPT

    def extra_tools(self) -> list[BaseTool]:
        # Full board scope, notes authored as "tinker" — same authorship the
        # interactive tinker uses, so the UI badges both identically.
        from yuyutsava.todoboard.tools import make_todo_tools

        return make_todo_tools(scope="full", author="tinker")

    def search_tools(self) -> list[BaseTool]:
        """Tinkering means researching: expose the full ws_* set whenever a
        provider is configured (the GeneralPurposeAgent rule), merged with any
        skill-declared tools from the base implementation."""
        if self._search_config is None:
            return []
        from yuyutsava.tools.search import make_search_tools

        tools = make_search_tools(self._search_config, cap_enforcer=self._cap_enforcer)
        by_name = {t.name: t for t in tools}
        for t in super().search_tools():
            by_name.setdefault(t.name, t)
        return list(by_name.values())


def make_tinker_subagent(
    *,
    skill_registry: object | None = None,
    search_config: object | None = None,
    mcp_manager: object | None = None,
    cap_enforcer: object | None = None,
    memory_store: object | None = None,
    skill_store: object | None = None,
    policy: object | None = None,
    consent: object | None = None,
) -> TinkerSubAgent:
    """Build the bg tinkerer with its board-rooted TaskRunner.

    One construction path for the daemon bootstrap and the CLI stack so the
    zone layout can't drift: workspace = the board root (every card dir is in
    zone), sandbox OUTSIDE the board root — the UnifiedSweeper deletes any
    ``blobs/todoboard/<dir>`` without a card row, so a sandbox in there would
    be swept mid-task.
    """
    from yuyutsava.storage.paths import blobs_dir
    from yuyutsava.todoboard.exchange import board_workspace_root

    task_runner = TaskRunnerAgent(
        workspace_root=board_workspace_root(),
        sandbox_root=blobs_dir() / "tinker_sandbox",
        policy=policy,
        consent=consent,
    )
    return TinkerSubAgent(
        task_runner,
        skill_registry=skill_registry,
        can_write_skills=True,
        search_config=search_config,
        mcp_manager=mcp_manager,
        cap_enforcer=cap_enforcer,
        memory_store=memory_store,
        skill_store=skill_store,
    )


__all__ = ["TinkerSubAgent", "make_tinker_subagent"]
