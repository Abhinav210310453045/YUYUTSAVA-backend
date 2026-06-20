"""
Triage agent: one LLM call per event with a structured decision.

Not a deepagents subagent (no nested tool loop, no parent). It's a peer
loop the daemon runs that consumes from the bus and produces ``Proposal``s
into the orchestrator's task queue.

Uses ``llm_settings_from_env("triage")`` so the user can run a small/cheap
model (Ollama, Groq llama-3.1-8b) here while the orchestrator uses
something stronger.
"""

from __future__ import annotations

import json
import logging
from typing import Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from yuyutsava.agents.triage.prompts import TRIAGE_SYSTEM_PROMPT, render_event_message
from yuyutsava.core.tracing import get_callback
from yuyutsava.events.bus import EventEnvelope

logger = logging.getLogger("yuyutsava.agents.triage")


class TriageDecision(BaseModel):
    """Structured output schema for the triage agent."""

    action: Literal["drop", "log", "propose"] = Field(
        description="What to do with this event. Bias toward drop."
    )
    subagent_hint: str | None = Field(
        default=None,
        description="Required when action='propose'. Name of the subagent to run.",
    )
    proposed_instruction: str | None = Field(
        default=None,
        description="Required when action='propose'. One-line user-readable "
                    "instruction the user will approve or modify.",
    )
    reason: str = Field(
        description="One short sentence explaining the decision.",
    )
    urgency: int = Field(
        default=1, ge=0, le=3,
        description="0=trace, 1=info, 2=notable, 3=urgent.",
    )
    complexity: int = Field(
        default=3, ge=1, le=5,
        description="How complex the proposed work is for an agent, 1-5. "
                    "See the anchored examples in the system prompt.",
    )


class TriageAgent:
    """Single-call event classifier. Deliberately NOT a ``BaseSubAgent`` —
    it's a one-shot LLM call without tools or a delegation graph, invoked
    directly by the daemon's triage loop. If you're adding a new agent and
    the orchestrator should be able to spawn it, inherit ``BaseSubAgent``
    instead.
    """

    def __init__(self, model: BaseChatModel) -> None:
        # ``with_structured_output`` produces a runnable that returns a TriageDecision.
        self._runnable = model.with_structured_output(TriageDecision)

    async def classify(
        self,
        envelope: EventEnvelope,
        capabilities_block: str,
        skills_index: str = "",
    ) -> TriageDecision:
        msg = render_event_message(
            envelope_summary=envelope.summary,
            topic=envelope.topic,
            hints_json=json.dumps(envelope.hints),
            capabilities_block=capabilities_block,
            skills_index=skills_index,
        )
        _lf_cb = get_callback(run_name=f"triage:{envelope.topic}")
        _invoke_cfg = {"callbacks": [_lf_cb]} if _lf_cb else {}
        messages = [SystemMessage(content=TRIAGE_SYSTEM_PROMPT), HumanMessage(content=msg)]
        # One retry: a structured-output parse failure (e.g. a truncated/garbled
        # JSON response) is not retried by LangChain, so do it here. The raised
        # exception text already carries the offending raw output for diagnosis.
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                decision: TriageDecision = await self._runnable.ainvoke(
                    messages, config=_invoke_cfg,
                )  # type: ignore[assignment]
                break
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "triage LLM call failed (attempt %d/2): %s", attempt + 1, exc,
                )
        else:
            logger.warning("triage LLM call failed after retry; defaulting to drop")
            return TriageDecision(
                action="drop", reason=f"triage error: {last_exc}", urgency=0,
            )

        # Defensive: validate that 'propose' carries the required fields.
        if decision.action == "propose" and (
            not decision.subagent_hint or not decision.proposed_instruction
        ):
            return TriageDecision(
                action="drop",
                reason="propose missing subagent_hint or proposed_instruction",
                urgency=0,
            )
        return decision
