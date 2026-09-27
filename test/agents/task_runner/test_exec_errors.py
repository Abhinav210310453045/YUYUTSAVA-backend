"""A command or script that overruns its limit is one line and a usable message.

The executor documents a timeout as an expected outcome — "Raises
``asyncio.TimeoutError`` if the script exceeds *timeout* seconds" — and the
task runner returns it to the agent as an error. The R-ERR-01 sweep gave every
exec error a traceback, which made a user script that simply ran long print a
40-line stack at the end of a turn (found live), and the agent was told only
``"TimeoutError: "``. Anything else that escapes an operation is unexpected and
keeps its traceback.

Run:  .venv/bin/python -m pytest test/agents/task_runner/test_exec_errors.py
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yuyutsava.agents.task_runner import agent as agent_mod
from yuyutsava.agents.task_runner.agent import TaskRunnerAgent
from yuyutsava.models.operations import OperationRequest, OperationType


class _Backend:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def run(self, command: str, cwd: Path, timeout: int) -> dict:
        raise self._exc

    async def run_python(self, script: Path, cwd: Path, timeout: int) -> dict:
        raise self._exc


def _python(script: str, where: Path) -> OperationRequest:
    return OperationRequest(
        request_id="r",
        task_id="t",
        task_description="d",
        operation=OperationType.EXECUTE,
        paths=[str(where)],
        reason="x",
        additional_context={"python": {"script_path": script}},
    )


class ExecErrors(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _handle(self, exc: BaseException):
        runner = TaskRunnerAgent(self.ws, exec_backend=_Backend(exc))
        runner.sandbox_root.mkdir(parents=True, exist_ok=True)
        with mock.patch.object(agent_mod, "interrupt", lambda payload: "approve"):
            return asyncio.run(
                asyncio.wait_for(runner.handle(_python("s.py", runner.sandbox_root)), 20)
            )

    def test_a_timeout_is_one_line_and_says_what_happened(self) -> None:
        with self.assertLogs("yuyutsava.task_runner", "WARNING") as logs:
            res = self._handle(TimeoutError())
        self.assertEqual(res.status, "error")
        self.assertIn("time limit", res.error)
        timeouts = [r for r in logs.records if "EXEC TIMEOUT" in r.getMessage()]
        self.assertEqual(len(timeouts), 1)
        self.assertIsNone(timeouts[0].exc_info, "a timeout printed a traceback")

    def test_anything_else_keeps_its_traceback(self) -> None:
        with self.assertLogs("yuyutsava.task_runner", "ERROR") as logs:
            res = self._handle(RuntimeError("boom"))
        self.assertEqual(res.status, "error")
        self.assertIn("RuntimeError: boom", res.error)
        self.assertIsNotNone(logs.records[0].exc_info)


if __name__ == "__main__":
    unittest.main()
