"""What each operation runs once the gateway allows it.

``TaskRunnerAgent._execute`` dispatches by operation, and EXECUTE further by
what the tool put in ``additional_context``: a grep, a fetch, a Python script,
or a shell command — sandboxed (the exec backend decides where) or on the host.
Driven through ``handle`` in zones that need no prompt, with a stand-in exec
backend and host shell.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from yuyutsava.agents.task_runner import agent as agent_mod
from yuyutsava.agents.task_runner.agent import TaskRunnerAgent
from yuyutsava.models.operations import OperationRequest, OperationType

RAW = {"stdout": "out", "stderr": "err", "exit_code": 3}


class _Backend:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def run(self, command: str, cwd: Path, timeout: int) -> dict:
        self.calls.append(("run", command, cwd, timeout))
        return RAW

    async def run_python(self, script: Path, cwd: Path, timeout: int) -> dict:
        self.calls.append(("run_python", script, cwd, timeout))
        return RAW


def _req(op: OperationType, path: str, **ctx: Any) -> OperationRequest:
    return OperationRequest(
        request_id="r",
        task_id="t",
        task_description="d",
        operation=op,
        paths=[path],
        reason="x",
        additional_context=ctx or None,
    )


class DispatchCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        self.backend = _Backend()
        self.agent = TaskRunnerAgent(self.ws, exec_backend=self.backend)
        self.sandbox = self.agent.sandbox_root
        self.sandbox.mkdir(parents=True, exist_ok=True)
        self.host_runs: list[tuple] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _handle(self, request: OperationRequest):
        async def fake_run(command, cwd, timeout, *, elevated=False):
            self.host_runs.append((command, cwd, timeout, elevated))
            return RAW

        with (
            mock.patch.object(agent_mod._exec, "execute_run", fake_run),
            mock.patch.object(agent_mod, "interrupt", lambda payload: "approve"),
        ):
            return asyncio.run(asyncio.wait_for(self.agent.handle(request), 20))


class Files(DispatchCase):
    def test_write_append_then_read_a_window(self) -> None:
        f = str(self.ws / "a.txt")
        self.assertEqual(
            self._handle(_req(OperationType.WRITE, f, content="1\n2\n")).status, "success"
        )
        self._handle(_req(OperationType.CREATE, f, content="3\n4\n", append=True))
        res = self._handle(_req(OperationType.READ, f, offset=1, limit=2)).result
        self.assertEqual((res.offset, res.limit, res.returned_lines, res.total_lines), (1, 2, 2, 4))
        self.assertTrue(res.has_more)
        self.assertIsNotNone(res.truncation_notice)
        self.assertIn("2", res.content)

    def test_a_whole_read_has_no_notice(self) -> None:
        f = self.ws / "b.txt"
        f.write_text("only\n")
        res = self._handle(_req(OperationType.READ, str(f))).result
        self.assertFalse(res.has_more)
        self.assertIsNone(res.truncation_notice)
        self.assertIsNone(res.limit)

    def test_list_and_glob(self) -> None:
        for n in ("x.py", "y.py", "z.txt"):
            (self.ws / n).write_text("")
        listed = self._handle(_req(OperationType.LIST, str(self.ws), max_entries=2)).result
        self.assertEqual((listed.returned, listed.pattern, listed.root), (2, None, str(self.ws)))
        self.assertTrue(listed.has_more)
        globbed = self._handle(_req(OperationType.GLOB, str(self.ws), pattern="*.py")).result
        self.assertEqual(globbed.pattern, "*.py")
        self.assertEqual(sorted(e.name for e in globbed.entries), ["x.py", "y.py"])


class Execute(DispatchCase):
    def _shell(self, **ctx: Any):
        resp = self._handle(_req(OperationType.EXECUTE, str(self.sandbox), **ctx))
        self.assertEqual(resp.status, "success", resp.error)
        return resp.result

    def test_a_grep_runs_in_python(self) -> None:
        (self.ws / "g.txt").write_text("alpha\nneedle here\n")
        res = self._shell(search={"pattern": "needle", "path": str(self.ws)})
        self.assertIn("needle here", res.stdout)
        self.assertEqual(self.backend.calls + self.host_runs, [])

    def test_a_python_script_goes_to_the_backend_with_sandbox_cwd(self) -> None:
        res = self._shell(python={"script_path": "s.py"})
        self.assertEqual((res.stdout, res.exit_code), ("out", 3))
        self.assertEqual(self.backend.calls, [("run_python", Path("s.py"), self.sandbox, 120)])

    def test_a_sandboxed_command_goes_to_the_backend(self) -> None:
        self._shell(command="ls", sandboxed=True, timeout=7)
        self.assertEqual(self.backend.calls, [("run", "ls", self.sandbox, 7)])
        self.assertEqual(self.host_runs, [])

    def test_an_elevated_command_runs_on_the_host_even_if_sandboxed(self) -> None:
        self._shell(command="id", sandboxed=True, elevated=True)
        self.assertEqual(self.host_runs, [("id", self.sandbox, 120, True)])
        self.assertEqual(self.backend.calls, [])

    def test_a_plain_command_runs_on_the_host(self) -> None:
        self._shell(command="echo hi", cwd=str(self.ws), timeout="9")
        self.assertEqual(self.host_runs, [("echo hi", self.ws, 9, False)])

    def test_a_fetch(self) -> None:
        calls = []

        async def fake_fetch(url, dest, *, user_agent, timeout):
            calls.append((url, dest, user_agent, timeout))
            return RAW

        with mock.patch.object(agent_mod._exec, "execute_fetch", fake_fetch):
            self._shell(fetch={"url": "https://x", "dest": "d.bin", "user_agent": "ua"})
        self.assertEqual(calls, [("https://x", Path("d.bin"), "ua", 120)])


if __name__ == "__main__":
    unittest.main()
