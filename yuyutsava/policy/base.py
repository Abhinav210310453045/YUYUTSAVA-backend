"""The ``Policy`` contract — a YUYUTSAVA cross-cutting concern, in our own types.

Phase 4 step 4.2, [ADR-004](../../docs/architecture-review/adr/ADR-004-framework-boundary.md)
item 1, addressing [`F-T01`](../../docs/architecture-review/04-findings-thirdparty-coupling.md#f-t01).

Fourteen classes implementing this system's own policies — permissions, budget
ceilings, cost accounting, tool filtering, transcript persistence, skill
retrieval — subclass ``langchain.agents.middleware.AgentMiddleware`` directly.
None of those is a LangChain concern. All of them are LangChain subclasses, and
the practical consequence is that **testing one means constructing framework
objects**, which is why this project avoids those tests and why the policy layer
is effectively uncovered.

A :class:`Policy` is a plain object. It is constructed, called, and asserted on
without a graph, a model, or an import of any framework.

## Why a base class and not only a Protocol

Both. :class:`Policy` is the ABC that supplies do-nothing defaults, so a policy
implements only the hook it cares about — four of the six tool policies decide
before the call and never touch the result. The adapter accepts anything
*structurally* matching, so a policy need not inherit; inheriting just saves
writing the no-ops.

The adapter also asks :meth:`handles_before` / :meth:`handles_after` so an
unimplemented hook costs nothing per tool call. Overriding a hook is what
enables it — there is no separate registration to forget.

## Scope of this step

Tool-call hooks only, deliberately. Six of the fourteen policies are tool
policies, they include the hardest one (``PermissionMiddleware``, the only class
in the codebase carrying ``# type: ignore[misc]`` because the framework's base
contract does not fit it), and ADR-004's own risk table says to migrate that one
first: *"if the adapter handles that one, it handles the rest"*. Model-call hooks
follow once this shape is proven against live traffic.
"""

from __future__ import annotations

from typing import Any

from yuyutsava.policy.types import Directive, ModelCall, ToolCall, ToolDecision, Turn


class Policy:
    """One cross-cutting concern. Override the hooks it needs; ignore the rest."""

    #: Identifies the policy in logs, in the adapter's ordering, and in the agent
    #: fingerprint. Defaults to the class name, which is what the fourteen
    #: framework subclasses were identified by before this existed.
    name: str = ""

    def __init__(self) -> None:
        if not self.name:
            self.name = type(self).__name__

    async def before_tool(self, call: ToolCall) -> ToolDecision:
        """Decide whether *call* may run.

        Return ``None`` to allow it — the default, and what a policy that only
        cares about other tools should return for everything else. Return
        :class:`~yuyutsava.policy.types.Denied` to refuse it, or
        :class:`~yuyutsava.policy.types.Raw` to substitute a framework-native
        result.
        """
        return None

    async def after_tool(self, call: ToolCall, result: Any) -> Any:
        """Rewrite the tool's *result*, or return it unchanged.

        Runs only if the call was allowed. *result* is the framework's own value
        (a ``ToolMessage``, a ``Command``, …) — ADR-004 draws the boundary around
        our decisions, not around their message types, so a policy that rewrites
        results still speaks the framework's result vocabulary here.
        """
        return result

    async def revise_model_call(self, call: ModelCall) -> None:
        """Change what goes to the model: the system prompt, the bound tools.

        Edits are recorded on *call* (``append_system_text``,
        ``rewrite_system_block``, ``suppress_tools``) and applied by the adapter.
        Return nothing.

        There is no way to refuse a model call, deliberately: none of the five
        policies that revise one has ever wanted to, and a hook that *could*
        would have to hand back a model response it has no way to construct.
        """
        return None

    # -- observing the conversation ------------------------------------------

    async def before_model(self, turn: Turn) -> Directive | None:
        """Runs before each model call, after the request has been assembled."""
        return None

    async def after_model(self, turn: Turn) -> Directive | None:
        """Runs after each model call, with the model's reply in ``turn.messages``."""
        return None

    async def after_agent(self, turn: Turn) -> Directive | None:
        """Runs once when the agent finishes, before control returns to the caller."""
        return None

    # -- capability probes, used by the adapter to skip dead hooks ------------

    def handles_before(self) -> bool:
        return type(self).before_tool is not Policy.before_tool

    def handles_after(self) -> bool:
        return type(self).after_tool is not Policy.after_tool

    def handles_model_call(self) -> bool:
        return type(self).revise_model_call is not Policy.revise_model_call

    def observes(self, phase: str) -> bool:
        """Whether this policy overrides the ``before_model``/… hook for *phase*."""
        hook = {"before_model": "before_model", "after_model": "after_model",
                "after_agent": "after_agent"}[phase]
        return getattr(type(self), hook) is not getattr(Policy, hook)

    def __repr__(self) -> str:
        return f"<Policy {self.name}>"


__all__ = ["Policy"]
