"""``LangChainPolicyAdapter`` reproduces middleware nesting with one class.

Phase 4 step 4.2.

Six tool policies are going to be collapsed into this adapter. Before that, the
adapter has to be shown to preserve the two properties nesting gave for free:

**Order.** With N middlewares, the first is outermost. Its before-hook runs first
and its after-hook runs *last*. That is not cosmetic — the offload policy shrinks
an oversized tool result, so a policy outside it must keep seeing the digest and
one inside it must keep seeing the original. Collapsing the stack means the
adapter reproduces that by hand, and ``AfterHooksRunOutermostLast`` is what
proves it did.

**Short-circuiting.** An outer middleware that returns without calling
``handler`` skips everything inside it: inner before-hooks, the tool, and every
after-hook. A refusal has no result to rewrite.

The rest is the boundary itself: policies get YUYUTSAVA types, never the
framework's request object, and a policy that implements no hook costs nothing.

Run:  .venv/bin/python test/policy/test_adapter.py
"""

from __future__ import annotations

import pathlib
import unittest
from typing import Any

from yuyutsava.policy.adapter import LangChainPolicyAdapter
from yuyutsava.policy.base import Policy
from yuyutsava.policy.types import Denied, Raw, ToolCall


class _Request:
    """The framework's ``ToolCallRequest`` shape, as the adapter reads it."""

    def __init__(self, name: str = "execute", args: dict | None = None,
                 call_id: str = "c1", state: dict | None = None) -> None:
        self.tool_call = {"name": name, "args": args or {}, "id": call_id}
        self.state = state or {}
        self.runtime = None
        self.tool = None


class _Recorder(Policy):
    """Records the order hooks fire in, on a shared log."""

    def __init__(self, name: str, log: list[str]) -> None:
        self.name = name
        super().__init__()
        self._log = log

    async def before_tool(self, call: ToolCall):
        self._log.append(f"before:{self.name}")
        return None

    async def after_tool(self, call: ToolCall, result: Any) -> Any:
        self._log.append(f"after:{self.name}")
        return result


async def _run(adapter: LangChainPolicyAdapter, request: Any = None,
               result: Any = "TOOL-RAN") -> Any:
    ran: list[bool] = []

    async def handler(_req: Any) -> Any:
        ran.append(True)
        return result

    out = await adapter.awrap_tool_call(request or _Request(), handler)
    return out, bool(ran)


class Ordering(unittest.IsolatedAsyncioTestCase):
    async def test_before_hooks_run_in_stack_order(self) -> None:
        log: list[str] = []
        adapter = LangChainPolicyAdapter(
            [_Recorder("a", log), _Recorder("b", log), _Recorder("c", log)])
        await _run(adapter)
        self.assertEqual(log[:3], ["before:a", "before:b", "before:c"])

    async def test_after_hooks_run_outermost_last(self) -> None:
        """Reversed, because the first policy is the outermost wrapper."""
        log: list[str] = []
        adapter = LangChainPolicyAdapter(
            [_Recorder("a", log), _Recorder("b", log), _Recorder("c", log)])
        await _run(adapter)
        self.assertEqual(
            log[3:], ["after:c", "after:b", "after:a"],
            "after-hooks are not reversed; a policy that wrapped another would "
            "start seeing its unmodified result",
        )

    async def test_a_result_rewrite_is_visible_to_the_policy_outside_it(self) -> None:
        """The offload case, in miniature."""
        class Shrink(Policy):
            async def after_tool(self, call: ToolCall, result: Any) -> Any:
                return "SHRUNK"

        seen: list[Any] = []

        class Observe(Policy):
            async def after_tool(self, call: ToolCall, result: Any) -> Any:
                seen.append(result)
                return result

        # Observe is OUTSIDE Shrink, so it must see the shrunk value.
        await _run(LangChainPolicyAdapter([Observe(), Shrink()]))
        self.assertEqual(seen, ["SHRUNK"])

        seen.clear()
        # Reversed: Observe is INSIDE, so it sees the original.
        await _run(LangChainPolicyAdapter([Shrink(), Observe()]))
        self.assertEqual(seen, ["TOOL-RAN"])


