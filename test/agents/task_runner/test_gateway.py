"""The TaskRunner gateway: what ``handle`` decides for each request, and why.

Every ``tr_*`` operation passes through ``TaskRunnerAgent.handle`` — the zone
table, the policy file's auto-approve, a remembered consent grant, and finally
the user. Nothing drove it end to end, so these pin each path: which requests
are refused without asking, which ask, what an answer does, and which ones a
standing grant or policy lets through. ``interrupt`` is a stand-in that
records the question and returns a scripted answer.
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


def _req(op: OperationType, *paths: str, **extra: Any) -> OperationRequest:
    return OperationRequest(
        request_id="r1",
        task_id="t1",
        task_description="test",
        operation=op,
        paths=list(paths),
        reason="because",
        **extra,
    )


class _Policy:
    def __init__(self, table: dict[str, str]) -> None:
        self.table = table

    def policy_for(self, tool_name: str) -> str:
        return self.table.get(tool_name, "propose")


class _Consent:
    def __init__(self, verdict: str = "prompt") -> None:
        self.verdict = verdict
        self.grants: list[dict] = []

    def check_tool_permission(self, **kw: Any) -> str:
        return self.verdict

    async def grant_tool_permission(self, **kw: Any) -> None:
        self.grants.append(kw)


class GatewayCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._outside = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        self.outside = Path(self._outside.name).resolve()
        self.asked: list[dict] = []
        self.answer = "approve"

    def tearDown(self) -> None:
        self._tmp.cleanup()
        self._outside.cleanup()

    def _handle(self, request: OperationRequest, **agent_kw: Any):
        agent = TaskRunnerAgent(self.ws, **agent_kw)

        def fake_interrupt(payload: dict) -> str:
            self.asked.append(payload)
            return self.answer

        with mock.patch.object(agent_mod, "interrupt", fake_interrupt):
            return asyncio.run(asyncio.wait_for(agent.handle(request), 20))

    def _file(self, name: str = "f.txt", where: Path | None = None) -> Path:
        path = (where or self.ws) / name
        path.write_text("hello")
        return path


class RefusedWithoutAsking(GatewayCase):
    def test_no_paths(self) -> None:
        resp = self._handle(_req(OperationType.READ))
        self.assertEqual((resp.status, resp.error_code), ("denied", "TR004"))
        self.assertEqual(self.asked, [])

    def test_a_system_critical_path(self) -> None:
        resp = self._handle(_req(OperationType.READ, "/etc/passwd"))
        self.assertEqual((resp.status, resp.error_code), ("denied", "TR002"))
        self.assertIn("system-critical", resp.error)
        self.assertEqual(self.asked, [])

    def test_an_operation_the_zone_forbids(self) -> None:
        # Execute in the workspace is DENY in the rule table.
        resp = self._handle(_req(OperationType.EXECUTE, str(self.ws / "x.sh")))
        self.assertEqual((resp.status, resp.error_code), ("denied", "TR004"))
        self.assertIn("not permitted in the 'workspace' zone", resp.error)
        self.assertEqual(self.asked, [])

    def test_the_policy_file_cannot_auto_approve_a_forbidden_operation(self) -> None:
        resp = self._handle(
            _req(OperationType.EXECUTE, str(self.ws / "x.sh")),
            policy=_Policy({"tr_execute": "auto_approve"}),
        )
        self.assertEqual(resp.status, "denied")


class AllowedWithoutAsking(GatewayCase):
    def test_a_workspace_read(self) -> None:
        f = self._file()
        resp = self._handle(_req(OperationType.READ, str(f)))
        self.assertEqual((resp.status, resp.zone, resp.operation), ("success", "workspace", "read"))
        self.assertEqual(self.asked, [])

    def test_an_execution_error_is_an_error_response_not_a_raise(self) -> None:
        resp = self._handle(_req(OperationType.READ, str(self.ws / "missing.txt")))
        self.assertEqual((resp.status, resp.error_code), ("error", "TR005"))


class AskedFirst(GatewayCase):
    def test_a_workspace_delete_asks_and_runs_on_approve(self) -> None:
        f = self._file()
        resp = self._handle(_req(OperationType.DELETE, str(f)))
        self.assertEqual(resp.status, "success")
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(self.asked[0]["type"], "task_runner_permission")
        self.assertFalse(f.exists())

    def test_a_rejection_leaves_the_file_and_says_so(self) -> None:
        f = self._file()
        self.answer = "reject"
        resp = self._handle(_req(OperationType.DELETE, str(f)))
        self.assertEqual((resp.status, resp.error_code), ("denied", "TR003"))
        self.assertIn("User denied permission for DELETE", resp.error)
        self.assertEqual(resp.alternatives, [])  # none offered for a workspace delete
        self.assertTrue(f.exists())

    def test_an_external_read_asks(self) -> None:
        f = self._file(where=self.outside)
        resp = self._handle(_req(OperationType.READ, str(f)))
        self.assertEqual((resp.status, resp.zone), ("success", "external"))
        self.assertEqual(len(self.asked), 1)


class PolicyAndConsent(GatewayCase):
    def test_policy_auto_approve_skips_the_question(self) -> None:
        f = self._file()
        resp = self._handle(
            _req(OperationType.DELETE, str(f)),
            policy=_Policy({"tr_delete_file": "auto_approve"}),
        )
        self.assertEqual(resp.status, "success")
        self.assertEqual(self.asked, [])

    def test_an_elevated_request_asks_even_when_the_policy_would_not(self) -> None:
        f = self._file()
        consent = _Consent("allow")
        resp = self._handle(
            _req(OperationType.DELETE, str(f), additional_context={"elevated": True}),
            policy=_Policy({"tr_delete_file": "auto_approve"}),
            consent=consent,
        )
        self.assertEqual(resp.status, "success")
        self.assertEqual(len(self.asked), 1)

    def test_a_standing_grant_skips_the_question(self) -> None:
        f = self._file()
        resp = self._handle(_req(OperationType.DELETE, str(f)), consent=_Consent("allow"))
        self.assertEqual(resp.status, "success")
        self.assertEqual(self.asked, [])

    def test_a_standing_denial_refuses_without_asking(self) -> None:
        f = self._file()
        resp = self._handle(_req(OperationType.DELETE, str(f)), consent=_Consent("deny"))
        self.assertEqual((resp.status, resp.error_code), ("denied", "TR003"))
        self.assertIn("standing rule denies DELETE", resp.error)
        self.assertEqual(self.asked, [])
        self.assertTrue(f.exists())

    def test_approving_for_the_session_records_a_grant_at_the_workspace_root(self) -> None:
        f = self._file()
        consent = _Consent("prompt")
        self.answer = "approve_session"
        resp = self._handle(_req(OperationType.DELETE, str(f)), consent=consent)
        self.assertEqual(resp.status, "success")
        (grant,) = consent.grants
        self.assertEqual(
            (grant["operation"], grant["zone"], grant["scope"], grant["directory"]),
            ("delete", "workspace", "session", str(self.ws)),
        )

    def test_approving_once_records_nothing(self) -> None:
        consent = _Consent("prompt")
        self._handle(_req(OperationType.DELETE, str(self._file())), consent=consent)
        self.assertEqual(consent.grants, [])

    def test_an_elevated_approval_is_never_remembered(self) -> None:
        consent = _Consent("prompt")
        self.answer = "approve_project"
        self._handle(
            _req(OperationType.DELETE, str(self._file()), additional_context={"elevated": True}),
            consent=consent,
        )
        self.assertEqual(consent.grants, [])


if __name__ == "__main__":
    unittest.main()
