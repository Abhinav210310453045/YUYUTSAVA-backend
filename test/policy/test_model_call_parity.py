"""The five model-call policies revise requests exactly as their middlewares did.

Phase 4 step 4.6. Written before the cutover and run against both
implementations while both exist.

What is being protected is **the system prompt**, byte for byte. It is a cached
prefix on every provider that does prefix caching, so a stray newline is not
cosmetic — it is a cache miss on every turn. And it is what the model is actually
told, so a dropped block changes behaviour without changing a single test that
does not look at the prompt.

Each case runs one policy and its middleware over the same ``ModelRequest`` and
compares the request that reaches the handler: system-message blocks, tool names,
and nothing else changed.

``NoSystemMessage`` is separate because the middlewares each had a bespoke
``system_message is None`` branch — three of them differed from one another — and
that is where any divergence will be.

## The golden record

The five middlewares were deleted at cutover, so they can no longer be run side
by side. Their behaviour was **captured first** into ``model_call_golden.json``
— every system-message block and tool list they produced, over the matrix below.
Regenerating it is not a way to fix a failure; the code it came from is gone.

Run:  .venv/bin/python test/policy/test_model_call_parity.py
"""

from __future__ import annotations

import json
import pathlib
import unittest
from typing import Any

from langchain.agents.middleware import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from yuyutsava.policy.adapter import LangChainPolicyAdapter

BASE_PROMPT = "You are YUYUTSAVA. Base instructions."
FS_BLOCK = "## Filesystem Tools\n\nUse read_file and write_file to manage files."


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name


def _request(
    *,
    system: Any = BASE_PROMPT,
    tools: list[str] | None = None,
    messages: list[Any] | None = None,
    extra_blocks: list[dict] | None = None,
) -> ModelRequest:
    if system is None:
        system_message = None
    elif extra_blocks is not None:
        system_message = SystemMessage(
            content_blocks=[{"type": "text", "text": system}] + extra_blocks)
    else:
        system_message = SystemMessage(system)
    return ModelRequest(
        model=None,
        messages=messages if messages is not None else [HumanMessage("do the thing")],
        system_message=system_message,
        tool_choice=None,
        tools=[_Tool(n) for n in (tools or [])],
        response_format=None,
        state={},
        runtime=None,
        model_settings={},
    )


def _shape(request: ModelRequest) -> dict[str, Any]:
    """Everything a policy is allowed to change, plus proof it changed nothing else."""
    sm = request.system_message
    return {
        "blocks": list(sm.content_blocks) if sm is not None else None,
        "tools": [t.name for t in (request.tools or [])],
        "messages": [type(m).__name__ for m in (request.messages or [])],
        "tool_choice": request.tool_choice,
        "response_format": request.response_format,
    }


async def _via_policy(policy: Any, request: ModelRequest) -> dict[str, Any]:
    seen: list[ModelRequest] = []

    async def handler(req: ModelRequest) -> str:
        seen.append(req)
        return "RESPONSE"

    await LangChainPolicyAdapter([policy]).awrap_model_call(request, handler)
    return _shape(seen[0])


class _Injector:
    def __init__(self, block: str) -> None:
        self._block = block

    async def build_block(self, task_text: str) -> str:
        return self._block


class _BrokenInjector:
    async def build_block(self, task_text: str) -> str:
        raise RuntimeError("pgvector is down")


GOLDEN = json.loads(
    (pathlib.Path(__file__).resolve().parent / "model_call_golden.json")
    .read_text(encoding="utf-8"))


def _pairs() -> list[tuple[str, Any]]:
    """(label, policy) for every migrated model-call policy."""
    from yuyutsava.core.filesystem_prompt_policy import FilesystemPromptPolicy
    from yuyutsava.core.retrieval_injection_policy import RetrievalInjectionPolicy
    from yuyutsava.core.subagent_gate_policy import SubagentGatePolicy
    from yuyutsava.core.tool_filter_policy import ToolFilterPolicy
    from yuyutsava.core.voice_style_policy import VoiceStylePolicy

    injectors = [_Injector("## MEMORY\nrecalled"), _Injector("## SKILLS\nmatched")]
    return [
        ("tool_filter", ToolFilterPolicy()),
        ("voice_style", VoiceStylePolicy()),
        ("filesystem_drop", FilesystemPromptPolicy()),
        ("filesystem_replace", FilesystemPromptPolicy("Use tr_* tools.")),
        ("subagent_gate", SubagentGatePolicy(_Settings({"researcher"}))),
        ("subagent_gate_none", SubagentGatePolicy(None)),
        ("retrieval", RetrievalInjectionPolicy(list(injectors))),
        ("retrieval_empty", RetrievalInjectionPolicy([])),
        ("retrieval_broken", RetrievalInjectionPolicy([_BrokenInjector()])),
    ]