class Refusal(unittest.IsolatedAsyncioTestCase):
    async def test_denied_stops_the_tool(self) -> None:
        class No(Policy):
            async def before_tool(self, call: ToolCall):
                return Denied("nope")

        result, ran = await _run(LangChainPolicyAdapter([No()]))
        self.assertFalse(ran, "the tool ran despite a refusal")
        self.assertEqual(result.content, "nope")

    async def test_the_refusal_is_addressed_to_the_call(self) -> None:
        """The model matches a result to its request by ``tool_call_id``."""
        class No(Policy):
            async def before_tool(self, call: ToolCall):
                return Denied("nope")

        request = _Request(name="execute", call_id="call-42")
        result, _ = await _run(LangChainPolicyAdapter([No()]), request)
        self.assertEqual(result.tool_call_id, "call-42")
        self.assertEqual(result.name, "execute")

    async def test_first_refusal_wins_and_skips_the_rest(self) -> None:
        log: list[str] = []

        class No(Policy):
            name = "no"

            async def before_tool(self, call: ToolCall):
                log.append("before:no")
                return Denied("nope")

        adapter = LangChainPolicyAdapter(
            [_Recorder("a", log), No(), _Recorder("c", log)])
        _, ran = await _run(adapter)
        self.assertFalse(ran)
        self.assertEqual(
            log, ["before:a", "before:no"],
            "a policy after the refusal still ran, or an after-hook fired with "
            "no result to rewrite",
        )

    async def test_no_after_hook_runs_on_a_refusal(self) -> None:
        log: list[str] = []

        class No(Policy):
            async def before_tool(self, call: ToolCall):
                return Denied("nope")

        await _run(LangChainPolicyAdapter([_Recorder("a", log), No()]))
        self.assertNotIn("after:a", log)


class EscapeHatch(unittest.IsolatedAsyncioTestCase):
    async def test_raw_is_returned_untouched(self) -> None:
        sentinel = object()

        class Replay(Policy):
            async def before_tool(self, call: ToolCall):
                return Raw(sentinel)

        result, ran = await _run(LangChainPolicyAdapter([Replay()]))
        self.assertIs(result, sentinel)
        self.assertFalse(ran)

    async def test_an_unknown_decision_type_is_loud(self) -> None:
        """Silently ignoring it would let a policy think it refused a call."""
        class Confused(Policy):
            async def before_tool(self, call: ToolCall):
                return "no"  # type: ignore[return-value]

        with self.assertRaises(TypeError):
            await _run(LangChainPolicyAdapter([Confused()]))


class TheBoundary(unittest.IsolatedAsyncioTestCase):
    """Policies see our types. That is the whole exercise."""

    async def test_the_policy_never_receives_the_framework_request(self) -> None:
        seen: list[Any] = []

        class Peek(Policy):
            async def before_tool(self, call: ToolCall):
                seen.append(call)
                return None

        await _run(LangChainPolicyAdapter([Peek()]), _Request(
            name="execute", args={"command": "ls"}, call_id="c9",
            state={"messages": []}))
        (call,) = seen
        self.assertIsInstance(call, ToolCall)
        self.assertEqual(call.name, "execute")
        self.assertEqual(dict(call.args), {"command": "ls"})
        self.assertEqual(call.id, "c9")
        self.assertEqual(dict(call.state), {"messages": []})

    async def test_a_malformed_request_does_not_reach_the_policy(self) -> None:
        """Defensive unwrapping lives in the adapter, once, not in six policies."""
        seen: list[ToolCall] = []

        class Peek(Policy):
            async def before_tool(self, call: ToolCall):
                seen.append(call)
                return None

        class Broken:
            tool_call = None
            state = None

        await _run(LangChainPolicyAdapter([Peek()]), Broken())
        (call,) = seen
        self.assertEqual((call.name, dict(call.args), call.id), ("", {}, ""))

    async def test_args_that_are_not_a_mapping_become_empty(self) -> None:
        seen: list[ToolCall] = []

        class Peek(Policy):
            async def before_tool(self, call: ToolCall):
                seen.append(call)
                return None

        await _run(LangChainPolicyAdapter([Peek()]),
                   _Request(args=["not", "a", "mapping"]))  # type: ignore[arg-type]
        self.assertEqual(dict(seen[0].args), {})

    async def test_the_ask_port_is_handed_to_the_policy(self) -> None:
        from yuyutsava.policy.ask import ScriptedAskUser

        ask = ScriptedAskUser(["approve"])
        seen: list[Any] = []

        class Peek(Policy):
            async def before_tool(self, call: ToolCall):
                seen.append(call.ask)
                return None

        await _run(LangChainPolicyAdapter([Peek()], ask=ask))
        self.assertIs(seen[0], ask)

    def test_the_default_ask_is_the_langgraph_one(self) -> None:
        """Production wiring: no explicit port means interrupt()."""
        from yuyutsava.policy.ask import LangGraphAskUser

        adapter = LangChainPolicyAdapter([])
        self.assertIsInstance(adapter._ask, LangGraphAskUser)


