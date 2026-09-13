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

A provider reports one number for a whole prompt and never a figure per region,
so segment *sizes* have to be estimated from characters. The *total* does not:
``usage_metadata["input_tokens"]`` is an exact measurement of the very prompt we
just described. So the headline is anchored on it, and only the growth since is
estimated::

    used = last measured input_tokens + estimate of what was appended since

On the publish right after a call that second term is the provider's own
``output_tokens``, so the headline is measured end to end
(:attr:`ContextSnapshot.window_measured`). The segment rows are then reconciled
to sum to that total: a breakdown of a measured number rather than a sum of
guesses, which is also why "used" and "last call in" can no longer disagree.

**Two correction factors, not one**, because the halves of a prompt mis-estimate
in opposite directions. Roughly 20k *characters* of JSON tool schema reach
Gemini as ``FunctionDeclaration`` protos, where ``chars/4`` overcounts about
twofold, while ``chars/4`` estimates prose reasonably. Averaging the two got
both wrong, and worse, a single factor multiplied *every* row on *every*
publish — so when the factor moved, the conversation changed size. On 14 Sep a
session read 26.9k against a measured 13.4k, then **fell** to 23.5k as the
correction landed, while the real prompt had grown to 27.6k: a headline moving
for a reason that had nothing to do with the conversation. The static prefix and
the messages are therefore fitted separately. A conversation's first call
carries almost no message tokens and is thus a free exact reading of the prefix;
after that ``measured - prefix`` measures the messages by subtraction. Factors
are remembered per model, so a second conversation starts calibrated instead of
reading 2x high for one call.

Last-call and session figures are always raw provider numbers. Renderers must
keep the distinction visible; ``/context`` marks estimates with ``≈``.
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

#: Guard rails on a fitted correction factor. Outside this range the estimate
#: and the provider are not describing the same prompt (a provider reporting
#: cumulative tokens, a mid-call compaction) — such a call is recorded as spend
#: but never fitted from, and never anchored on. Wide enough to actually *hold*
#: a real correction: the old [0.5, 2.0] pinned a genuine 0.499 at the floor and
#: left the panel ~2x wrong with nothing to say the clamp was the problem.
_FACTOR_MIN = 0.2
_FACTOR_MAX = 5.0

#: At or below this share of message tokens a call is a near-pure prefix, so its
#: reported input tokens measure the static prefix on their own. A conversation's
#: first call is the archetype: 5 message tokens against 26.9k of prefix.
_PREFIX_ONLY_SHARE = 0.15

#: The segments that make up the static prefix — everything that is not the
#: conversation itself, and so the part a prefix-only call measures.
_STATIC_KEYS = ("system", "tools", "memory", "skills")

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
    #: Whether the headline total rests on a provider measurement rather than
    #: being a free-floating character estimate.
    anchored: bool = False
    #: Whether the headline is the provider's own arithmetic end to end — true
    #: on the publish right after a call, when nothing has been appended since
    #: and so nothing is estimated. Renderers drop the ``≈`` from the total.
    window_measured: bool = False

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
            "anchored": self.anchored,
            "window_measured": self.window_measured,
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


def _injected_block_chars() -> dict[str, int]:
    """Sizes of the blocks the retrieval injectors last rendered.

    Lazy import: ``retrieval.injector`` is cheap but this module is imported
    from engine's middleware assembly, and the fewer edges there the better.
    """
    try:
        from yuyutsava.retrieval.injector import last_block_chars

        return last_block_chars()
    except Exception:  # noqa: BLE001
        return {}


def _is_offloaded_digest(content: Any) -> bool:
    return isinstance(content, str) and content.lstrip().startswith('{"offloaded": true')


# ----------------------------------------------------------------------
# The policy
# ----------------------------------------------------------------------


