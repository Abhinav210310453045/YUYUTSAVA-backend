"""Context meter — what is in the window right now, and what the last call cost.

The 12 Sep post-mortem could only answer "12,285 → 72,848 input tokens, ~93 %
cache-read, 0 compactions" by querying Postgres after the session was over. From
inside a session none of it was visible, so a context window filling up looked
exactly like a context window that was fine.

This is the one measurement source both fronts read. It publishes a
:class:`ContextSnapshot` twice per model call — before (what we are about to
send, by segment) and after (what the provider says it actually charged) — onto
a process-global :class:`MeterBus`. The CLI dashboard subscribes and repaints;
the daemon's ``/ws/converse`` handler subscribes and forwards a frame to the
app. Deliberately **not** a ``StreamEvent`` kind: that would mean teaching
``core/streaming``, two CLI renderers, the SSE payload union and the static web
UI about a frame none of them can act on.

## Estimated versus measured

Segment sizes are estimates — there is no way to attribute a provider's token
count to regions of a prompt, and asking a provider to count costs money and a
round trip per turn. But an uncalibrated character estimate drifts from the real
number by a lot on some models, so after every completed call the meter divides
the provider's reported ``input_tokens`` by its own estimate for that same call
and keeps the ratio (:attr:`ContextSnapshot.calibrated`). Subsequent estimates
are scaled by it. The headline occupancy is therefore a *calibrated estimate of
the current window*, and last-call figures are always raw provider numbers.
Renderers must keep that distinction visible; ``/context`` marks estimates with
``≈``.
"""

from __future__ import annotations

import logging
import time
import weakref
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from yuyutsava.context.tokens import approx_tokens, approx_tokens_chars
from yuyutsava.policy.base import Policy
from yuyutsava.policy.types import Directive, ModelCall, Turn

logger = logging.getLogger("yuyutsava.context.meter")

#: How many threads' worth of state to retain. A daemon serves conversations
#: indefinitely; unbounded per-thread dicts are a slow leak.
_MAX_THREADS = 64

#: Guard rails on the estimate→reported correction. A ratio outside this range
#: means the two are measuring different things (a mid-call compaction, a
#: provider reporting cumulative tokens), and trusting it would make the panel
#: swing wildly. Clamping keeps a wrong calibration merely imprecise.
_CALIBRATION_MIN = 0.5
_CALIBRATION_MAX = 2.0

#: Order the segments are reported in — largest structural pieces first, so a
#: reader scans "what is this window made of" top to bottom.
SEGMENT_LABELS: tuple[tuple[str, str], ...] = (
    ("system", "system prompt"),
    ("tools", "tool schemas"),
    ("memory", "memory"),
    ("skills", "skills"),
    ("messages", "messages"),
)


class _Bounded(OrderedDict):
    """Insertion-ordered dict that evicts its oldest key past ``_MAX_THREADS``."""

    def __setitem__(self, key: Any, value: Any) -> None:
        super().__setitem__(key, value)
        self.move_to_end(key)
        while len(self) > _MAX_THREADS:
            self.popitem(last=False)


