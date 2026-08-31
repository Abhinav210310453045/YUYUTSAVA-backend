"""The one place this system knows about ``AgentMiddleware``.

Phase 4 step 4.2, [ADR-004](../../docs/architecture-review/adr/ADR-004-framework-boundary.md)
item 1. Fourteen framework subclasses collapse to one adapter; a hook-signature
change becomes a one-file change instead of a fourteen-file change.

## Ordering

Middleware nests: the first entry in the stack is outermost, and each wraps the
next via ``handler``. Before-hooks therefore run outermost-first and after-hooks
outermost-**last**. Collapsing several policies into one adapter means that
nesting is no longer done by the framework, so this class reproduces it
explicitly — :meth:`awrap_tool_call` runs ``before_tool`` in list order and
``after_tool`` in reverse.

That is not cosmetic. The offload policy shrinks an oversized tool result; a
policy that inspects results and sat *outside* it must keep seeing the digest
rather than the original, and one that sat *inside* must keep seeing the
original. Reversing the after-pass is what preserves that.

## First refusal wins

A refused call short-circuits: no later policy's ``before_tool`` runs, the tool
does not run, and no ``after_tool`` runs — because there is no result to rewrite.
This matches nesting exactly, where an outer middleware returning without calling
``handler`` skips everything inside it.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Sequence

from langchain.agents.middleware import AgentMiddleware

from yuyutsava.policy.base import Policy
from yuyutsava.policy.types import Denied, ModelCall, Raw, ToolCall, Turn, Usage
from yuyutsava.ports.ask import AskUser

logger = logging.getLogger("yuyutsava.policy.adapter")


class LangChainPolicyAdapter(AgentMiddleware):  # type: ignore[misc]
    """Run a list of :class:`~yuyutsava.policy.base.Policy` objects as middleware."""

    def __init__(
        self,
        policies: Sequence[Policy],
        *,
        ask: AskUser | None = None,
    ) -> None:
        super().__init__()
        self._policies = tuple(policies)
        # Resolved once at build time rather than per call: the default reaches
        # into LangGraph, and a policy that never asks should not pay for it.
        self._ask = ask if ask is not None else _default_ask()
        self._before = tuple(p for p in self._policies if p.handles_before())
        self._after = tuple(reversed([p for p in self._policies if p.handles_after()]))
        self._revisers = tuple(p for p in self._policies if p.handles_model_call())
        self._observers = {
            phase: tuple(p for p in self._policies if p.observes(phase))
            for phase in ("before_model", "after_model", "after_agent")
        }

    @property
    def name(self) -> str:  # type: ignore[override]
        """Identity within one middleware stack, and it must be unique.

        ``create_deep_agent`` asserts ``len({m.name for m in middleware}) ==
        len(middleware)`` and rejects the graph outright otherwise. The base
        class's ``name`` is the class name, so **two adapters in one stack is a
        hard build failure** — which is what happens the moment more than one
        policy is migrated while each keeps its own adapter at its own position.

        Found by ``scripts/verify_framework_contract.py``, which actually builds
        a graph. The fingerprint gate never saw it: it intercepts
        ``create_deep_agent`` and records the kwargs, so the validation that
        rejects this never runs there.

        Naming the adapter after what it carries satisfies the constraint and
        keeps each policy at the exact position its middleware held, so no
        ordering had to change to unblock the migration.
        """
        return f"{type(self).__name__}[{','.join(p.name for p in self._policies)}]"

    @property
    def policies(self) -> tuple[Policy, ...]:
        """The policies this adapter runs, in stack order.

        Public because the agent fingerprint reports them: an adapter that
        reported only its own class name would make the stack *less* legible than
        the fourteen subclasses it replaces, which is the opposite of the point.
        """
        return self._policies

    def _to_call(self, request: Any) -> ToolCall:
        """Translate the framework's tool request into ours.

        Everything defensive about the framework's shape lives here, once,
        instead of at the top of six policies.
        """
        raw = getattr(request, "tool_call", None) or {}
        args = raw.get("args") if isinstance(raw, dict) else None
        # `request.tool` is None when the model named a tool that is not bound.
        # Reading `.name` off it unguarded is how the interrupt-patch middleware
        # crashed a whole turn on a mistyped tool name (finding BA); resolving it
        # once, here, is what makes that unavailable to policies.
        tool = getattr(request, "tool", None)
        resolved = getattr(tool, "name", None) if tool is not None else None
        return ToolCall(
            name=(raw.get("name", "") if isinstance(raw, dict) else "") or "",
            args=args if isinstance(args, dict) else {},
            id=(raw.get("id", "") if isinstance(raw, dict) else "") or "",
            state=getattr(request, "state", None) or {},
            resolved_tool=resolved if isinstance(resolved, str) else None,
            ask=self._ask,
        )

    def _denial(self, call: ToolCall, decision: Denied) -> Any:
        from langchain_core.messages import ToolMessage

        return ToolMessage(
            content=decision.message,
            tool_call_id=call.id,
            status=decision.status,
            **({"name": call.name} if decision.named else {}),
        )

    # ------------------------------------------------------------------
    # Model calls
    # ------------------------------------------------------------------

    def _to_model_call(self, request: Any) -> ModelCall:
        """Translate the framework's ``ModelRequest`` into ours."""
        system_message = getattr(request, "system_message", None)
        blocks = list(getattr(system_message, "content_blocks", None) or [])
        texts = tuple(
            b.get("text", "") if isinstance(b, dict) and b.get("type") == "text" else None
            for b in blocks
        )
        return ModelCall(
            system_texts=texts,
            has_system_prompt=system_message is not None,
            tool_names=tuple(_tool_name(t) for t in (getattr(request, "tools", None) or [])),
            latest_human_text=_latest_human_text(getattr(request, "messages", None)),
            state=getattr(request, "state", None) or {},
        )

    def _apply(self, request: Any, call: ModelCall) -> Any:
        """Replay a ``ModelCall``'s recorded edits onto the framework request.

        This is the eight lines that were written out four times across
        ``VoiceStyleMiddleware``, ``SubagentGateMiddleware``,
        ``RetrievalInjectionMiddleware`` and (in a variant)
        ``FilesystemPromptOverrideMiddleware``. Once, here.
        """
        from langchain_core.messages import SystemMessage

        if not call.changed:
            return request

        overrides: dict[str, Any] = {}

        if call.appended or call.rewritten:
            system_message = getattr(request, "system_message", None)
            original = list(getattr(system_message, "content_blocks", None) or [])
            blocks: list[Any] = []
            for i, block in enumerate(original):
                if i in call.rewritten:
                    replacement = call.rewritten[i]
                    if replacement is None:
                        continue  # dropped
                    blocks.append({"type": "text", "text": replacement})
                else:
                    # Non-text blocks land here untouched, which is the whole
                    # reason ModelCall keeps them as None rather than flattening.
                    blocks.append(block)
            blocks.extend({"type": "text", "text": t} for t in call.appended)
            overrides["system_message"] = SystemMessage(content_blocks=blocks)

        if call.suppressed_tools:
            overrides["tools"] = [
                t for t in (getattr(request, "tools", None) or [])
                if _tool_name(t) not in call.suppressed_tools
            ]

        return request.override(**overrides)

    async def awrap_model_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        if not self._revisers:
            return await handler(request)
        call = self._to_model_call(request)
        for policy in self._revisers:
            await policy.revise_model_call(call)
        return await handler(self._apply(request, call))

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        """Async-only, for the same reason as :meth:`wrap_tool_call`.

        One nuance worth recording: ``RetrievalInjectionMiddleware`` *did* have a
        sync path, and it deliberately skipped injection rather than block the
        loop — so on the sync path memory and skills were silently not injected.
        Raising is a change from that, and the right one: silently sending the
        model a prompt missing its retrieved context is exactly the class of
        quiet degradation this review exists to remove. Unreachable today; no
        graph in this codebase is driven synchronously.
        """
        if not self._revisers:
            return handler(request)
        raise RuntimeError(
            f"LangChainPolicyAdapter reached the synchronous model path with "
            f"policies {[p.name for p in self._revisers]}. Policy hooks are "
            f"async; drive the graph with ainvoke()/astream()."
        )

    # ------------------------------------------------------------------
    # Observers
    # ------------------------------------------------------------------

    def _to_turn(self, state: Any) -> Turn:
        """Translate agent state into a ``Turn``, resolving usage once."""
        messages = state.get("messages", []) if isinstance(state, dict) else (
            getattr(state, "messages", None) or [])
        return Turn(
            messages=tuple(messages),
            thread_id=_current_thread_id(),
            usage=_latest_usage(messages),
            state=state if isinstance(state, dict) else {},
        )

    async def _observe(self, phase: str, state: Any) -> dict[str, Any] | None:
        """Run every policy that implements *phase*; collect their directives."""
        policies = self._observers.get(phase, ())
        if not policies:
            return None
        turn = self._to_turn(state)
        directives = [d for d in
                      [await getattr(p, phase)(turn) for p in policies]
                      if d is not None]
        if not directives:
            return None
        from langchain_core.messages import SystemMessage

        return {"messages": [SystemMessage(content=d.text) for d in directives]}

    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return await self._observe("before_model", state)

    async def aafter_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return await self._observe("after_model", state)

    async def aafter_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return await self._observe("after_agent", state)

    # ------------------------------------------------------------------
    # Tool calls
    # ------------------------------------------------------------------

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        """Refuse to run. Policy hooks are ``async``; this path cannot honour them.

        LangChain picks the sync or async hook by how the graph was driven.
        Omitting this method entirely would be the dangerous option: the sync
        path would simply not call the adapter, and **every policy would be
        silently skipped** — including the permission gate, whose only job is
        stopping destructive commands.

        Nothing in this codebase invokes a graph synchronously (verified: no
        ``.invoke()``/``.stream()`` on any agent or graph), and
        ``PermissionMiddleware`` was already async-only before the migration. So
        this is unreachable today. If it ever becomes reachable, a loud failure
        naming the cause beats a safety layer that quietly stopped running.
        """
        raise RuntimeError(
            f"LangChainPolicyAdapter reached the synchronous tool path with "
            f"policies {[p.name for p in self._policies]}. Policy hooks are "
            f"async; drive the graph with ainvoke()/astream(), or give the "
            f"adapter a sync bridge. Running the tool without these policies is "
            f"not an option — one of them is the permission gate."
        )

    async def awrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        call = self._to_call(request)

        for policy in self._before:
            decision = await policy.before_tool(call)
            if decision is None:
                continue
            if isinstance(decision, Raw):
                return decision.value
            if isinstance(decision, Denied):
                logger.debug(
                    "policy %s refused %s: %s",
                    policy.name, call.name, decision.message[:120],
                )
                return self._denial(call, decision)
            raise TypeError(
                f"{policy.name}.before_tool returned {type(decision).__name__}; "
                f"expected Denied, Raw, or None"
            )

        result = await handler(request)

        for policy in self._after:
            result = await policy.after_tool(call, result)
        return result

    def __repr__(self) -> str:
        return f"<LangChainPolicyAdapter {[p.name for p in self._policies]}>"


