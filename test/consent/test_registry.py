"""Unit tests for the reusable consent / allowlist core (Part B1).

No DB required for the in-memory cases; a small fake ConsentStore covers
persistence + load-on-init. Runnable as a script or via pytest.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from yuyutsava.consent import (
    ConsentRegistry,
    ConsentScope,
    Grant,
    Verdict,
    is_permission_ask,
    parse_consent_decision,
)
from yuyutsava.consent.domains import ToolPermissionDomain
from yuyutsava.daemon.interrupt_format import options_for_interrupt

FOLDER = "/Users/x/Desktop/yuyu-test-organize"


class _FakeStore:
    """In-memory ConsentStore stand-in."""

    def __init__(self, seed: list[Grant] | None = None) -> None:
        self.grants: list[Grant] = list(seed or [])

    async def put_consent_grant(self, grant: Grant) -> None:
        self.grants.append(grant)

    async def delete_consent_grant(self, grant_id: str) -> None:
        self.grants = [g for g in self.grants if g.grant_id != grant_id]

    def list_consent_grants(self) -> list[Grant]:
        return list(self.grants)


# ---------------------------------------------------------------------------
# Domain
# ---------------------------------------------------------------------------


class ToolPermissionDomainTests(unittest.TestCase):
    def test_directory_of_single_file_uses_parent(self):
        d = ToolPermissionDomain()
        self.assertEqual(d.directory_of([f"{FOLDER}/a.txt"]), FOLDER)

    def test_directory_of_multiple_uses_common_parent(self):
        d = ToolPermissionDomain()
        self.assertEqual(
            d.directory_of([f"{FOLDER}/Docs/a.txt", f"{FOLDER}/Img/b.png"]), FOLDER
        )

    def test_subject_key_shape(self):
        d = ToolPermissionDomain()
        key = d.subject_key({"operation": "LIST", "zone": "EXTERNAL", "paths": [FOLDER]})
        self.assertEqual(key, f"list|external|{FOLDER}")


# ---------------------------------------------------------------------------
# Registry semantics
# ---------------------------------------------------------------------------


class RegistryTests(unittest.IsolatedAsyncioTestCase):
    def _check(self, reg, op, paths, session="S1", ws="/ws"):
        return reg.check_tool_permission(
            operation=op, zone="external", paths=paths, session_id=session, workspace=ws
        )

    async def test_prompt_then_session_grant_allows_subpaths(self):
        reg = ConsentRegistry()
        self.assertEqual(self._check(reg, "list", [FOLDER]), Verdict.PROMPT.value)
        await reg.grant_tool_permission(
            operation="list", zone="external", paths=[FOLDER],
            scope=ConsentScope.SESSION.value, session_id="S1", workspace="/ws",
        )
        # subpath in same session → allow
        self.assertEqual(self._check(reg, "list", [f"{FOLDER}/Documents"]), Verdict.ALLOW.value)

    async def test_session_isolation(self):
        reg = ConsentRegistry()
        await reg.grant_tool_permission(
            operation="list", zone="external", paths=[FOLDER],
            scope=ConsentScope.SESSION.value, session_id="S1", workspace="/ws",
        )
        self.assertEqual(self._check(reg, "list", [FOLDER], session="S2"), Verdict.PROMPT.value)

    async def test_operation_isolation(self):
        reg = ConsentRegistry()
        await reg.grant_tool_permission(
            operation="list", zone="external", paths=[FOLDER],
            scope=ConsentScope.SESSION.value, session_id="S1", workspace="/ws",
        )
        self.assertEqual(self._check(reg, "write", [f"{FOLDER}/a.txt"]), Verdict.PROMPT.value)

    async def test_directory_isolation(self):
        reg = ConsentRegistry()
        await reg.grant_tool_permission(
            operation="list", zone="external", paths=[FOLDER],
            scope=ConsentScope.SESSION.value, session_id="S1", workspace="/ws",
        )
        self.assertEqual(self._check(reg, "list", ["/etc"]), Verdict.PROMPT.value)

    async def test_project_grant_persists_and_reloads(self):
        store = _FakeStore()
        reg = ConsentRegistry(store=store)
        await reg.grant_tool_permission(
            operation="list", zone="external", paths=[FOLDER],
            scope=ConsentScope.PROJECT.value, session_id="S1", workspace="/ws",
        )
        self.assertEqual(len(store.grants), 1)  # persisted
        # a fresh registry over the same store loads it; different session still hits
        reg2 = ConsentRegistry(store=store)
        v = reg2.check_tool_permission(
            operation="list", zone="external", paths=[f"{FOLDER}/Documents"],
            session_id="OTHER", workspace="/ws",
        )
        self.assertEqual(v, Verdict.ALLOW.value)

    async def test_workspace_wide_grant_covers_sibling_subfolders(self):
        # Mirrors TaskRunnerAgent._record_consent_grant widening an in-workspace
        # session grant to the workspace root: one DELETE approval then covers
        # every subfolder (Archives / Audio / Data / …) with no re-asks.
        reg = ConsentRegistry()
        await reg.grant_tool_permission(
            operation="delete", zone="workspace",
            paths=[f"{FOLDER}/Archives/sample.zip"],
            scope=ConsentScope.SESSION.value, session_id="S1", workspace=FOLDER,
            directory=FOLDER,
        )
        for sub in ("Archives/sample.zip", "Audio/sample.mp3", "Data/sample.csv"):
            v = reg.check_tool_permission(
                operation="delete", zone="workspace", paths=[f"{FOLDER}/{sub}"],
                session_id="S1", workspace=FOLDER,
            )
            self.assertEqual(v, Verdict.ALLOW.value, sub)

    async def test_unwidened_grant_does_not_cover_siblings(self):
        # Without the directory override the grant is keyed to the file's parent
        # folder only — the original behaviour, kept for the `once` path.
        reg = ConsentRegistry()
        await reg.grant_tool_permission(
            operation="delete", zone="workspace",
            paths=[f"{FOLDER}/Archives/sample.zip"],
            scope=ConsentScope.SESSION.value, session_id="S1", workspace=FOLDER,
        )
        same = reg.check_tool_permission(
            operation="delete", zone="workspace", paths=[f"{FOLDER}/Archives/other.zip"],
            session_id="S1", workspace=FOLDER,
        )
        self.assertEqual(same, Verdict.ALLOW.value)
        sibling = reg.check_tool_permission(
            operation="delete", zone="workspace", paths=[f"{FOLDER}/Audio/x.mp3"],
            session_id="S1", workspace=FOLDER,
        )
        self.assertEqual(sibling, Verdict.PROMPT.value)

    async def test_session_grant_not_persisted(self):
        store = _FakeStore()
        reg = ConsentRegistry(store=store)
        await reg.grant_tool_permission(
            operation="list", zone="external", paths=[FOLDER],
            scope=ConsentScope.SESSION.value, session_id="S1", workspace="/ws",
        )
        self.assertEqual(store.grants, [])  # session never touches the store

    async def test_expired_grant_ignored(self):
        store = _FakeStore(seed=[Grant(
            grant_id="g1", domain="tool_permission",
            subject_key=f"list|external|{FOLDER}", decision="allow",
            scope="project", scope_ref="/ws", created_ts=time.time() - 100,
            expires_ts=time.time() - 1,
        )])
        reg = ConsentRegistry(store=store)
        self.assertEqual(self._check(reg, "list", [FOLDER]), Verdict.PROMPT.value)


# ---------------------------------------------------------------------------
# Decision parsing + ask options
# ---------------------------------------------------------------------------


class DecisionParsingTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_consent_decision("y"), (True, None))
        self.assertEqual(parse_consent_decision("approve"), (True, None))
        self.assertEqual(parse_consent_decision("s"), (True, ConsentScope.SESSION.value))
        self.assertEqual(parse_consent_decision("session"), (True, ConsentScope.SESSION.value))
        self.assertEqual(parse_consent_decision("project"), (True, ConsentScope.PROJECT.value))
        self.assertEqual(parse_consent_decision("N"), (False, None))
        self.assertEqual(parse_consent_decision(""), (False, None))

    def test_is_permission_ask(self):
        self.assertTrue(is_permission_ask(["approve", "session", "project", "reject"]))
        self.assertTrue(is_permission_ask(["approve", "reject"]))
        self.assertFalse(is_permission_ask(["yes", "no"]))

    def test_options_scope_for_low_risk(self):
        opts = options_for_interrupt({"type": "task_runner_permission", "risk_level": "LOW"})
        self.assertEqual(opts, ["approve", "session", "project", "reject"])

    def test_options_scope_offered_for_all_risk_levels(self):
        # Every op type is allowlistable (incl. HIGH/CRITICAL like bash), matching
        # Claude Code's per-tool permission rules.
        for risk in ("HIGH", "CRITICAL"):
            opts = options_for_interrupt({"type": "task_runner_permission", "risk_level": risk})
            self.assertEqual(opts, ["approve", "session", "project", "reject"], risk)


if __name__ == "__main__":
    unittest.main(verbosity=2)
