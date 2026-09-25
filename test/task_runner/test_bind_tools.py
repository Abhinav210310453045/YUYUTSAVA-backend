"""The tr_* tools act through the gateway they are bound to, and no other.

`R-GLB-01`. The tools used to be bound to a per-workspace agent cached in the
module, whose policy and consent were process globals — and every chat stack
the daemon built reinstalled a fresh, store-less consent registry on all of
them. From the first chat on, "allow for this project" grants were no longer
persisted and the ones loaded at boot were no longer consulted, for the chat
and for the orchestrator's subagents alike. Now each stack builds its gateway
with the policy and consent it was given, and binding one agent's tools cannot
touch another's.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from yuyutsava.agents.task_runner.agent import TaskRunnerAgent
from yuyutsava.agents.task_runner.tools import bind_tools
from yuyutsava.consent.registry import ConsentRegistry
from yuyutsava.core.agent_stack import _stack_consent
from yuyutsava.core.deps import StackWiring
from yuyutsava.models.operations import OperationResponse
from yuyutsava.storage.paths import WorkspaceLayout


class _Recording(TaskRunnerAgent):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.requests: list = []

    async def handle(self, request):
        self.requests.append(request)
        return OperationResponse(
            request_id=request.request_id,
            operation_id="op",
            status="success",
            operation=request.operation.value,
        )


def _tool(tools, name: str):
    return next(t for t in tools if t.name == name)


class ToolsUseTheirOwnGateway(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_call_goes_through_the_bound_agent(self) -> None:
        mine, other = _Recording(self.ws), _Recording(self.ws)
        tools = bind_tools(mine, agent_name="gp")
        out = asyncio.run(_tool(tools, "tr_ls").ainvoke({"path": str(self.ws), "reason": "look"}))
        self.assertEqual(json.loads(out)["status"], "success")
        self.assertEqual(len(mine.requests), 1)
        self.assertEqual(mine.requests[0].requesting_agent, "gp")
        self.assertEqual(other.requests, [])

    def test_binding_one_agent_leaves_another_agents_consent_alone(self) -> None:
        daemon_consent, chat_consent = ConsentRegistry(), ConsentRegistry()
        orchestrator_side = TaskRunnerAgent(self.ws, consent=daemon_consent)
        bind_tools(orchestrator_side)
        bind_tools(TaskRunnerAgent(self.ws, consent=chat_consent))
        self.assertIs(orchestrator_side._consent, daemon_consent)

    def test_the_exec_backend_is_installed_on_the_bound_agent(self) -> None:
        agent, backend = TaskRunnerAgent(self.ws), object()
        bind_tools(agent, exec_backend=backend)
        self.assertIs(agent.exec_backend, backend)

    def test_the_default_sandbox_is_the_layouts(self) -> None:
        self.assertEqual(
            TaskRunnerAgent(self.ws).sandbox_root, WorkspaceLayout.for_workspace(self.ws).sandbox
        )


class AStackUsesTheDaemonsConsent(unittest.TestCase):
    def test_under_the_daemon_it_is_the_daemons_registry(self) -> None:
        daemon_consent = ConsentRegistry()
        self.assertIs(_stack_consent(StackWiring(consent=daemon_consent)), daemon_consent)

    def test_standalone_it_is_a_registry_of_its_own(self) -> None:
        self.assertIsInstance(_stack_consent(StackWiring()), ConsentRegistry)


if __name__ == "__main__":
    unittest.main()