# ----------------------------------------------------------------------
# The snapshot
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class ContextSnapshot:
    """One reading of a conversation's context window and spend."""

    role: str = "agent"
    thread_id: str = ""
    model: str = ""

    # The window the compactor steers under.
    max_input_tokens: int = 0
    compact_trigger_tokens: int = 0

    # Calibrated estimates of what occupies it.
    system_tokens: int = 0
    tools_tokens: int = 0
    memory_tokens: int = 0
    skills_tokens: int = 0
    messages_tokens: int = 0

    # Shape of the message list.
    message_count: int = 0
    tool_count: int = 0
    offloaded_digests: int = 0

    #: Whether the segment estimates have been corrected against a real call.
    calibrated: bool = False

    # The last completed call — provider-reported, never estimated.
    call_no: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    est_cost_usd: float = 0.0
    #: False when the model has no entry in the price table, so every surface
    #: can say "unpriced" instead of showing a confident $0.00.
    priced: bool = False

    # This conversation so far.
    calls: int = 0
    session_input_tokens: int = 0
    session_output_tokens: int = 0
    session_cache_read_tokens: int = 0
    session_cost_usd: float = 0.0
    compactions: int = 0
    offloads: int = 0
    started_at: float = 0.0

    ts: float = field(default_factory=time.time)

    # -- derived ------------------------------------------------------------

    @property
    def used_tokens(self) -> int:
        return (
            self.system_tokens + self.tools_tokens + self.memory_tokens
            + self.skills_tokens + self.messages_tokens
        )

    @property
    def free_tokens(self) -> int:
        return max(0, self.max_input_tokens - self.used_tokens)

    @property
    def used_fraction(self) -> float:
        if self.max_input_tokens <= 0:
            return 0.0
        return min(1.0, self.used_tokens / self.max_input_tokens)

    @property
    def cache_hit_fraction(self) -> float:
        """Share of the last call's input the provider served from cache."""
        if self.input_tokens <= 0:
            return 0.0
        return min(1.0, self.cache_read_tokens / self.input_tokens)

    def segments(self) -> list[tuple[str, str, int]]:
        """``(key, label, tokens)`` in report order, zero rows included.

        Zeros are kept on purpose: "skills 0" is the finding that 31 indexed
        skills went unused, and a row that vanishes cannot say that.
        """
        return [
            (key, label, int(getattr(self, f"{key}_tokens", 0)))
            for key, label in SEGMENT_LABELS
        ]

    def as_dict(self) -> dict[str, Any]:
        """Wire shape for the app. Flat where the UI wants numbers, nested
        where it wants a group, and segments pre-ordered so both fronts show
        the same rows in the same order."""
        return {
            "role": self.role,
            "thread_id": self.thread_id,
            "model": self.model,
            "max_input_tokens": self.max_input_tokens,
            "compact_trigger_tokens": self.compact_trigger_tokens,
            "used_tokens": self.used_tokens,
            "free_tokens": self.free_tokens,
            "used_fraction": round(self.used_fraction, 6),
            "calibrated": self.calibrated,
            "segments": [
                {"key": k, "label": lbl, "tokens": n} for k, lbl, n in self.segments()
            ],
            "message_count": self.message_count,
            "tool_count": self.tool_count,
            "offloaded_digests": self.offloaded_digests,
            "call": {
                "n": self.call_no,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "cache_read_tokens": self.cache_read_tokens,
                "cache_creation_tokens": self.cache_creation_tokens,
                "cache_hit_fraction": round(self.cache_hit_fraction, 6),
                "est_cost_usd": self.est_cost_usd,
                "priced": self.priced,
            },
            "session": {
                "calls": self.calls,
                "input_tokens": self.session_input_tokens,
                "output_tokens": self.session_output_tokens,
                "cache_read_tokens": self.session_cache_read_tokens,
                "est_cost_usd": self.session_cost_usd,
                "compactions": self.compactions,
                "offloads": self.offloads,
                "started_at": self.started_at,
            },
            "ts": self.ts,
        }


# ----------------------------------------------------------------------
# The bus
# ----------------------------------------------------------------------

Subscriber = Callable[[ContextSnapshot], None]