class DeadHooksCostNothing(unittest.IsolatedAsyncioTestCase):
    """Four of the six tool policies never touch a result. Do not call them."""

    def test_a_policy_that_overrides_nothing_is_in_neither_pass(self) -> None:
        adapter = LangChainPolicyAdapter([Policy()])
        self.assertEqual(adapter._before, ())
        self.assertEqual(adapter._after, ())

    def test_overriding_before_only_enrols_it_in_before(self) -> None:
        class OnlyBefore(Policy):
            async def before_tool(self, call: ToolCall):
                return None

        adapter = LangChainPolicyAdapter([OnlyBefore()])
        self.assertEqual(len(adapter._before), 1)
        self.assertEqual(adapter._after, ())

    def test_overriding_after_only_enrols_it_in_after(self) -> None:
        class OnlyAfter(Policy):
            async def after_tool(self, call: ToolCall, result: Any) -> Any:
                return result

        adapter = LangChainPolicyAdapter([OnlyAfter()])
        self.assertEqual(adapter._before, ())
        self.assertEqual(len(adapter._after), 1)

    async def test_a_no_op_policy_changes_nothing(self) -> None:
        result, ran = await _run(LangChainPolicyAdapter([Policy(), Policy()]))
        self.assertTrue(ran)
        self.assertEqual(result, "TOOL-RAN")


class Identity(unittest.TestCase):
    def test_policies_are_reported_in_order(self) -> None:
        """The agent fingerprint reads this; an adapter that hid its policies
        would make the stack less legible than the classes it replaced."""
        log: list[str] = []
        adapter = LangChainPolicyAdapter([_Recorder("a", log), _Recorder("b", log)])
        self.assertEqual([p.name for p in adapter.policies], ["a", "b"])

    def test_name_defaults_to_the_class(self) -> None:
        class SomePolicy(Policy):
            pass

        self.assertEqual(SomePolicy().name, "SomePolicy")

    def test_it_is_the_only_agent_middleware_subclass_in_the_policy_package(self) -> None:
        """The headline of ADR-004 item 1, as a ratchet over ``yuyutsava/policy/``."""
        import ast
        import pathlib

        pkg = pathlib.Path(__file__).resolve().parents[2] / "yuyutsava/policy"
        offenders: list[str] = []
        for path in sorted(pkg.glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.ClassDef):
                    continue
                for base in node.bases:
                    if isinstance(base, ast.Name) and base.id == "AgentMiddleware":
                        offenders.append(f"{path.name}:{node.name}")
        self.assertEqual(
            offenders, ["adapter.py:LangChainPolicyAdapter"],
            f"the policy package must contain exactly one AgentMiddleware "
            f"subclass; found {offenders}",
        )


class TheSyncPathIsLoud(unittest.TestCase):
    """Silently skipping every policy is the one outcome worse than crashing.

    LangChain chooses the sync or async hook by how the graph was driven. An
    adapter with no ``wrap_tool_call`` is simply not consulted on the sync path,
    so every policy it carries — the permission gate included — stops running
    with no error and no log line. Nothing in this codebase drives a graph
    synchronously, so this is unreachable; if that changes, it fails loudly.
    """

    def test_it_raises_rather_than_skipping_policies(self) -> None:
        adapter = LangChainPolicyAdapter([Policy()])
        with self.assertRaises(RuntimeError) as ctx:
            adapter.wrap_tool_call(_Request(), lambda r: "RAN")
        self.assertIn("ainvoke", str(ctx.exception))

    def test_the_error_names_the_policies_that_would_have_been_skipped(self) -> None:
        log: list[str] = []
        adapter = LangChainPolicyAdapter([_Recorder("gatekeeper", log)])
        with self.assertRaises(RuntimeError) as ctx:
            adapter.wrap_tool_call(_Request(), lambda r: "RAN")
        self.assertIn("gatekeeper", str(ctx.exception))

    def test_nothing_drives_a_graph_synchronously(self) -> None:
        """The premise above, checked — across ``test/`` as well as ``yuyutsava/``.

        The first version of this scanned only ``yuyutsava/``, so it passed while
        ``test/test_filesystem_prompt_override.py`` — the Phase 0 tripwire that
        renders the real system prompt — was calling ``bundle.agent.invoke()``.
        The premise was stated about "the codebase" and checked against
        production code only, which is the same mistake as scoping a check to
        the shape you happened to change. The tripwire now drives the graph the
        way production does.
        """
        import re

        here = pathlib.Path(__file__).resolve()
        repo = here.parents[2]
        pattern = re.compile(r"\b(agent|graph|bundle\.agent)\.(invoke|stream)\(")
        offenders = [
            str(path.relative_to(repo))
            for root in (repo / "yuyutsava", repo / "test", repo / "scripts")
            for path in root.rglob("*.py")
            # This file is excluded because it *contains* the pattern above;
            # without that it reports itself and the check is permanently red.
            if "__pycache__" not in path.parts and path.resolve() != here
            and pattern.search(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(
            sorted(offenders), [],
            f"a graph is driven synchronously in {sorted(offenders)}; the "
            f"adapter raises there. Drive it with ainvoke()/astream(), or give "
            f"the adapter a sync bridge before shipping that.",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
