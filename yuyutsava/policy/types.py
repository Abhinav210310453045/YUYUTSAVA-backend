"""What a policy sees and what it may answer — in YUYUTSAVA's own types.

Phase 4 step 4.2, [ADR-004](../../docs/architecture/review/adr/ADR-004-framework-boundary.md)
item 1.

## Scope of these types

The boundary is drawn **around what is ours** — the decision a policy makes and
the context it needs — and *not* around what is theirs. ``BaseMessage``,
``BaseTool`` and ``AgentState`` stay framework types (ADR-004, Alternative C:
wrapping them would touch nearly every module for mostly theoretical benefit).

So :class:`ToolCall` is a plain record carrying a tool's name, its arguments and
its call id — the three things every tool policy in this system actually reads —
and nothing a policy does not use. That is what makes a policy test one line of
construction instead of a graph.

## Why not a single ``PolicyAction`` return

ADR-004 sketched ``on_tool_call(ctx) -> PolicyAction``. Reading the six tool
policies first showed two distinct shapes: four decide **before** the tool runs
and may refuse it (permission, subagent gate, background-task cap, async check
guard), and two rewrite the **result** afterwards (offload, and the check guard
again). One hook cannot express both without the caller passing a handler —
which is the framework's own shape, and re-importing it would defeat the
exercise. Two hooks say it plainly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from yuyutsava.ports.ask import AskUser


@dataclass(frozen=True)
class ToolCall:
    """A tool the model asked to run, as a policy sees it."""

    #: What the model **asked for**. May name a tool that does not exist.
    name: str

    #: Arguments the model supplied. Never ``None`` — an absent ``args`` becomes
    #: an empty mapping, because every policy that reads it would otherwise need
    #: the same guard.
    args: Mapping[str, Any] = field(default_factory=dict)

    #: The framework's id for this call, echoed back on any refusal so the model
    #: can match question to answer. Empty when the caller did not supply one.
    id: str = ""

    #: Agent state, read-only. Present because a policy may need turn context;
    #: ``{}`` in tests that do not.
    state: Mapping[str, Any] = field(default_factory=dict)

    #: What the call **resolved to** — the bound tool's name, or ``None`` when
    #: the model named a tool that is not bound (hallucinated or mistyped).
    #:
    #: Separate from :attr:`name` because the difference is load-bearing and was
    #: getting lost. Three middlewares gated on the resolved tool via
    #: ``request.tool.name``; two guarded ``request.tool is None`` first and one
    #: did not, so a hallucinated tool name crashed the turn with
    #: ``AttributeError: 'NoneType' object has no attribute 'name'`` instead of
    #: taking the framework's unknown-tool path (finding BA). Comparing against
    #: an ``Optional[str]`` makes that mistake unavailable.
    resolved_tool: str | None = None

    #: How to put a question to the user. ``None`` means *nobody is listening* —
    #: a policy that would have asked must then choose a safe default rather than
    #: assume approval. See :class:`~yuyutsava.ports.ask.AskUser`.
    ask: AskUser | None = None


@dataclass(frozen=True)
class Denied:
    """Refuse the call. *message* is what the model receives instead of a result.

    The tool does not run. The text is written for the model, not the user: it
    has to explain the refusal well enough that the model picks a different
    approach rather than retrying the same command.
    """

    message: str

    #: ``ToolMessage.status``. ``"error"`` presents the refusal as a *failure*
    #: rather than a result, which some providers surface differently.
    #:
    #: This is a field rather than a constant because the existing refusals
    #: disagree: the permission block used the default, the concurrency cap used
    #: ``"error"``. Nothing recorded that as a decision. Both are carried over
    #: exactly — harmonising them would be a behaviour change smuggled into a
    #: migration, and is a separate call to make deliberately.
    status: str = "success"

    #: Label the result with the tool's name. Same story: the permission refusal
    #: set ``name``, the concurrency-cap refusal did not.
    named: bool = True


@dataclass(frozen=True)
class Raw:
    """Escape hatch — a framework-native value the adapter returns untouched.

    ADR-004 predicted this: *"expect at least one policy to need an escape hatch.
    Grant it explicitly and document it rather than weakening the protocol for
    everyone."* The known case is the async check guard, which replays a
    LangGraph ``Command`` for an already-answered background task; there is no
    YUYUTSAVA-level meaning to express, only a framework object to hand back.

    Reaching for this is a signal. A second unrelated use means the protocol is
    missing a concept — add the concept, do not widen the hatch.
    """

    value: Any


#: What a before-hook may answer. ``None`` means "no opinion, carry on" — the
#: overwhelmingly common case, which is why it is the falsy one.
ToolDecision = Denied | Raw | None


@dataclass
class ModelCall:
    """A model call a policy may revise before it is made.

    Five policies revise model calls and **none of them short-circuits** — they
    all edit the request and delegate. So this is a mutable record of edits, not
    a decision type: a policy calls the methods below and the adapter replays
    them onto the framework's request afterwards.

    ## Why edits and not the request itself

    Four of the five did the same eight lines by hand — *"append a text block to
    the system message, and cope with there being no system message yet"* —
    written out separately in ``VoiceStyleMiddleware``,
    ``SubagentGateMiddleware`` and ``RetrievalInjectionMiddleware``, with a
    fourth variant in ``FilesystemPromptOverrideMiddleware``. That block is
    framework plumbing, and duplicating it four times is what made those classes
    need a framework to test.

    Recording the edit instead means the plumbing exists once, in the adapter,
    and a policy test asserts *what a policy decided to change* without
    constructing a ``ModelRequest`` or a ``SystemMessage``.

    ## Non-text blocks

    A system message's blocks are not all text. :attr:`system_texts` holds
    ``None`` at those positions, and the adapter carries the original block
    across untouched — the same preservation ``FilesystemPromptOverrideMiddleware``
    did by hand. Flattening the prompt to ``list[str]`` would have dropped them
    silently.
    """

    #: Text of each system-prompt block in order; ``None`` where the block is
    #: not text. Read-only — use the methods below to change anything.
    system_texts: tuple[str | None, ...] = ()

    #: Whether there is a system message at all. Distinct from an empty
    #: :attr:`system_texts`, and load-bearing: each of the three appending
    #: middlewares had its own ``system_message is None`` branch and they did
    #: **not** agree — voice-style stripped leading newlines from its addendum,
    #: the subagent gate used it verbatim, and retrieval omitted the ``"\\n\\n"``
    #: separator it adds everywhere else. Nothing recorded that as a decision.
    #: deepagents always supplies a system prompt so none of it runs in
    #: production, but the differences are reproduced rather than quietly
    #: harmonised inside a migration.
    has_system_prompt: bool = True

    #: Names of the tools bound for this call, in order.
    tool_names: tuple[str, ...] = ()

    #: The last message's text if it is a human turn, else ``""``. Flattened
    #: from whatever block shape the message carries.
    latest_human_text: str = ""

    #: Agent state, read-only.
    state: Mapping[str, Any] = field(default_factory=dict)

    # -- recorded edits, replayed by the adapter -----------------------------
    appended: list[str] = field(default_factory=list)
    #: index -> replacement text, or ``None`` to drop the block entirely.
    rewritten: dict[int, str | None] = field(default_factory=dict)
    #: Tool names to remove. ``set()`` means "remove nothing".
    suppressed_tools: set[str] = field(default_factory=set)

    # -- the policy-facing API ----------------------------------------------

    def text_blocks(self) -> list[tuple[int, str]]:
        """``(index, text)`` for each block that is text. Skips the rest."""
        return [(i, t) for i, t in enumerate(self.system_texts) if t is not None]

    def append_system_text(self, text: str) -> None:
        """Add *text* as a new block at the end of the system prompt."""
        if text:
            self.appended.append(text)

    def rewrite_system_block(self, index: int, text: str | None) -> None:
        """Replace the block at *index*, or drop it entirely with ``None``."""
        self.rewritten[index] = text

    def suppress_tools(self, names: Iterable[str]) -> None:
        """Remove these tools from the call. Names not bound are ignored."""
        self.suppressed_tools.update(names)

    @property
    def changed(self) -> bool:
        """Whether any policy actually edited this call.

        The adapter skips rebuilding the request when nothing changed, which
        keeps the untouched case free — and the untouched case is the common
        one for every profile that wires few policies.
        """
        return bool(self.appended or self.rewritten or self.suppressed_tools)


@dataclass(frozen=True)
class Usage:
    """Tokens the model reported for the most recent AI message.

    Both ``BudgetMiddleware`` and ``UsageRecorder`` dug this out of state with
    near-identical code — find the last ``AIMessage``, read ``usage_metadata``,
    cope with it being a dict or an object, coerce to ``int``. Two copies of one
    extraction, and the thing they extract is the input to a spend ceiling.
    Resolved once by the adapter now.

    ``None`` on :attr:`Turn.usage` means the model reported nothing — fakes, and
    providers that omit ``usage_metadata``. Both policies already treated that as
    "skip", never as zero.
    """

    input_tokens: int = 0
    output_tokens: int = 0

    #: What the provider called the model on this response
    #: (``response_metadata["model_name"]``), or ``""``. The usage recorder falls
    #: back to this when it was built without a model name.
    model: str = ""

    @property
    def any_tokens(self) -> bool:
        return bool(self.input_tokens or self.output_tokens)


@dataclass(frozen=True)
class Turn:
    """The conversation as an observer policy sees it."""

    #: Messages currently in state. Framework types — ADR-004 Alternative C
    #: keeps the boundary off message types, and a transcript recorder's whole
    #: job is persisting them as they are.
    messages: tuple[Any, ...] = ()

    #: The thread this turn belongs to, resolved from the run config. ``""``
    #: outside a graph run, which every consumer already treats as "skip".
    thread_id: str = ""

    #: Tokens for the latest AI message, or ``None`` if the model reported none.
    usage: Usage | None = None

    state: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Directive:
    """Add a system instruction to the conversation.

    The only state update any observer makes: ``BudgetMiddleware`` injects a
    wrap-up instruction when the token ceiling is reached. Expressed as *what to
    say* rather than as a ``{"messages": [SystemMessage(...)]}`` dict, so the
    policy that decides it needs no message types.
    """

    text: str


__all__ = [
    "Denied",
    "Directive",
    "ModelCall",
    "Raw",
    "ToolCall",
    "ToolDecision",
    "Turn",
    "Usage",
]