class MeterBus:
    """Fan-out of snapshots to whoever is displaying them.

    Same shape as ``llm.quirks.first_chunk_retry.set_retry_listener``: process
    global, synchronous, and defensive. Callbacks run inside the agent's model
    step, so they must be cheap and non-blocking — repaint requests and queue
    puts, not I/O. A raising subscriber is logged and dropped from that publish,
    never propagated: a broken meter must not fail a turn.
    """

    def __init__(self) -> None:
        self._subs: dict[int, tuple[str, Subscriber]] = {}
        self._latest: _Bounded = _Bounded()
        self._next = 1

    def subscribe(self, thread_id: str, cb: Subscriber) -> int:
        """Listen to one thread, or to every thread when *thread_id* is ``""``."""
        token = self._next
        self._next += 1
        self._subs[token] = (thread_id, cb)
        return token

    def unsubscribe(self, token: int) -> None:
        self._subs.pop(token, None)

    def publish(self, snap: ContextSnapshot) -> None:
        if snap.thread_id:
            self._latest[snap.thread_id] = snap
        for thread_id, cb in list(self._subs.values()):
            if thread_id and thread_id != snap.thread_id:
                continue
            try:
                cb(snap)
            except Exception:  # noqa: BLE001 — see the class docstring
                logger.debug("context meter: subscriber raised", exc_info=True)

    def latest(self, thread_id: str = "") -> ContextSnapshot | None:
        """Most recent snapshot for a thread, for a client that just connected."""
        if thread_id:
            return self._latest.get(thread_id)
        return next(reversed(self._latest.values()), None) if self._latest else None

    def clear(self) -> None:
        """Drop all state. Tests only."""
        self._subs.clear()
        self._latest.clear()


_BUS = MeterBus()


def bus() -> MeterBus:
    """The process-wide meter bus."""
    return _BUS


# ----------------------------------------------------------------------
# Counters the context controllers report into
# ----------------------------------------------------------------------

_compactions: _Bounded = _Bounded()
_offloads: _Bounded = _Bounded()


def note_compaction(thread_id: str) -> None:
    """Record that history was summarized away on this thread.

    Compaction is the only step that drops messages, so "how many times has
    this happened" is the single most useful number for judging whether a
    session's window is under pressure — and it was unobservable.
    """
    if thread_id:
        _compactions[thread_id] = _compactions.get(thread_id, 0) + 1


def note_offload(thread_id: str) -> None:
    """Record that a tool result was moved out of context into an artifact."""
    if thread_id:
        _offloads[thread_id] = _offloads.get(thread_id, 0) + 1


def counters(thread_id: str) -> tuple[int, int]:
    """``(compactions, offloads)`` seen for this thread in this process."""
    return _compactions.get(thread_id, 0), _offloads.get(thread_id, 0)


# ----------------------------------------------------------------------
# Tool-schema sizing
# ----------------------------------------------------------------------

#: Weak refs to the live tool registries, by role. Weak so a bundle that is
#: closed can be collected — the alternative is a registry (and every tool it
#: holds) pinned for the life of the process by a telemetry dict.
_registries: dict[str, weakref.ReferenceType] = {}

#: name -> schema chars, resolved on first sight and reused. Tool names are
#: globally unique by the prefix convention, so one cache serves every role.
_schema_chars: dict[str, int] = {}
_catalog_chars: dict[str, int] = {}


def note_tool_registry(role: str, registry: Any) -> None:
    """Keep a weak handle on a role's tool registry, for late-seen tool names.

    Weak on purpose: holding it strongly would pin every registry — and every
    tool in it — for the life of the process. It survives the builder's local
    because the ``tool_search`` tool bound to the graph captures it
    (``ToolRegistry.to_catalog`` closes over ``self`` in each entry's
    ``load_detail``). If that ever stops being true the fallback resolves to
    nothing and unmeasured tools contribute 0 rather than raising.

    Prefer :func:`note_bound_tools`, which measures up front and makes this a
    fallback rather than the primary path.
    """
    try:
        _registries[role] = weakref.ref(registry)
    except TypeError:  # not weak-referenceable (a test double)
        pass


def note_bound_tools(role: str, registry: Any, tools: Any) -> None:
    """Measure, once at build time, the schemas of the tools bound to a graph.

    Resolving sizes lazily from the registry misses the most important tool of
    all: ``tool_search`` is *created* by the registry and prepended to the
    bound list, never registered in it, so a registry lookup priced our single
    always-visible tool at zero — and under lazy discovery that is most of what
    the model is actually sent. Measuring the bound list directly is both exact
    and cheaper, since it happens once per bundle instead of per lookup.
    """
    note_tool_registry(role, registry)
    for tool in tools or ():
        name = getattr(tool, "name", "")
        if not name or name in _schema_chars:
            continue
        try:
            _schema_chars[name] = len(registry.schema_block([tool]))
        except Exception:  # noqa: BLE001 — sizing is best-effort
            _schema_chars[name] = 0
    if role not in _catalog_chars:
        try:
            _catalog_chars[role] = len(registry.catalog_block() or "")
        except Exception:  # noqa: BLE001
            _catalog_chars[role] = 0


