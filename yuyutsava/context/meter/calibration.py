"""Turning a character estimate into a token count the provider would agree with.

A provider reports one number for a whole prompt and never a figure per region,
so segment *sizes* have to be estimated from characters. The *total* does not:
``usage_metadata["input_tokens"]`` is an exact measurement of the very prompt
just described. So the headline is anchored on it, and only the growth since is
estimated::

    used = last measured input_tokens + estimate of what was appended since

The rows are then reconciled to sum to that total — a breakdown of a measured
number rather than a sum of guesses, which is also why "used" and "last call in"
cannot disagree.

**Two correction factors, not one**, because the halves of a prompt mis-estimate
in opposite directions. Roughly 20k *characters* of JSON tool schema reach
Gemini as ``FunctionDeclaration`` protos, where ``chars/4`` overcounts about
twofold, while ``chars/4`` estimates prose reasonably. Averaging the two gets
both wrong, and a single factor multiplies *every* row on *every* publish — so
when the factor moves, the conversation appears to change size. A session can
read 26.9k against a measured 13.4k and then *fall* to 23.5k as the correction
lands, while the real prompt has grown to 27.6k: a headline moving for a reason
that has nothing to do with the conversation.

The static prefix and the messages are therefore fitted separately. A
conversation's first call carries almost no message tokens and is thus a free
exact reading of the prefix; after that ``measured - prefix`` measures the
messages by subtraction. Factors are remembered per model, because they
describe a tokenizer and a tool-schema encoding rather than one conversation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from yuyutsava.context.meter.bounded import Bounded

#: Guard rails on a fitted correction factor. Outside this range the estimate
#: and the provider are not describing the same prompt (a provider reporting
#: cumulative tokens, a mid-call compaction) — such a call is recorded as spend
#: but never fitted from, and never anchored on. Wide enough to actually *hold*
#: a real correction: the old [0.5, 2.0] pinned a genuine 0.499 at the floor and
#: left the panel ~2x wrong with nothing to say the clamp was the problem.
FACTOR_MIN = 0.2


FACTOR_MAX = 5.0


#: At or below this share of message tokens a call is a near-pure prefix, so its
#: reported input tokens measure the static prefix on their own. A conversation's
#: first call is the archetype: 5 message tokens against 26.9k of prefix.
PREFIX_ONLY_SHARE = 0.15


#: The segments that make up the static prefix — everything that is not the
#: conversation itself, and so the part a prefix-only call measures.
STATIC_KEYS = ("system", "tools", "memory", "skills")


@dataclass
class ThreadState:
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
_thread_states: Bounded = Bounded()


#: Fitted factors by model name. They describe a tokenizer and a tool-schema
#: encoding — properties of the model, not of one thread — so a new conversation
#: on a model this process has already calibrated starts right instead of
#: reading ~2x high until its first call comes back.
model_factors: Bounded = Bounded()


def state_for(thread_id: str, model: str = "") -> ThreadState:
    state = _thread_states.get(thread_id)
    if state is None:
        state = ThreadState()
        seed = model_factors.get(model) if model else None
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
    model_factors.clear()


def clamp_factor(value: float) -> float:
    return max(FACTOR_MIN, min(FACTOR_MAX, value))


def reconcile(rows: dict[str, int], target: int) -> dict[str, int]:
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


def allocate(state: ThreadState, raw: dict[str, int], target: int) -> dict[str, int]:
    """Per-segment tokens that sum to *target*.

    The prefix rows carry the fitted estimate and ``messages`` takes the
    remainder, which makes the messages row a *measured residual* — total minus
    prefix — rather than a second independent guess. Should the prefix alone
    exceed the target (a badly fitted factor, or a target from a tiny call),
    everything is scaled proportionally instead so no row can go negative.
    """
    out = {key: max(0, int(round(raw.get(key, 0) * state.static_factor))) for key in STATIC_KEYS}
    if target > 0 and sum(out.values()) < target:
        out["messages"] = target - sum(out.values())
        return out
    out["messages"] = max(0, int(round(raw.get("messages", 0) * state.messages_factor)))
    return reconcile(out, target) if target > 0 else out


def target_tokens(state: ThreadState, raw: dict[str, int]) -> int:
    """What the window holds now: the last measurement plus what grew since.

    ``anchor_tokens`` is the provider's own count for the last call's prompt,
    so only the delta since is estimated — and it is scaled by the message
    factor, because everything appended between calls is conversation. The
    delta is signed: compaction shrinks the raw estimate and the headline has
    to fall with it rather than sit at a stale high-water mark, then re-anchor
    on the next real call.
    """
    prefix = int(round(sum(raw.get(key, 0) for key in STATIC_KEYS) * state.static_factor))
    if state.anchor_tokens <= 0:
        # Nothing measured on this thread yet; the seeded model factors are the
        # best correction available.
        return max(0, prefix + int(round(raw.get("messages", 0) * state.messages_factor)))
    grown = int(round((sum(raw.values()) - state.anchor_raw) * state.messages_factor))
    return max(prefix, state.anchor_tokens + grown)


__all__ = [
    "FACTOR_MAX",
    "FACTOR_MIN",
    "PREFIX_ONLY_SHARE",
    "STATIC_KEYS",
    "ThreadState",
    "allocate",
    "clamp_factor",
    "model_factors",
    "reconcile",
    "reset_thread_state",
    "state_for",
    "target_tokens",
]
