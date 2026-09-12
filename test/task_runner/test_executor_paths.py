"""Executor + tool-registry contracts introduced with the per-workspace layout.

* ``execute_write(append=True)`` appends (creating the file) instead of
  overwriting — the knowledge files in ``.yuyutsava/`` are append-only.
* ``_sync_grep`` / ``_sync_glob`` skip ``.yuyutsava/`` when the search is rooted
  outside it and see everything when rooted inside it: a workspace-wide code
  search is not polluted by offloaded tool dumps, while the prompt's explicit
  "tr_grep the state dir" still works.
* ``_get_or_create_agent(ws)`` and ``_get_or_create_agent(ws, <layout sandbox>)``
  return the same cached ``TaskRunnerAgent`` — the default is resolved before
  keying, so master, subagents and ``spawn.py`` share one instance.

Run:  .venv/bin/python test/task_runner/test_executor_paths.py
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from yuyutsava.agents.task_runner import executor
from yuyutsava.storage.paths import WorkspaceLayout


class AppendWrite(unittest.TestCase):
    def test_append_creates_then_appends_then_overwrite_still_overwrites(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "nested" / "ChangeLog.md"
            asyncio.run(executor.execute_write(p, "one\n", append=True))
            asyncio.run(executor.execute_write(p, "two\n", append=True))
            self.assertEqual(p.read_text(encoding="utf-8"), "one\ntwo\n")
            asyncio.run(executor.execute_write(p, "fresh\n"))
            self.assertEqual(p.read_text(encoding="utf-8"), "fresh\n")


class StateDirHygiene(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        self.layout = WorkspaceLayout.for_workspace(self.ws)
        (self.ws / "src").mkdir()
        (self.ws / "src" / "app.py").write_text("needle = 1\n", encoding="utf-8")
        (self.ws / "notes.txt").write_text("plain needle\n", encoding="utf-8")
        self.layout.large_tool_results.mkdir(parents=True)
        (self.layout.large_tool_results / "dump.txt").write_text(
            "needle in an offloaded dump\n", encoding="utf-8"
        )
        self.layout.ensure()
        self.layout.changelog.write_text("# ChangeLog\n- needle changed\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_grep_from_workspace_root_skips_state_dir(self) -> None:
        out = executor._sync_grep("needle", self.ws, 0, False, 100)["stdout"]
        self.assertIn("src/app.py", out)
        self.assertIn("notes.txt", out)
        self.assertNotIn("dump.txt", out)
        self.assertNotIn("ChangeLog.md", out)

    def test_grep_rooted_in_state_dir_sees_everything(self) -> None:
        out = executor._sync_grep("needle", self.layout.state, 0, False, 100)["stdout"]
        self.assertIn("ChangeLog.md", out)
        self.assertIn("dump.txt", out)

    def test_glob_from_workspace_root_skips_state_dir(self) -> None:
        res = executor._sync_glob(self.ws, "**/*.txt", 500)
        joined = json.dumps(res["entries"])
        self.assertIn("notes.txt", joined)
        self.assertNotIn("dump.txt", joined)
        self.assertEqual(res["total"], 1)

    def test_glob_rooted_in_state_dir_sees_everything(self) -> None:
        res = executor._sync_glob(self.layout.state, "**/*", 500)
        joined = json.dumps(res["entries"])
        self.assertIn("dump.txt", joined)
        self.assertIn("ChangeLog.md", joined)


class RegistryKey(unittest.TestCase):
    def test_default_sandbox_shares_the_cached_agent(self) -> None:
        from yuyutsava.agents.task_runner.tools import _get_or_create_agent

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp).resolve()
            layout = WorkspaceLayout.for_workspace(ws)
            a = _get_or_create_agent(ws)
            b = _get_or_create_agent(ws, layout.sandbox)
            self.assertIs(a, b)
            self.assertEqual(a.sandbox_root, layout.sandbox)
            self.assertEqual(a.workspace_root, ws)


if __name__ == "__main__":
    unittest.main()