class _Toggles:
    def __init__(self, disabled: set[str]) -> None:
        self.disabled = frozenset(disabled)


class _Settings:
    """Stands in for RuntimeSettings: a snapshot accessor plus an async refresh."""

    def __init__(self, disabled: set[str]) -> None:
        self._disabled = disabled
        self.refreshed = 0

    def subagents(self) -> _Toggles:
        return _Toggles(self._disabled)

    async def refresh(self) -> None:
        self.refreshed += 1


REQUESTS: list[tuple[str, dict]] = [
    ("plain", {}),
    ("with tools", {"tools": ["tr_grep", "ctx_recall", "execute", "tool_search",
                              "ws_exa_search", "task"]}),
    ("with the filesystem block", {"extra_blocks": [{"type": "text", "text": FS_BLOCK}]}),
    ("non-text block present", {"extra_blocks": [{"type": "image", "url": "x"}]}),
    ("last message is not human", {"messages": [HumanMessage("a"), AIMessage("b")]}),
    ("no messages", {"messages": []}),
]


class Parity(unittest.IsolatedAsyncioTestCase):
    async def test_every_policy_matches_its_middleware(self) -> None:
        for label, policy in _pairs():
            for req_label, kwargs in REQUESTS:
                with self.subTest(policy=label, request=req_label):
                    self.assertEqual(
                        await _via_policy(policy, _request(**kwargs)),
                        GOLDEN[label][req_label],
                        f"{label} / {req_label}: the request reaching the model "
                        f"differs from what the middleware produced",
                    )

    def test_every_combination_has_a_recorded_outcome(self) -> None:
        """Negative control — an unrecorded case asserts nothing."""
        missing = [
            f"{label}/{req_label}"
            for label, _ in _pairs()
            for req_label, _ in REQUESTS
            if req_label not in GOLDEN.get(label, {})
        ]
        self.assertEqual(missing, [])

    async def test_the_matrix_actually_changes_something(self) -> None:
        """Negative control — comparing two no-ops proves nothing."""
        changed = 0
        for label, policy in _pairs():
            for req_label, kwargs in REQUESTS:
                before = _shape(_request(**kwargs))
                after = await _via_policy(policy, _request(**kwargs))
                if before != after:
                    changed += 1
        self.assertGreater(
            changed, 5,
            f"only {changed} of the policy/request combinations changed the "
            f"request at all; the parity check above is mostly vacuous",
        )


class NoSystemMessage(unittest.IsolatedAsyncioTestCase):
    """Each middleware had its own ``system_message is None`` branch.

    ``VoiceStyleMiddleware`` used ``addendum.lstrip("\\n")``,
    ``SubagentGateMiddleware`` used the addendum as-is, and
    ``RetrievalInjectionMiddleware`` omitted the ``"\\n\\n"`` separator it adds in
    every other case — three different answers to the same question, none of them
    written down as a decision.

    deepagents always supplies a system prompt, so none of these branches runs in
    production. They are asserted anyway: if one of them *does* diverge, this
    says so explicitly rather than leaving it to be discovered later.
    """

    async def test_each_policy_against_its_middleware(self) -> None:
        for label, policy in _pairs():
            with self.subTest(policy=label):
                self.assertEqual(
                    await _via_policy(policy, _request(system=None)),
                    GOLDEN[label]["__no_system_message__"],
                    f"{label}: diverges when there is no system message. The "
                    f"middleware's bespoke None-branch is not reproduced.",
                )