def collapse_policy_adapters(middleware: list[Any]) -> list[Any]:
    """Merge every :class:`LangChainPolicyAdapter` in *middleware* into one.

    Phase 4 step 4.7. Each policy was migrated with its own adapter at the exact
    position its middleware held, so that every cutover diff was one entry
    swapped in place and provable on its own. That leaves N wrap layers on the
    hot path where one will do — the indirection cost ADR-004 flagged as the
    thing to watch.

    **Placement: after every non-adapter entry.** Order *within* a hook chain is
    decided by list position; order *between* chains is decided by the agent
    loop. The only non-adapter left is ``YuyutsavaCompactionMiddleware``, which
    is a ``before_model`` hook, and two policies share that chain — the
    transcript recorder and the prompt inspector, both of which must observe the
    *compacted* message list. Putting the merged adapter last is what keeps that
    true. Nothing else in the stack shares a chain with a policy.

    Every same-chain order is otherwise untouched, because the policies are
    concatenated in their existing stack order. That is the property the
    ``chains`` fingerprint field exists to prove, and it is asserted for all 9
    configurations in ``test/core/test_agent_fingerprint.py``.

    A list with fewer than two adapters is returned unchanged.
    """
    adapters = [m for m in middleware if isinstance(m, LangChainPolicyAdapter)]
    if len(adapters) < 2:
        return list(middleware)

    others = [m for m in middleware if not isinstance(m, LangChainPolicyAdapter)]
    policies = [p for a in adapters for p in a.policies]
    # Every adapter in one stack is built with the same ask port; take the first.
    merged = LangChainPolicyAdapter(policies, ask=adapters[0]._ask)
    return [*others, merged]


