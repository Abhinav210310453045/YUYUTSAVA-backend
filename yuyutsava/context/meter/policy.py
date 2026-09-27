"""The policy that measures a call and publishes what it found.

One instance per graph, but the state it reads and writes is per *conversation*
and lives in :mod:`~yuyutsava.context.meter.calibration`: a master and every
subagent it delegates to get their own policy instance, and a subagent's tokens
are still money this conversation spent. Per-instance totals made each subagent
count from zero and then publish its own small total over the master's.

It runs three times per call — before the model sees the prompt, while revising
the call, and after the provider answers — which is why it is the only place
that can both describe what is about to be sent and correct that description
against what was actually charged.
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from typing import Any

from yuyutsava.context.digests import is_offloaded_digest
from yuyutsava.context.meter.bus import bus, counters
from yuyutsava.context.meter.calibration import (
    FACTOR_MAX,
    FACTOR_MIN,
    PREFIX_ONLY_SHARE,
    STATIC_KEYS,
    ThreadState,
    allocate,
    clamp_factor,
    model_factors,
    state_for,
    target_tokens,
)
from yuyutsava.context.meter.sizing import (
    catalog_chars,
    injected_block_chars,
    markers,
    split_system_chars,
    tool_schema_chars,
)
from yuyutsava.context.meter.snapshot import ContextSnapshot
from yuyutsava.context.tokens import approx_tokens, approx_tokens_chars
from yuyutsava.llm.pricing import estimate_cost_usd, is_priced, load_price_table
from yuyutsava.policy.base import Policy
from yuyutsava.policy.types import Directive, ModelCall, Turn

logger = logging.getLogger("yuyutsava.context.meter")


class ContextMeterPolicy(Policy):
    """Publish a context snapshot before and after every model call.

    Pure observability: returns ``None`` from every hook and records no edits
    on the model call, so :meth:`LangChainPolicyAdapter._apply` hands the
    request through untouched.

    Three hooks, because the three things a reader wants live in three places:

    * ``before_model`` — the message list, post-offload and post-compaction
      (this policy is wired last in ``_context_middleware`` for exactly that
      reason, beside ``PromptInspectorPolicy``);
    * ``revise_model_call`` — the tool names actually bound to this call, after
      ``ToolFilterPolicy`` has done its suppressing;
    * ``after_model`` — what the provider says it charged.
    """

    name = "ContextMeterPolicy"

    def __init__(
        self,
        *,
        settings: Any,
        role: str = "agent",
        model: Any | None = None,
        model_name: str = "",
        window: bool = True,
    ) -> None:
        super().__init__()
        self._settings = settings
        self._role = role
        self._model = model
        self._model_name = model_name
        #: Whether this agent's message list IS the conversation's window.
        #: False for subagents: they run inside the master's turn on their own
        #: message list, so publishing their segments would make the panel jump
        #: to a different conversation's shape mid-delegation. Their *spend*
        #: still accumulates — see ``thread_states``.
        self._window = window
        self._prices: dict[str, tuple[float, float]] | None = None
        #: Set by ``before_model``, consumed by ``revise_model_call``.
        self._pending: ContextSnapshot | None = None

    # -- hooks --------------------------------------------------------------

    async def before_model(self, turn: Turn) -> Directive | None:
        if not self._window:
            return None
        try:
            self._pending = self._measure(turn)
        except Exception:  # noqa: BLE001 — never fail a turn over a number
            logger.debug("context meter: measuring failed", exc_info=True)
            self._pending = None
        return None

    async def revise_model_call(self, call: ModelCall) -> None:
        snap = self._pending
        self._pending = None
        if snap is None:
            return
        try:
            state = self._state(snap.thread_id)

            tools_chars = catalog_chars(self._role) + tool_schema_chars(self._role, call.tool_names)
            # The system prompt is segmented HERE, not from turn.messages.
            # ``ModelRequest.messages`` is documented "excluding system
            # message" — the prompt travels as ``request.system_message``, so
            # looking for a SystemMessage in state found nothing and the panel
            # reported "system prompt ≈0" on every single call, hiding both the
            # prompt itself and the agent-memory block baked into it.
            system_chars = {"system": 0, "memory": 0, "skills": 0}
            for text in call.system_texts:
                if not text:
                    continue
                for key, chars in split_system_chars(text).items():
                    system_chars[key] = system_chars.get(key, 0) + chars
            # Blocks the retrieval injectors append at model-call time. They are
            # real prompt bytes but are added after every before_model hook has
            # run, so counting the message list alone under-reports the prompt.
            for prefix, chars in injected_block_chars(snap.thread_id).items():
                key = self._segment_for_prefix(prefix)
                system_chars[key] = system_chars.get(key, 0) + chars

            # Raw, uncalibrated estimates, per segment. The provider's answer
            # has to be divided by an estimate of the same prompt, so these are
            # what gets kept — and the whole prompt is only accounted for here,
            # tool schemas included.
            raw = {
                "system": approx_tokens_chars(system_chars["system"], model=self._model),
                "tools": approx_tokens_chars(tools_chars, model=self._model),
                "memory": approx_tokens_chars(system_chars["memory"], model=self._model),
                "skills": approx_tokens_chars(system_chars["skills"], model=self._model),
                # ``before_model`` leaves its raw message estimate here.
                "messages": snap.messages_tokens,
            }
            rows = allocate(state, raw, target_tokens(state, raw))
            state.pending_raw = raw
            bus().publish(
                replace(
                    snap,
                    system_tokens=rows["system"],
                    tools_tokens=rows["tools"],
                    memory_tokens=rows["memory"],
                    skills_tokens=rows["skills"],
                    messages_tokens=rows["messages"],
                    tool_count=len(call.tool_names),
                    calibrated=state.calibrated,
                    anchored=state.anchor_tokens > 0,
                    # Something has been appended since the last measurement, so
                    # the growth term is an estimate.
                    window_measured=False,
                )
            )
        except Exception:  # noqa: BLE001 — the meter must never break a model call
            logger.debug("context meter: pre-call publish failed", exc_info=True)

    async def after_model(self, turn: Turn) -> Directive | None:
        usage = turn.usage
        if usage is None or not usage.any_tokens:
            return None
        try:
            self._record_call(turn.thread_id, usage)
        except Exception:  # noqa: BLE001 — the meter must never break a model call
            logger.debug("context meter: post-call publish failed", exc_info=True)
        return None

    # -- internals ----------------------------------------------------------

    def _state(self, thread_id: str) -> ThreadState:
        return state_for(thread_id, self._model_name)

    def _price_table(self) -> dict[str, tuple[float, float]]:
        if self._prices is None:
            self._prices = load_price_table()
        return self._prices

    def _fit(self, state: ThreadState, usage: Any) -> None:
        """Learn the two corrections from a completed call, then re-anchor.

        A near-pure-prefix call measures the prefix directly. Once that factor
        is known, ``measured - prefix`` measures the messages, so the second
        factor comes from a subtraction rather than from a second guess. Later
        fits are averaged with the standing factor, because the residual
        carries the prefix factor's error too and one odd call should nudge the
        scale rather than redefine it.
        """
        measured = int(getattr(usage, "input_tokens", 0) or 0)
        raw = state.pending_raw
        raw_total = sum(raw.values())
        if measured <= 0 or raw_total <= 0:
            return
        if not FACTOR_MIN <= measured / raw_total <= FACTOR_MAX:
            # The estimate and the provider are not describing the same prompt.
            # The spend is real and has already been recorded, but anchoring the
            # window on this number — or fitting a factor from it — would put
            # the panel on a scale nothing else shares.
            logger.debug(
                "context meter: ignoring an implausible reading (%d reported "
                "against a %d-token estimate)",
                measured,
                raw_total,
            )
            return
        raw_prefix = sum(raw.get(key, 0) for key in STATIC_KEYS)
        raw_messages = raw.get("messages", 0)
        if raw_messages / raw_total <= PREFIX_ONLY_SHARE and raw_prefix > 0:
            state.static_factor = clamp_factor(measured / raw_total)
            state.static_fitted = True
            state.calibrated = True
        elif state.static_fitted and raw_messages > 0:
            residual = measured - state.static_factor * raw_prefix
            if residual > 0:
                ratio = clamp_factor(residual / raw_messages)
                state.messages_factor = (
                    (state.messages_factor + ratio) / 2 if state.messages_fitted else ratio
                )
                state.messages_fitted = True
                state.calibrated = True
        elif not state.static_fitted:
            # A resumed thread never sees a prefix-only call, so there is
            # nothing to separate the halves with. One factor for both is what
            # the anchor exists to make survivable.
            both = clamp_factor(measured / raw_total)
            state.static_factor = both
            state.messages_factor = both
            state.calibrated = True
        state.anchor_tokens = measured
        state.anchor_raw = raw_total
        if state.calibrated and self._model_name:
            model_factors[self._model_name] = (
                state.static_factor,
                state.messages_factor,
            )

    def _measure(self, turn: Turn) -> ContextSnapshot:
        """Segment the conversation.

        Only the message side. The system prompt and the tool schemas are added
        by ``revise_model_call``, which is the hook that can actually see them
        — the framework hands the prompt over as ``request.system_message``,
        not as a message in state. A ``SystemMessage`` that does appear in
        state is skipped rather than counted, so the same bytes are not
        attributed twice.
        """
        state = self._state(turn.thread_id)

        convo: list[Any] = []
        offloaded = 0
        for msg in turn.messages:
            content = getattr(msg, "content", "")
            if getattr(msg, "type", "") == "system":
                continue
            convo.append(msg)
            if is_offloaded_digest(content):
                offloaded += 1

        return ContextSnapshot(
            role=self._role,
            thread_id=turn.thread_id,
            model=self._model_name,
            max_input_tokens=int(getattr(self._settings, "max_input_tokens", 0) or 0),
            compact_trigger_tokens=int(getattr(self._settings, "compact_trigger_tokens", 0) or 0),
            # Raw and uncorrected: ``revise_model_call`` is the hook that can
            # see the whole prompt, so it does the correcting and the
            # reconciling once, in one place.
            messages_tokens=approx_tokens(convo, model=self._model),
            message_count=len(turn.messages),
            offloaded_digests=offloaded,
            calibrated=state.calibrated,
            anchored=state.anchor_tokens > 0,
            **self._carry(turn.thread_id),
        )

    @staticmethod
    def _segment_for_prefix(prefix: str) -> str:
        for segment, marker in markers():
            if marker and prefix.startswith(marker[:40]):
                return segment
        return "memory"

    def _carry(self, thread_id: str) -> dict[str, Any]:
        """Last-call and session fields, unchanged by a new measurement."""
        state = self._state(thread_id)
        compactions, offloads = counters(thread_id)
        last = bus().latest(thread_id)
        return {
            "call_no": state.calls,
            "input_tokens": last.input_tokens if last else 0,
            "output_tokens": last.output_tokens if last else 0,
            "cache_read_tokens": last.cache_read_tokens if last else 0,
            "cache_creation_tokens": last.cache_creation_tokens if last else 0,
            "est_cost_usd": last.est_cost_usd if last else 0.0,
            "priced": last.priced if last else False,
            "calls": state.calls,
            "session_input_tokens": state.input_tokens,
            "session_output_tokens": state.output_tokens,
            "session_cache_read_tokens": state.cache_read_tokens,
            "session_cost_usd": state.cost_usd,
            "compactions": compactions,
            "offloads": offloads,
            "started_at": state.started_at,
        }

    def _record_call(self, thread_id: str, usage: Any) -> None:
        state = self._state(thread_id)
        model = self._model_name or getattr(usage, "model", "") or ""
        prices = self._price_table()
        cost = estimate_cost_usd(model, usage.input_tokens, usage.output_tokens, prices)

        state.calls += 1
        state.input_tokens += usage.input_tokens
        state.output_tokens += usage.output_tokens
        state.cache_read_tokens += getattr(usage, "cache_read_tokens", 0)
        state.cost_usd += cost

        # Fit the corrections and re-anchor: the provider just told us what
        # our own estimate of this exact prompt was worth.
        #
        # Only the window owner may. The pending estimate describes the
        # master's prompt and the thread state is shared, so a subagent
        # reaching it would divide the master's estimate by its own, much
        # smaller, token count and permanently skew the panel's scale.
        if self._window:
            self._fit(state, usage)

        # Keep the window rows from the last measurement: this call changed what
        # was spent, not what is in the prompt. For a subagent (window=False)
        # that also means the master's role and model stay on the snapshot —
        # relabelling the panel mid-delegation would describe a conversation
        # the segment rows are not about.
        previous = bus().latest(thread_id)
        base = previous or ContextSnapshot(role=self._role, thread_id=thread_id, model=model)
        compactions, offloads = counters(thread_id)
        # The prompt has just been measured and the reply it produced is now
        # part of the window — and that is a provider number too. So for this
        # one publish the headline is the provider's own arithmetic, with
        # nothing estimated, and the rows are reconciled onto it.
        window: dict[str, Any] = {}
        if self._window and state.anchor_tokens > 0 and state.pending_raw:
            rows = allocate(
                state,
                state.pending_raw,
                usage.input_tokens + usage.output_tokens,
            )
            window = {
                "system_tokens": rows["system"],
                "tools_tokens": rows["tools"],
                "memory_tokens": rows["memory"],
                "skills_tokens": rows["skills"],
                "messages_tokens": rows["messages"],
                "anchored": True,
                "window_measured": True,
            }
        bus().publish(
            replace(
                base,
                role=self._role if self._window else base.role,
                thread_id=thread_id,
                model=model if self._window else (base.model or model),
                calibrated=state.calibrated,
                call_no=state.calls,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=getattr(usage, "cache_read_tokens", 0),
                cache_creation_tokens=getattr(usage, "cache_creation_tokens", 0),
                est_cost_usd=cost,
                priced=is_priced(model, prices),
                calls=state.calls,
                session_input_tokens=state.input_tokens,
                session_output_tokens=state.output_tokens,
                session_cache_read_tokens=state.cache_read_tokens,
                session_cost_usd=state.cost_usd,
                compactions=compactions,
                offloads=offloads,
                started_at=state.started_at,
                ts=time.time(),
                **window,
            )
        )


__all__ = ["ContextMeterPolicy"]