class TheWholePoint(unittest.IsolatedAsyncioTestCase):
    """Policies decide; the adapter does the framework plumbing.

    No ``ModelRequest``, no ``SystemMessage`` — a ``ModelCall`` is three fields.
    """

    async def test_tool_filter_suppresses_by_prefix_and_name(self) -> None:
        from yuyutsava.core.tool_filter_policy import ToolFilterPolicy
        from yuyutsava.policy.types import ModelCall

        call = ModelCall(tool_names=("tr_grep", "ctx_recall", "execute", "tool_search"))
        await ToolFilterPolicy().revise_model_call(call)
        self.assertEqual(call.suppressed_tools, {"tr_grep", "execute"})

    async def test_the_filesystem_block_is_found_by_index(self) -> None:
        from yuyutsava.core.filesystem_prompt_policy import FilesystemPromptPolicy
        from yuyutsava.policy.types import ModelCall

        call = ModelCall(system_texts=("base", FS_BLOCK, "tail"))
        await FilesystemPromptPolicy().revise_model_call(call)
        self.assertEqual(call.rewritten, {1: None}, "wrong block targeted")

    async def test_a_replacement_swaps_rather_than_drops(self) -> None:
        from yuyutsava.core.filesystem_prompt_policy import FilesystemPromptPolicy
        from yuyutsava.policy.types import ModelCall

        call = ModelCall(system_texts=("base", FS_BLOCK))
        await FilesystemPromptPolicy("use tr_*").revise_model_call(call)
        self.assertEqual(call.rewritten, {1: "use tr_*"})

    async def test_no_filesystem_block_warns_exactly_once(self) -> None:
        """The Phase 0 silent-failure seam, without a graph."""
        from yuyutsava.core.filesystem_prompt_policy import FilesystemPromptPolicy
        from yuyutsava.policy.types import ModelCall

        policy = FilesystemPromptPolicy()
        with self.assertLogs(
                "yuyutsava.core.filesystem_prompt_policy", level="WARNING") as cm:
            await policy.revise_model_call(ModelCall(system_texts=("base",)))
            await policy.revise_model_call(ModelCall(system_texts=("base",)))
        self.assertEqual(len(cm.output), 1, "the warning repeats every turn")

    async def test_a_healthy_run_is_silent(self) -> None:
        import logging

        from yuyutsava.core.filesystem_prompt_policy import FilesystemPromptPolicy
        from yuyutsava.policy.types import ModelCall

        policy = FilesystemPromptPolicy()
        logger = logging.getLogger("yuyutsava.core.filesystem_prompt_policy")
        with self.assertNoLogs(logger, level="WARNING"):
            await policy.revise_model_call(ModelCall(system_texts=("base", FS_BLOCK)))

    async def test_a_broken_injector_is_skipped_not_fatal(self) -> None:
        from yuyutsava.core.retrieval_injection_policy import RetrievalInjectionPolicy
        from yuyutsava.policy.types import ModelCall

        policy = RetrievalInjectionPolicy([_BrokenInjector(), _Injector("## OK\nx")])
        call = ModelCall(latest_human_text="do the thing")
        await policy.revise_model_call(call)
        self.assertEqual(len(call.appended), 1)
        self.assertIn("## OK", call.appended[0])

    async def test_retrieval_does_nothing_without_a_human_turn(self) -> None:
        from yuyutsava.core.retrieval_injection_policy import RetrievalInjectionPolicy
        from yuyutsava.policy.types import ModelCall

        call = ModelCall(latest_human_text="")
        await RetrievalInjectionPolicy([_Injector("## MEM\nx")]).revise_model_call(call)
        self.assertFalse(call.changed)

    async def test_the_gate_says_which_subagents_are_off(self) -> None:
        from yuyutsava.core.subagent_gate_policy import SubagentGatePolicy
        from yuyutsava.policy.types import ModelCall

        call = ModelCall(system_texts=("base",))
        await SubagentGatePolicy(_Settings({"researcher", "coder"})).revise_model_call(call)
        self.assertEqual(len(call.appended), 1)
        self.assertIn("coder, researcher", call.appended[0])

    async def test_the_gate_is_quiet_when_nothing_is_off(self) -> None:
        from yuyutsava.core.subagent_gate_policy import SubagentGatePolicy
        from yuyutsava.policy.types import ModelCall

        call = ModelCall(system_texts=("base",))
        await SubagentGatePolicy(_Settings(set())).revise_model_call(call)
        self.assertFalse(call.changed)


class NonTextBlocksSurvive(unittest.IsolatedAsyncioTestCase):
    """Flattening the prompt to ``list[str]`` would drop them silently."""

    async def test_an_image_block_is_carried_across(self) -> None:
        from yuyutsava.core.voice_style_policy import VoiceStylePolicy

        image = {"type": "image", "url": "data:image/png;base64,AAA"}
        request = _request(extra_blocks=[image])

        class _AlwaysVoice(VoiceStylePolicy):
            async def revise_model_call(self, call):
                call.append_system_text("SPOKEN STYLE")

        after = await _via_policy(_AlwaysVoice(), request)
        self.assertIn(
            image, after["blocks"],
            "a non-text system block was lost when the prompt was rebuilt",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
