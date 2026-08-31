#!/usr/bin/env python
"""What the policy layer costs per call. No model, no network, nothing billable.

ADR-004 lists this under risks: *"Adapters add indirection to the hot path —
every model and tool call passes through one more layer. Measure before and
after; the overhead should be negligible, but 'should be' is not a
measurement."*

So this measures it. Three shapes, using the **real** policies from a real CLI
build:

    collapsed    one adapter holding every policy      (what ships)
    per-policy   one adapter per policy, nested        (the pre-4.7 shape)
    bare         the handler alone, no policy layer    (the floor)

What it does *not* claim: this is dispatch cost only. The work inside a policy —
an artifact write, a transcript insert, a retrieval round-trip — dwarfs it and is
unchanged by the migration, because it is the same code.

Run:  .venv/bin/python scripts/benchmark_policy_adapter.py
"""

from __future__ import annotations

import asyncio
import pathlib
import statistics
import sys
import time
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from yuyutsava.policy.adapter import LangChainPolicyAdapter  # noqa: E402

ITERATIONS = 2000


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name


class _ToolRequest:
    def __init__(self) -> None:
        self.tool_call = {"name": "tr_grep", "args": {"pattern": "x"}, "id": "c1"}
        self.tool = _Tool("tr_grep")
        self.state: dict = {}
        self.runtime = None


def _model_request() -> Any:
    from langchain.agents.middleware import ModelRequest
    from langchain_core.messages import HumanMessage, SystemMessage

    return ModelRequest(
        model=None,
        messages=[HumanMessage("do the thing")],
        system_message=SystemMessage("You are YUYUTSAVA."),
        tool_choice=None,
        tools=[_Tool(n) for n in ("tr_grep", "ctx_recall", "tool_search", "task")],
        response_format=None,
        state={},
        runtime=None,
        model_settings={},
    )


def _real_policies() -> list[Any]:
    """The policies a CLI agent actually attaches, built the way the engine builds them."""
    from yuyutsava.core.filesystem_prompt_policy import FilesystemPromptPolicy
    from yuyutsava.core.permission_policy import PermissionPolicy
    from yuyutsava.core.subagent_gate_policy import SubagentGatePolicy
    from yuyutsava.core.tool_filter_policy import ToolFilterPolicy
    from yuyutsava.core.voice_style_policy import VoiceStylePolicy

    return [
        ToolFilterPolicy(),
        FilesystemPromptPolicy(),
        VoiceStylePolicy(),
        SubagentGatePolicy(None),
        PermissionPolicy(pathlib.Path.cwd()),
    ]


async def _time(fn: Any, n: int = ITERATIONS) -> float:
    """Median nanoseconds per call, over *n* calls after a warm-up."""
    for _ in range(50):
        await fn()
    samples: list[float] = []
    for _ in range(n):
        start = time.perf_counter_ns()
        await fn()
        samples.append(time.perf_counter_ns() - start)
    return statistics.median(samples)


async def main() -> int:
    import logging

    # The benchmark prompt has no deepagents filesystem block, so the policy's
    # warn-once fires. That is correct behaviour, and noise here.
    logging.getLogger("yuyutsava.core.filesystem_prompt_policy").setLevel(
        logging.ERROR)

    policies = _real_policies()
    collapsed = LangChainPolicyAdapter(policies)
    per_policy = [LangChainPolicyAdapter([p]) for p in policies]

    async def handler(_req: Any) -> str:
        return "RESULT"

    async def bare_tool() -> Any:
        return await handler(_ToolRequest())

    async def collapsed_tool() -> Any:
        return await collapsed.awrap_tool_call(_ToolRequest(), handler)

    async def nested_tool() -> Any:
        call = handler
        for adapter in reversed(per_policy):
            call = _wrap_tool(adapter, call)
        return await call(_ToolRequest())

    async def bare_model() -> Any:
        return await handler(_model_request())

    async def collapsed_model() -> Any:
        return await collapsed.awrap_model_call(_model_request(), handler)

    async def nested_model() -> Any:
        call = handler
        for adapter in reversed(per_policy):
            call = _wrap_model(adapter, call)
        return await call(_model_request())

    rows = [
        ("tool call", await _time(bare_tool), await _time(collapsed_tool),
         await _time(nested_tool)),
        ("model call", await _time(bare_model), await _time(collapsed_model),
         await _time(nested_model)),
    ]

    print(f"{len(policies)} real policies, median of {ITERATIONS} calls, "
          f"microseconds per call\n")
    print(f"{'':<12}{'bare':>10}{'collapsed':>12}{'per-policy':>12}"
          f"{'collapsed cost':>16}")
    print("-" * 62)
    for label, bare, coll, nested in rows:
        print(f"{label:<12}{bare / 1000:>10.2f}{coll / 1000:>12.2f}"
              f"{nested / 1000:>12.2f}{(coll - bare) / 1000:>15.2f}µs")

    print(
        "\nA model turn is hundreds of milliseconds of network. The layer above "
        "\nis microseconds, and the collapse (4.7) removes the per-policy nesting."
    )
    return 0


def _wrap_tool(adapter: Any, nxt: Any) -> Any:
    async def call(request: Any) -> Any:
        return await adapter.awrap_tool_call(request, nxt)

    return call


def _wrap_model(adapter: Any, nxt: Any) -> Any:
    async def call(request: Any) -> Any:
        return await adapter.awrap_model_call(request, nxt)

    return call


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