def _default_ask() -> AskUser:
    from yuyutsava.policy.ask import LangGraphAskUser

    return LangGraphAskUser()


def _tool_name(tool: Any) -> str:
    """A bound tool's name, whether it is an object or a schema dict."""
    name = getattr(tool, "name", None)
    if isinstance(name, str):
        return name
    if isinstance(tool, dict):
        return str(tool.get("name", ""))
    return ""


def _current_thread_id() -> str:
    """The thread this run belongs to, or ``""`` outside a graph.

    Was ``_current_thread_id`` in ``context/transcript_middleware.py``; the
    transcript recorder, the offload policy and the usage rows all need it, and
    resolving it in the adapter means none of them reaches for the run config.
    """
    try:
        from langgraph.config import get_config

        cfg = get_config() or {}
        return str(cfg.get("configurable", {}).get("thread_id", "") or "")
    except Exception:  # noqa: BLE001 — outside a graph run, or shape changed
        return ""


def _latest_usage(messages: Any) -> Usage | None:
    """Token usage from the most recent AI message, or ``None`` if it reported none.

    One copy of what ``BudgetMiddleware._accumulate`` and ``UsageRecorder._tokens``
    each did separately. ``usage_metadata`` is a dict on some providers and an
    object on others, hence the two-way read.
    """
    from langchain_core.messages import AIMessage

    msg = next((m for m in reversed(list(messages or [])) if isinstance(m, AIMessage)),
               None)
    if msg is None:
        return None
    metadata = getattr(msg, "usage_metadata", None)
    if not metadata:
        return None

    def field(name: str) -> int:
        v = (metadata.get(name) if isinstance(metadata, dict)
             else getattr(metadata, name, 0))
        return int(v or 0)

    return Usage(
        input_tokens=field("input_tokens"),
        output_tokens=field("output_tokens"),
        model=str((getattr(msg, "response_metadata", None) or {}).get("model_name", "")),
    )


def _latest_human_text(messages: Any) -> str:
    """The last message's text if it is a human turn, else ``""``.

    Only ``RetrievalInjectionPolicy`` reads this, and only to decide what to
    retrieve *for*. Resolving it here keeps the message-content flattening — str
    vs. block-list — out of the policy.
    """
    from langchain_core.messages import HumanMessage

    msgs = list(messages or [])
    if not msgs or not isinstance(msgs[-1], HumanMessage):
        return ""
    content = getattr(msgs[-1], "content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            b if isinstance(b, str)
            else b.get("text", "") if isinstance(b, dict) and b.get("type") == "text"
            else ""
            for b in content
        ]
        return " ".join(p for p in parts if p).strip()
    return str(content).strip()


__all__ = ["LangChainPolicyAdapter"]