def _registry_for(role: str) -> Any | None:
    """This role's registry, else any live one.

    The builders name their registry with an *agent* name ("cli") while a
    policy carries a *role* ("cli", "orchestrator", "tinker"), and the two do
    not always coincide. Tool names are globally unique by the prefix
    convention, so any registry that holds the tool answers correctly — and
    falling back beats a "tool schemas 0" row that looks like a finding.
    """
    ref = _registries.get(role)
    registry = ref() if ref is not None else None
    if registry is not None:
        return registry
    for ref in list(_registries.values()):
        registry = ref()
        if registry is not None:
            return registry
    return None


def tool_schema_chars(role: str, names: tuple[str, ...]) -> int:
    """Chars of JSON schema for the tools actually bound to a call.

    Only the bound names are measured — with lazy discovery that is a handful
    out of dozens, and that gap is exactly what the "tool schemas" row exists
    to show.
    """
    registry = _registry_for(role)
    total = 0
    for name in names:
        cached = _schema_chars.get(name)
        if cached is None:
            cached = 0
            if registry is not None:
                try:
                    tool = next(
                        (t for t in registry.all_tools() if t.name == name), None
                    )
                    if tool is not None:
                        cached = len(registry.schema_block([tool]))
                except Exception:  # noqa: BLE001 — sizing is best-effort
                    cached = 0
            _schema_chars[name] = cached
        total += cached
    return total


def catalog_chars(role: str) -> int:
    """Chars of the tier-0 ``name: blurb`` catalog carried in the prompt."""
    cached = _catalog_chars.get(role)
    if cached is not None:
        return cached
    registry = _registry_for(role)
    size = 0
    if registry is not None:
        try:
            size = len(registry.catalog_block() or "")
        except Exception:  # noqa: BLE001
            size = 0
    _catalog_chars[role] = size
    return size


def reset_sizing_caches() -> None:
    """Drop registry refs and size caches. Tests only."""
    _registries.clear()
    _schema_chars.clear()
    _catalog_chars.clear()
    _compactions.clear()
    _offloads.clear()


# ----------------------------------------------------------------------
# Segment attribution
# ----------------------------------------------------------------------

_MARKERS_CACHE: tuple[tuple[str, str], ...] | None = None


def _markers() -> tuple[tuple[str, str], ...]:
    """``(segment, marker)`` for the blocks we can find inside a system prompt.

    Imported from the modules that own the strings, never re-spelled here — a
    second copy would drift and silently mis-attribute prompt bytes. Imported
    lazily: ``core.prompts`` pulls in ``core.__init__``, which imports
    ``engine``, and this module is imported *from* engine's middleware.
    """
    global _MARKERS_CACHE
    if _MARKERS_CACHE is not None:
        return _MARKERS_CACHE
    markers: list[tuple[str, str]] = []
    try:
        from yuyutsava.memory.agent_memory import INDEX_BLOCK_HEADER

        markers.append(("memory", INDEX_BLOCK_HEADER.strip()))
    except Exception:  # noqa: BLE001
        pass
    try:
        from yuyutsava.context.injector import MEMORY_BLOCK_PREFIX

        markers.append(("memory", MEMORY_BLOCK_PREFIX))
    except Exception:  # noqa: BLE001
        pass
    try:
        from yuyutsava.skills.injector import SKILLS_BLOCK_PREFIX

        markers.append(("skills", SKILLS_BLOCK_PREFIX))
    except Exception:  # noqa: BLE001
        pass
    _MARKERS_CACHE = tuple(markers)
    return _MARKERS_CACHE


