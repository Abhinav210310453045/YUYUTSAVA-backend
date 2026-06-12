"""OrchestratorLoop ↔ ModelRouter (Phase 4): per-task model selection and
the complexity/model columns on the task row.

Run:  uv run python -m unittest test.daemon.test_orchestrator_routing -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from yuyutsava.agents.orchestrator.agent import OrchestratorDeps
from yuyutsava.core.streaming import StreamEvent
from yuyutsava.daemon.channels import ChannelRouter, UserChannel
from yuyutsava.daemon.orchestrator_loop import OrchestratorLoop
from yuyutsava.daemon.task_registry import SqliteTaskStore, TaskRegistry
from yuyutsava.daemon.triage_loop import OrchestratorTask


class _NullChannel(UserChannel):
    name = "null"

    async def post_event(self, ev) -> None: ...

    async def post_proposal(self, p):
        raise NotImplementedError

    async def post_ask(self, a) -> str:
        raise NotImplementedError


class _RecordingStore:
    async def put_decision(self, **kw) -> None: ...


class _NamedModel:
    """Duck-typed chat model with just enough surface for model_name_of."""

    def __init__(self, name: str) -> None:
        self.model_name = name


class _StubRouter:
    """ModelRouter stand-in: fixed tier models, records lookups."""

    def __init__(self, tiers: dict[str, _NamedModel], enabled: bool = True) -> None:
        self.tiers = tiers
        self.enabled = enabled
        self.calls: list[int | None] = []

    def model_for(self, complexity, *, fallback):
        self.calls.append(complexity)
        if not self.enabled:
            return fallback
        c = 3 if complexity is None else complexity
        tier = "light" if c <= 2 else ("standard" if c <= 3 else "heavy")
        return self.tiers[tier]


def _task(task_id: str, complexity: int = 3) -> OrchestratorTask:
    return OrchestratorTask(
        proposal_id="p1", event_id="e1", topic="user.task.submitted",
        summary="sum", instruction="do the thing", subagent_hint="general-purpose",
        urgency=2, task_id=task_id, complexity=complexity,
    )


class OrchestratorRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.registry = TaskRegistry(
            SqliteTaskStore(Path(self._tmp.name) / "state.db")
        )
        self.role_model = _NamedModel("role-orch")
        self.role_sub = _NamedModel("role-sub")
        self.tiers = {
            "light": _NamedModel("tiny:1b"),
            "standard": _NamedModel("mid:8b"),
            "heavy": _NamedModel("big:70b"),
        }
        self.router = _StubRouter(self.tiers)
        self.deps = OrchestratorDeps(
            subagents={},
            subagent_model=self.role_sub,  # type: ignore[arg-type]
            channels=None,  # type: ignore[arg-type]
            store=None,  # type: ignore[arg-type]
            subagent_token_budget=1000,
        )
        self.loop = OrchestratorLoop(
            task_queue=None,
            channels=ChannelRouter(channels=[_NullChannel()]),
            store=_RecordingStore(),
            orchestrator_model=self.role_model,
            deps=self.deps,
            orchestrator_token_budget=1000,
            task_registry=self.registry,
            model_router=self.router,
        )
        self.build_calls: list[dict] = []

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _patch_stream(self) -> None:
        build_calls = self.build_calls

        def fake_build(**kw):
            build_calls.append(kw)
            return object()

        async def fake_stream(graph, message, **kw):
            yield StreamEvent(kind="final", data={"text": "done"})

        for p in (
            mock.patch("yuyutsava.daemon.orchestrator_loop.build_orchestrator", fake_build),
            mock.patch("yuyutsava.daemon.orchestrator_loop.astream_agent_iter", fake_stream),
        ):
            p.start()
            self.addCleanup(p.stop)

    async def _run(self, complexity: int) -> str:
        task_id = self.registry.mint_task_id()
        await self.registry.create(task_id=task_id, origin="api", instruction="x")
        await self.loop._run_task(_task(task_id, complexity=complexity))
        return task_id

    async def test_complexity_one_routes_to_light(self) -> None:
        self._patch_stream()
        task_id = await self._run(complexity=1)

        build = self.build_calls[-1]
        self.assertIs(build["model"], self.tiers["light"])
        # Subagent model rides a per-task deps copy; the booted deps keep
        # the role model.
        self.assertIs(build["deps"].subagent_model, self.tiers["light"])
        self.assertIs(self.deps.subagent_model, self.role_sub)

        rec = await self.registry.get(task_id)
        self.assertEqual(rec.complexity, 1)
        self.assertEqual(rec.model, "tiny:1b")

    async def test_complexity_five_routes_to_heavy(self) -> None:
        self._patch_stream()
        task_id = await self._run(complexity=5)
        self.assertIs(self.build_calls[-1]["model"], self.tiers["heavy"])
        rec = await self.registry.get(task_id)
        self.assertEqual(rec.model, "big:70b")

    async def test_disabled_router_passes_role_models_through(self) -> None:
        self.router.enabled = False
        self._patch_stream()
        task_id = await self._run(complexity=1)
        build = self.build_calls[-1]
        self.assertIs(build["model"], self.role_model)
        self.assertIs(build["deps"], self.deps)  # untouched, no copy
        rec = await self.registry.get(task_id)
        self.assertEqual(rec.complexity, 1)
        self.assertEqual(rec.model, "role-orch")

    async def test_no_router_is_pre_phase4_behaviour(self) -> None:
        loop = OrchestratorLoop(
            task_queue=None,
            channels=ChannelRouter(channels=[_NullChannel()]),
            store=_RecordingStore(),
            orchestrator_model=self.role_model,
            deps=SimpleNamespace(
                skill_registry=None, async_task_mirror=None, memory_store=None,
            ),
            orchestrator_token_budget=1000,
            task_registry=self.registry,
        )
        self._patch_stream()
        task_id = self.registry.mint_task_id()
        await self.registry.create(task_id=task_id, origin="api", instruction="x")
        await loop._run_task(_task(task_id, complexity=5))
        self.assertIs(self.build_calls[-1]["model"], self.role_model)

    async def test_usage_context_carries_join_keys(self) -> None:
        self._patch_stream()
        task_id = await self._run(complexity=3)
        ctx = self.build_calls[-1]["usage_context"]
        self.assertEqual(ctx.task_id, task_id)
        rec = await self.registry.get(task_id)
        self.assertEqual(ctx.thread_id, rec.thread_id)


if __name__ == "__main__":
    unittest.main()
