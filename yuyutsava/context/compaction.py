"""Compaction middleware: summarize old turns when context nears the budget.

Extends ``langchain.agents.middleware.SummarizationMiddleware`` (which owns
the hard parts: safe cut-points that never split an AIMessage from its
ToolMessages, token counting with reported-usage scaling, state rewrite via
``RemoveMessage(REMOVE_ALL_MESSAGES)`` so the *checkpoint itself* compacts).

What this subclass adds:

- **Pinning** — the leading Human/System messages (the original task) are
  excluded from summarization and re-emitted at the head of the rewritten
  state, so the model never loses the session intent verbatim.
- **Persistence** — every produced summary is appended to
  :class:`~yuyutsava.context.summary_store.ThreadSummaryStore` and, when
  semantic memory is enabled, embedded as a ``kind="summary"`` memory.
- **Resume injection** — ``abefore_agent`` re-injects the latest persisted
  summary when a thread resumes with an empty history (checkpoints swept or
  daemon crashed between sweep and resume).
- **Structured summary prompt** — five fixed sections so a third compaction
  cycle still carries intent, decisions, and the next step.

Absolute-token trigger (``("tokens", N)`` with N from
:class:`ContextSettings`) is used instead of ``("fraction", …)`` because the
fraction form requires model-profile data that Groq/Ollama/OpenRouter
models don't reliably ship.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain.agents.middleware import SummarizationMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AnyMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
)
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from yuyutsava.context.config import ContextSettings
from yuyutsava.context.summary_store import ThreadSummaryStore

logger = logging.getLogger("yuyutsava.context.compaction")

# Threads whose next model call should compact regardless of the token
# threshold. Set by the ``ctx_compact`` tool (context.tools) and consumed —
# check-and-clear — by ``abefore_model`` below. Process-local by design: the
# tool call and the following model call run in the same event loop, and a
# stale flag after a crash merely no-ops on the next compactable turn.
_FORCE_COMPACT: set[str] = set()


def request_compaction(thread_id: str) -> None:
    """Mark *thread_id* for forced compaction on its next model call."""
    if thread_id:
        _FORCE_COMPACT.add(thread_id)

YUYUTSAVA_SUMMARY_PROMPT = """<role>
Context Extraction Assistant
</role>

<primary_objective>
The conversation history below is about to be REPLACED by the context you
extract here. Future turns will see only your extraction plus the most
recent messages — so anything you omit is gone. Extract the highest-value
context for continuing the task.
</primary_objective>

<instructions>
Structure your extraction using EXACTLY the following sections. Every
section is mandatory — write "None" if a section has nothing to report.

## SESSION INTENT
The user's original goal/request, restated faithfully. What is the overall
task this session is trying to accomplish?

## DECISIONS MADE
Key choices, conclusions, and strategies settled so far — with the
reasoning. Include rejected options and why they were rejected, so they are
not re-litigated.

## WORK COMPLETED
What has already been done. Be specific enough that no completed action is
ever repeated.

## ARTIFACTS
Files created/modified/read (full paths), and every offloaded artifact id
(art_…) still relevant — these ids are retrievable later via
ctx_fetch_artifact / ctx_grep_artifact, so losing an id loses the data.

## CURRENT STATE / NEXT STEP
Where the work stands right now and the single next concrete action.

## OPEN QUESTIONS
Unresolved questions, blockers, or things awaiting the user.
</instructions>

{readback_note}
Respond ONLY with the extracted context in the format above. No preamble,
no closing remarks.