def split_system_chars(text: str) -> dict[str, int]:
    """Attribute one system-prompt block's chars to segments.

    Each known block runs from its marker to the next marker (or the end), and
    everything before the first marker is the prompt proper. A block the
    markers do not cover stays in ``system`` — under-attributing to the
    catch-all is honest, inventing a row is not.
    """
    out = {"system": 0, "memory": 0, "skills": 0}
    if not text:
        return out
    hits: list[tuple[int, str]] = []
    for segment, marker in _markers():
        if not marker:
            continue
        idx = text.find(marker)
        if idx >= 0:
            hits.append((idx, segment))
    hits.sort()
    if not hits:
        out["system"] = len(text)
        return out
    out["system"] = hits[0][0]
    for i, (start, segment) in enumerate(hits):
        end = hits[i + 1][0] if i + 1 < len(hits) else len(text)
        out[segment] = out.get(segment, 0) + (end - start)
    return out


def _is_offloaded_digest(content: Any) -> bool:
    return isinstance(content, str) and content.lstrip().startswith('{"offloaded": true')


# ----------------------------------------------------------------------
# The policy
# ----------------------------------------------------------------------


@dataclass
class _ThreadState:
    """Running totals for one conversation, and its calibration."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    calibration: float = 1.0
    calibrated: bool = False
    #: Raw (uncalibrated) estimate for the call now in flight, kept so the
    #: provider's answer can be divided by the estimate of the *same* prompt.
    pending_estimate: int = 0
    started_at: float = field(default_factory=time.time)


#: Per-conversation totals, keyed by thread. Module-level rather than per-policy
#: because a conversation's spend is not one graph's: a master and every
#: subagent it delegates to get their own policy instance, and a subagent's
#: tokens are still money this conversation spent. Per-instance dicts made each
#: subagent start counting from zero and then publish its own small total over
#: the master's.
_thread_states: _Bounded = _Bounded()


def _state_for(thread_id: str) -> _ThreadState:
    state = _thread_states.get(thread_id)
    if state is None:
        state = _ThreadState()
        _thread_states[thread_id] = state
    return state


def reset_thread_state() -> None:
    """Drop all per-conversation totals. Tests only."""
    _thread_states.clear()


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
        #: still accumulates — see ``_thread_states``.
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
            tools_chars = catalog_chars(self._role) + tool_schema_chars(
                self._role, call.tool_names
            )
            state = self._state(snap.thread_id)
            tools = self._scaled(approx_tokens_chars(tools_chars, model=self._model),
                                 state.calibration)
            snap = replace(
                snap,
                tools_tokens=tools,
                tool_count=len(call.tool_names),
            )
            # The calibration divisor must describe the prompt that produced
            # the provider's number, so record it once the whole prompt is
            # accounted for — tool schemas included.
            state.pending_estimate = self._raw_estimate(snap, state.calibration)
            bus().publish(snap)
        except Exception:  # noqa: BLE001
            logger.debug("context meter: pre-call publish failed", exc_info=True)

    async def after_model(self, turn: Turn) -> Directive | None:
        usage = turn.usage
        if usage is None or not usage.any_tokens:
            return None
        try:
            self._record_call(turn.thread_id, usage)
        except Exception:  # noqa: BLE001
            logger.debug("context meter: post-call publish failed", exc_info=True)
        return None

    # -- internals ----------------------------------------------------------

    def _state(self, thread_id: str) -> _ThreadState:
        return _state_for(thread_id)

    def _price_table(self) -> dict[str, tuple[float, float]]:
        if self._prices is None:
            from yuyutsava.core.model_router import load_price_table

            self._prices = load_price_table()
        return self._prices

    @staticmethod
    def _scaled(tokens: int, calibration: float) -> int:
        return int(round(tokens * calibration))

    def _raw_estimate(self, snap: ContextSnapshot, calibration: float) -> int:
        """Undo the calibration, recovering the estimate as first computed."""
        if calibration <= 0:
            return snap.used_tokens
        return int(round(snap.used_tokens / calibration))

    def _measure(self, turn: Turn) -> ContextSnapshot:
        """Segment the message list. Tool schemas are added by the reviser."""
        from yuyutsava.retrieval.injector import last_block_chars

        state = self._state(turn.thread_id)
        cal = state.calibration

        system_chars = {"system": 0, "memory": 0, "skills": 0}
        convo: list[Any] = []
        offloaded = 0
        for msg in turn.messages:
            content = getattr(msg, "content", "")
            if getattr(msg, "type", "") == "system":
                text = content if isinstance(content, str) else str(content)
                for key, chars in split_system_chars(text).items():
                    system_chars[key] = system_chars.get(key, 0) + chars
                continue
            convo.append(msg)
            if _is_offloaded_digest(content):
                offloaded += 1

        # Blocks the retrieval injectors append at model-call time. They are
        # real prompt bytes but arrive after every before_model hook has run,
        # so counting the message list alone under-reports the prompt.
        injected = last_block_chars()
        for prefix, chars in injected.items():
            key = self._segment_for_prefix(prefix)
            system_chars[key] = system_chars.get(key, 0) + chars

        return ContextSnapshot(
            role=self._role,
            thread_id=turn.thread_id,
            model=self._model_name,
            max_input_tokens=int(getattr(self._settings, "max_input_tokens", 0) or 0),
            compact_trigger_tokens=int(
                getattr(self._settings, "compact_trigger_tokens", 0) or 0
            ),
            system_tokens=self._scaled(
                approx_tokens_chars(system_chars["system"], model=self._model), cal),
            memory_tokens=self._scaled(
                approx_tokens_chars(system_chars["memory"], model=self._model), cal),
            skills_tokens=self._scaled(
                approx_tokens_chars(system_chars["skills"], model=self._model), cal),
            messages_tokens=self._scaled(
                approx_tokens(convo, model=self._model), cal),
            message_count=len(turn.messages),
            offloaded_digests=offloaded,
            calibrated=state.calibrated,
            **self._carry(turn.thread_id),
        )

    @staticmethod
    def _segment_for_prefix(prefix: str) -> str:
        for segment, marker in _markers():
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
        from yuyutsava.core.model_router import estimate_cost_usd, is_priced

        state = self._state(thread_id)
        model = self._model_name or getattr(usage, "model", "") or ""
        prices = self._price_table()
        cost = estimate_cost_usd(
            model, usage.input_tokens, usage.output_tokens, prices
        )

        state.calls += 1
        state.input_tokens += usage.input_tokens
        state.output_tokens += usage.output_tokens
        state.cache_read_tokens += getattr(usage, "cache_read_tokens", 0)
        state.cost_usd += cost

        # Calibrate: the provider just told us what our own estimate of this
        # exact prompt was worth.
        #
        # Only the window owner may do this. The pending estimate describes the
        # master's prompt, and the thread state is shared, so a subagent
        # reaching it would divide the master's estimate by its own, much
        # smaller, token count and permanently skew the panel's scale.
        if self._window:
            if state.pending_estimate > 0 and usage.input_tokens > 0:
                ratio = usage.input_tokens / state.pending_estimate
                state.calibration = max(_CALIBRATION_MIN, min(_CALIBRATION_MAX, ratio))
                state.calibrated = True
            state.pending_estimate = 0

        # Keep the window rows from the last measurement: this call changed what
        # was spent, not what is in the prompt. For a subagent (window=False)
        # that also means the master's role and model stay on the snapshot —
        # relabelling the panel mid-delegation would describe a conversation
        # the segment rows are not about.
        previous = bus().latest(thread_id)
        base = previous or ContextSnapshot(
            role=self._role, thread_id=thread_id, model=model
        )
        compactions, offloads = counters(thread_id)
        bus().publish(replace(
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
        ))


__all__ = [
    "SEGMENT_LABELS",
    "ContextMeterPolicy",
    "ContextSnapshot",
    "MeterBus",
    "bus",
    "catalog_chars",
    "counters",
    "note_bound_tools",
    "note_compaction",
    "note_offload",
    "note_tool_registry",
    "reset_sizing_caches",
    "reset_thread_state",
    "split_system_chars",
    "tool_schema_chars",
]