@dataclass
class _ThreadState:
    """Running totals for one conversation, and its two correction factors."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0

    #: Corrections for the two halves of a prompt, fitted separately — see the
    #: module docstring for why one factor cannot serve both.
    static_factor: float = 1.0
    messages_factor: float = 1.0
    static_fitted: bool = False
    messages_fitted: bool = False
    calibrated: bool = False

    #: Raw (uncalibrated) per-segment estimate of the call now in flight, kept
    #: so the provider's answer can be divided by an estimate of the *same*
    #: prompt. Never the corrected figures the panel shows.
    pending_raw: dict[str, int] = field(default_factory=dict)

    #: The provider's measurement of the last call's prompt, and the raw
    #: estimate of that same prompt. Growth is measured against the second and
    #: added to the first, which is what stops a new correction factor from
    #: retroactively resizing the conversation.
    anchor_tokens: int = 0
    anchor_raw: int = 0

    started_at: float = field(default_factory=time.time)


#: Per-conversation totals, keyed by thread. Module-level rather than per-policy
#: because a conversation's spend is not one graph's: a master and every
#: subagent it delegates to get their own policy instance, and a subagent's
#: tokens are still money this conversation spent. Per-instance dicts made each
#: subagent start counting from zero and then publish its own small total over
#: the master's.
_thread_states: _Bounded = _Bounded()


#: Fitted factors by model name. They describe a tokenizer and a tool-schema
#: encoding — properties of the model, not of one thread — so a new conversation
#: on a model this process has already calibrated starts right instead of
#: reading ~2x high until its first call comes back.
_model_factors: _Bounded = _Bounded()


def _state_for(thread_id: str, model: str = "") -> _ThreadState:
    state = _thread_states.get(thread_id)
    if state is None:
        state = _ThreadState()
        seed = _model_factors.get(model) if model else None
        if seed is not None:
            state.static_factor, state.messages_factor = seed
            state.static_fitted = True
            state.messages_fitted = True
            state.calibrated = True
        _thread_states[thread_id] = state
    return state


def reset_thread_state() -> None:
    """Drop all per-conversation totals and learned factors. Tests only."""
    _thread_states.clear()
    _model_factors.clear()


def _clamp_factor(value: float) -> float:
    return max(_FACTOR_MIN, min(_FACTOR_MAX, value))


def _reconcile(rows: dict[str, int], target: int) -> dict[str, int]:
    """Scale *rows* so they sum to exactly *target*.

    The rows are a breakdown of a measured number, so they have to add up to
    it; a panel whose parts do not sum to its total is the whole complaint.
    Rounding drift lands on the largest row, where it is proportionally
    smallest.
    """
    total = sum(rows.values())
    if total <= 0 or target <= 0:
        return dict(rows)
    factor = target / total
    out = {key: max(0, int(round(value * factor))) for key, value in rows.items()}
    drift = target - sum(out.values())
    if drift and out:
        biggest = max(out, key=lambda k: out[k])
        out[biggest] = max(0, out[biggest] + drift)
    return out


def _allocate(state: _ThreadState, raw: dict[str, int], target: int) -> dict[str, int]:
    """Per-segment tokens that sum to *target*.

    The prefix rows carry the fitted estimate and ``messages`` takes the
    remainder, which makes the messages row a *measured residual* — total minus
    prefix — rather than a second independent guess. Should the prefix alone
    exceed the target (a badly fitted factor, or a target from a tiny call),
    everything is scaled proportionally instead so no row can go negative.
    """
    out = {
        key: max(0, int(round(raw.get(key, 0) * state.static_factor)))
        for key in _STATIC_KEYS
    }
    if target > 0 and sum(out.values()) < target:
        out["messages"] = target - sum(out.values())
        return out
    out["messages"] = max(
        0, int(round(raw.get("messages", 0) * state.messages_factor))
    )
    return _reconcile(out, target) if target > 0 else out


def _target_tokens(state: _ThreadState, raw: dict[str, int]) -> int:
    """What the window holds now: the last measurement plus what grew since.

    ``anchor_tokens`` is the provider's own count for the last call's prompt,
    so only the delta since is estimated — and it is scaled by the message
    factor, because everything appended between calls is conversation. The
    delta is signed: compaction shrinks the raw estimate and the headline has
    to fall with it rather than sit at a stale high-water mark, then re-anchor
    on the next real call.
    """
    prefix = int(round(
        sum(raw.get(key, 0) for key in _STATIC_KEYS) * state.static_factor
    ))
    if state.anchor_tokens <= 0:
        # Nothing measured on this thread yet; the seeded model factors are the
        # best correction available.
        return max(0, prefix + int(round(
            raw.get("messages", 0) * state.messages_factor
        )))
    grown = int(round((sum(raw.values()) - state.anchor_raw) * state.messages_factor))
    return max(prefix, state.anchor_tokens + grown)


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
            state = self._state(snap.thread_id)

            tools_chars = catalog_chars(self._role) + tool_schema_chars(
                self._role, call.tool_names
            )
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
            for prefix, chars in _injected_block_chars().items():
                key = self._segment_for_prefix(prefix)
                system_chars[key] = system_chars.get(key, 0) + chars

            # Raw, uncalibrated estimates, per segment. The provider's answer
            # has to be divided by an estimate of the same prompt, so these are
            # what gets kept — and the whole prompt is only accounted for here,
            # tool schemas included.
            raw = {
                "system": approx_tokens_chars(
                    system_chars["system"], model=self._model),
                "tools": approx_tokens_chars(tools_chars, model=self._model),
                "memory": approx_tokens_chars(
                    system_chars["memory"], model=self._model),
                "skills": approx_tokens_chars(
                    system_chars["skills"], model=self._model),
                # ``before_model`` leaves its raw message estimate here.
                "messages": snap.messages_tokens,
            }
            rows = _allocate(state, raw, _target_tokens(state, raw))
            state.pending_raw = raw
            bus().publish(replace(
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
            ))
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
        return _state_for(thread_id, self._model_name)

    def _price_table(self) -> dict[str, tuple[float, float]]:
        if self._prices is None:
            from yuyutsava.core.model_router import load_price_table

            self._prices = load_price_table()
        return self._prices

    def _fit(self, state: _ThreadState, usage: Any) -> None:
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
        if not _FACTOR_MIN <= measured / raw_total <= _FACTOR_MAX:
            # The estimate and the provider are not describing the same prompt.
            # The spend is real and has already been recorded, but anchoring the
            # window on this number — or fitting a factor from it — would put
            # the panel on a scale nothing else shares.
            logger.debug(
                "context meter: ignoring an implausible reading (%d reported "
                "against a %d-token estimate)", measured, raw_total,
            )
            return
        raw_prefix = sum(raw.get(key, 0) for key in _STATIC_KEYS)
        raw_messages = raw.get("messages", 0)
        if raw_messages / raw_total <= _PREFIX_ONLY_SHARE and raw_prefix > 0:
            state.static_factor = _clamp_factor(measured / raw_total)
            state.static_fitted = True
            state.calibrated = True
        elif state.static_fitted and raw_messages > 0:
            residual = measured - state.static_factor * raw_prefix
            if residual > 0:
                ratio = _clamp_factor(residual / raw_messages)
                state.messages_factor = (
                    (state.messages_factor + ratio) / 2
                    if state.messages_fitted else ratio
                )
                state.messages_fitted = True
                state.calibrated = True
        elif not state.static_fitted:
            # A resumed thread never sees a prefix-only call, so there is
            # nothing to separate the halves with. One factor for both is what
            # the anchor exists to make survivable.
            both = _clamp_factor(measured / raw_total)
            state.static_factor = both
            state.messages_factor = both
            state.calibrated = True
        state.anchor_tokens = measured
        state.anchor_raw = raw_total
        if state.calibrated and self._model_name:
            _model_factors[self._model_name] = (
                state.static_factor, state.messages_factor,
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
            if _is_offloaded_digest(content):
                offloaded += 1

        return ContextSnapshot(
            role=self._role,
            thread_id=turn.thread_id,
            model=self._model_name,
            max_input_tokens=int(getattr(self._settings, "max_input_tokens", 0) or 0),
            compact_trigger_tokens=int(
                getattr(self._settings, "compact_trigger_tokens", 0) or 0
            ),
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
        base = previous or ContextSnapshot(
            role=self._role, thread_id=thread_id, model=model
        )
        compactions, offloads = counters(thread_id)
        # The prompt has just been measured and the reply it produced is now
        # part of the window — and that is a provider number too. So for this
        # one publish the headline is the provider's own arithmetic, with
        # nothing estimated, and the rows are reconciled onto it.
        window: dict[str, Any] = {}
        if self._window and state.anchor_tokens > 0 and state.pending_raw:
            rows = _allocate(
                state, state.pending_raw,
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
            **window,
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
