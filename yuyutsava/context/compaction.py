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

Respond ONLY with the extracted context in the format above. No preamble,
no closing remarks.

<messages>
Messages to summarize:
{messages}
</messages>"""


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
    ) -> None:
        super().__init__(
            model,
            trigger=("tokens", settings.compact_trigger_tokens),
            keep=("messages", settings.keep_messages),
            summary_prompt=YUYUTSAVA_SUMMARY_PROMPT,
            trim_tokens_to_summarize=settings.summarizer_input_tokens,
        )
        self._settings = settings
        self._summary_store = summary_store
        self._memory = memory_sink
        self._role = role

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
        new_messages = self._build_new_messages(summary)

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
                *self._build_new_messages(summary),
                *preserved,
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
                    content=(
                        "Recovered context from a previous session of this "
                        f"thread (summary v{row.version}):\n\n{row.summary}"
                    ),
                )
            ]
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

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