<messages>
Messages to summarize:
{messages}
</messages>"""

# Spliced into the prompt above (by replace, not format — ``{messages}`` is
# langchain's placeholder and must survive untouched) only when the agent
# actually has the ctx_history* tools.
_READBACK_NOTE = """
Note: these messages are not being deleted — they stay readable verbatim via
ctx_history / ctx_history_grep / ctx_history_message(seq). So prefer naming
where a detail lives (an artifact id, a file path, a tool call) over copying
it out in full, and never invent a value you are unsure of: it can be looked
up.
"""

# Appended to the summary that replaces the evicted turns, so the surviving
# state itself says where the rest went. Without this the model has a summary
# and no reason to believe anything more is available.
_READBACK_FOOTER = (
    "\n\nThe summarized turns are still recorded verbatim. Recover any detail "
    "with ctx_history_grep(pattern) to find it, ctx_history(after_seq=N) to "
    "list turns, or ctx_history_message(seq) to read one in full."
)

# Tool-call arguments worth stubbing once the turn they belong to is old: the
# content was already written somewhere durable, so carrying a second copy in
# every later request buys nothing. Value = the argument holding the bulk, and
# the argument naming where it landed (``None`` = recoverable only through the
# verbatim history).
_STUBBABLE_ARGS: dict[str, tuple[str, str | None]] = {
    "tr_write_file": ("content", "path"),
    "sk_write_skill": ("body", "name"),
    "artifact_create": ("content", None),
}

# Below this an argument is not worth stubbing — the stub itself costs tokens.
_STUB_MIN_CHARS = 1_000

# Recent messages never stubbed: the agent may still be working on what it
# just wrote. Applied only at compaction time, so the boundary is fixed once
# rather than sliding every turn (a sliding boundary would rewrite the cached
# prompt prefix on every call and cost far more than the stub saves).
_STUB_KEEP_RECENT = 6


class YuyutsavaCompactionMiddleware(SummarizationMiddleware):
    """SummarizationMiddleware + pinning, persistence, and resume injection."""

    def __init__(
        self,
        *,
        model: BaseChatModel,
        settings: ContextSettings,
        summary_store: ThreadSummaryStore | None = None,
        memory_sink: Any | None = None,  # duck-typed MemoryStore (async .add)
        role: str = "agent",
        history_readback: bool = False,
    ) -> None:
        super().__init__(
            model,
            trigger=("tokens", settings.compact_trigger_tokens),
            keep=("messages", settings.keep_messages),
            summary_prompt=YUYUTSAVA_SUMMARY_PROMPT.replace(
                "{readback_note}", _READBACK_NOTE if history_readback else ""
            ),
            trim_tokens_to_summarize=settings.summarizer_input_tokens,
        )
        self._settings = settings
        self._summary_store = summary_store
        self._memory = memory_sink
        self._role = role
        # True when a transcript store is wired, i.e. the ctx_history* tools
        # exist for this agent. Everything that promises the model a read-back
        # path is conditional on it — a promise we cannot keep is worse than
        # no promise, because the model stops preserving detail in summaries.
        self._history_readback = history_readback

    # ------------------------------------------------------------------
    # Compaction (async path — the whole runtime streams via astream)
    # ------------------------------------------------------------------

    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        messages: list[AnyMessage] = state["messages"]
        self._ensure_message_ids(messages)

        # Agent-requested compaction (ctx_compact): consume the flag whether
        # or not anything ends up summarizable — a stuck flag must never make
        # every later turn re-attempt a forced pass.
        thread_id = _current_thread_id()
        forced = thread_id in _FORCE_COMPACT
        if forced:
            _FORCE_COMPACT.discard(thread_id)

        total_tokens = self.token_counter(messages)
        if not forced and not self._should_summarize(messages, total_tokens):
            return None

        pinned = self._pinned_head(messages)
        rest = messages[len(pinned):]

        keep_n = int(self.keep[1])
        cutoff = self._find_safe_cutoff(rest, keep_n)
        if cutoff <= 0:
            return None
        to_summarize, preserved = rest[:cutoff], rest[cutoff:]
        if not to_summarize:
            return None

        # Pinned messages are included in the summarizer's *input* (so the
        # summary is anchored to the real task) but stay verbatim in state.
        summary = await self._acreate_summary([*pinned, *to_summarize])
        new_messages = self._build_new_messages(self._with_readback(summary))
        # Compaction is the one moment the prompt prefix is rewritten anyway,
        # so it is also the only cheap moment to drop bulky already-persisted
        # tool arguments from the surviving tail.
        preserved = self._stub_bulky_args(preserved)

        logger.info(
            "%s: compacted %d msgs (~%d tokens) → summary + %d pinned + %d kept",
            self._role, len(to_summarize), total_tokens, len(pinned), len(preserved),
        )
        await self._persist_summary(summary)

        return {
            "messages": [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                *pinned,
                *new_messages,
                *preserved,
            ]
        }

    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        # Sync invocation path: same pinning/cutoff logic, but summary
        # persistence is skipped (the stores are async-only). The runtime
        # streams everything via astream, so this path is test/edge-only.
        messages: list[AnyMessage] = state["messages"]
        self._ensure_message_ids(messages)
        total_tokens = self.token_counter(messages)
        if not self._should_summarize(messages, total_tokens):
            return None
        pinned = self._pinned_head(messages)
        rest = messages[len(pinned):]
        cutoff = self._find_safe_cutoff(rest, int(self.keep[1]))
        if cutoff <= 0:
            return None
        to_summarize, preserved = rest[:cutoff], rest[cutoff:]
        if not to_summarize:
            return None
        summary = self._create_summary([*pinned, *to_summarize])
        logger.debug("%s: sync compaction path — summary not persisted", self._role)
        return {
            "messages": [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                *pinned,
                *self._build_new_messages(self._with_readback(summary)),
                *self._stub_bulky_args(preserved),
            ]
        }

    # ------------------------------------------------------------------
    # Resume injection
    # ------------------------------------------------------------------

    async def abefore_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """Re-inject the latest persisted summary on an empty-history resume.

        Fires when a thread restarts with at most the fresh task message in
        state (checkpoints swept / crash) but a summary survives in the
        store. Fresh threads have no stored summary, so this is a no-op for
        them.
        """
        if self._summary_store is None:
            return None
        messages = state.get("messages", []) if isinstance(state, dict) else []
        if len(messages) > 1:
            return None
        thread_id = _current_thread_id()
        if not thread_id:
            return None
        try:
            row = await self._summary_store.latest(thread_id)
        except Exception:
            logger.exception("compaction: summary lookup failed on resume")
            return None
        if row is None:
            return None
        logger.info(
            "%s: resumed thread %s with persisted summary v%d",
            self._role, thread_id, row.version,
        )
        return {
            "messages": [
                SystemMessage(
                    content=self._with_readback(
                        "Recovered context from a previous session of this "
                        f"thread (summary v{row.version}):\n\n{row.summary}"
                    ),
                )
            ]
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _with_readback(self, summary: str) -> str:
        """Summary text plus the read-back footer, when there is one to offer."""
        return f"{summary}{_READBACK_FOOTER}" if self._history_readback else summary

    def _stub_bulky_args(self, messages: list[AnyMessage]) -> list[AnyMessage]:
        """Replace already-persisted bulk tool arguments with a pointer.

        A ``tr_write_file(content=…)`` or ``artifact_create(content=…)`` call
        carries the whole payload, and that payload is then re-sent with every
        later request for the rest of the session — this session re-sent 7,564
        chars of artifact HTML on all 97 calls. The file and the artifact are
        on disk, so the copy in the prompt is pure duplication.

        This is **not** truncation: the transcript store dedups on message id,
        so the row it already holds keeps the original arguments in full, and
        the stub names how to read them back. Nothing becomes unreachable.

        The provider payload is built straight from ``tool_calls`` (Vertex:
        ``FunctionCall({"name": tc["name"], "args": tc["args"]})``), so only
        ``args`` values change — ``id`` and ``name`` are preserved, which is
        also what keeps Gemini thought-signature lookups matching.
        """
        if len(messages) <= _STUB_KEEP_RECENT:
            return messages
        cutoff = len(messages) - _STUB_KEEP_RECENT
        out: list[AnyMessage] = []
        stubbed = 0
        saved = 0
        for i, msg in enumerate(messages):
            calls = getattr(msg, "tool_calls", None)
            if i >= cutoff or not calls:
                out.append(msg)
                continue
            new_calls = []
            changed = False
            for call in calls:
                rewritten, freed = self._stub_one_call(call)
                if freed:
                    changed = True
                    stubbed += 1
                    saved += freed
                new_calls.append(rewritten)
            if not changed:
                out.append(msg)
                continue
            try:
                out.append(msg.model_copy(update={"tool_calls": new_calls}))
            except Exception:  # noqa: BLE001 — never drop a message over this
                logger.debug("compaction: could not stub args on %r", msg, exc_info=True)
                out.append(msg)
        if stubbed:
            logger.info(
                "%s: stubbed %d bulky tool arg(s) in the kept tail (~%d chars freed "
                "per later call)", self._role, stubbed, saved,
            )
        return out

    def _stub_one_call(self, call: Any) -> tuple[Any, int]:
        """``(call, chars_freed)`` — ``chars_freed == 0`` means untouched."""
        if not isinstance(call, dict):
            return call, 0
        spec = _STUBBABLE_ARGS.get(str(call.get("name") or ""))
        if spec is None:
            return call, 0
        arg_name, locator_arg = spec
        args = call.get("args")
        if not isinstance(args, dict):
            return call, 0
        value = args.get(arg_name)
        if not isinstance(value, str) or len(value) < _STUB_MIN_CHARS:
            return call, 0

        locator = str(args.get(locator_arg) or "") if locator_arg else ""
        if locator_arg and not locator:
            # No path/name to point at: leave the argument alone rather than
            # replace it with a stub nobody can resolve.
            return call, 0
        if locator_arg == "path":
            how = f'read it back with tr_read_file("{locator}")'
        elif locator_arg == "name":
            how = f'read it back with sk_read_skill("{locator}")'
        elif self._history_readback:
            how = "find it with ctx_history_grep, then ctx_history_message(seq)"
        else:
            # Nothing to point at and no history readers wired.
            return call, 0

        new_args = dict(args)
        new_args[arg_name] = (
            f"<stubbed: {len(value):,} chars, already written — {how}>"
        )
        freed = len(value) - len(new_args[arg_name])
        return {**call, "args": new_args}, max(0, freed)

    def _pinned_head(self, messages: list[AnyMessage]) -> list[AnyMessage]:
        """Leading Human/System messages (the task) — never summarized away.

        Stops at the first AI/Tool message so pinning can never split an
        AIMessage from its ToolMessages.
        """
        pinned: list[AnyMessage] = []
        for m in messages[: self._settings.pin_first_messages]:
            if isinstance(m, (HumanMessage, SystemMessage)):
                pinned.append(m)
            else:
                break
        return pinned

    async def _persist_summary(self, summary: str) -> None:
        """Write the summary to the store + memory. Never raises."""
        thread_id = _current_thread_id()
        if not thread_id:
            return
        token_count = 0
        try:
            token_count = int(self.token_counter([HumanMessage(content=summary)]))
        except Exception:
            pass
        if self._summary_store is not None:
            try:
                version = await self._summary_store.put(
                    thread_id, summary, token_count=token_count
                )
                logger.debug("compaction: stored summary v%d for %s", version, thread_id)
            except Exception:
                logger.exception("compaction: failed to persist summary")
        if self._memory is not None:
            try:
                await self._memory.add(
                    kind="summary", text=summary, source_thread_id=thread_id
                )
            except Exception:
                logger.exception("compaction: failed to embed summary into memory")


def _current_thread_id() -> str:
    """Thread id from the active LangGraph run config, or empty string."""
    try:
        from langgraph.config import get_config

        cfg = get_config() or {}
        return str(cfg.get("configurable", {}).get("thread_id", "") or "")
    except Exception:
        return ""
