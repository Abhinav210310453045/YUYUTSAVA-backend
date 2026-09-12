"""``WorkspaceLayout`` — the one helper every per-workspace path comes from.

Before it existed, ``<ws>/_sandbox``, ``<ws>/_output``, ``large_tool_results``
and friends were joined by hand in nine places; a path could enter the wrong
folder simply by one of them drifting. These checks pin the contract:

* every path derives from ``<ws>/.yuyutsava`` (pure, no filesystem),
* the explicit sandbox/outputs overrides still win,
* a workspace whose ``.yuyutsava`` *is* the global state dir is routed aside,
* ``ensure()`` creates the working dirs and seeds the knowledge files once,
  never rewrites them, and never creates the sandbox.

Run:  .venv/bin/python test/storage/test_workspace_layout.py
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yuyutsava.storage.paths import (
    WORKSPACE_GITIGNORE,
    WORKSPACE_STATE_DIRNAME,
    WorkspaceLayout,
    state_dir,
)


class DerivedPaths(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        self.layout = WorkspaceLayout.for_workspace(self.ws)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_state_dir_is_hidden_child_of_root(self) -> None:
        self.assertEqual(self.layout.root, self.ws)
        self.assertEqual(self.layout.state, self.ws / WORKSPACE_STATE_DIRNAME)
        self.assertEqual(WORKSPACE_STATE_DIRNAME, ".yuyutsava")

    def test_every_path_lives_under_state(self) -> None:
        st = self.layout.state
        self.assertEqual(self.layout.sandbox, st / "sandbox")
        self.assertEqual(self.layout.outputs, st / "outputs")
        self.assertEqual(self.layout.scripts, st / "scripts")
        self.assertEqual(self.layout.tmp, st / "tmp")
        self.assertEqual(self.layout.skills, st / "skills")
        self.assertEqual(self.layout.changelog, st / "ChangeLog.md")
        self.assertEqual(self.layout.assumptions, st / "Assumptions.md")
        self.assertEqual(self.layout.memory, st / "workspace_memory.md")
        self.assertEqual(self.layout.mcp_config, st / "mcp_config.json")
        self.assertEqual(self.layout.gitignore, st / ".gitignore")
        self.assertEqual(self.layout.large_tool_results, st / "tmp" / "large_tool_results")
        self.assertEqual(self.layout.conversation_history, st / "tmp" / "conversation_history")

    def test_for_workspace_is_pure(self) -> None:
        self.assertFalse(self.layout.state.exists())

    def test_overrides_win_and_are_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as other:
            sb = Path(other) / "sb"
            out = Path(other) / "out"
            layout = WorkspaceLayout.for_workspace(
                self.ws, sandbox_override=sb, outputs_override=out,
            )
        self.assertEqual(layout.sandbox, sb.resolve())
        self.assertEqual(layout.outputs, out.resolve())
        # Everything else is untouched by the overrides.
        self.assertEqual(layout.scripts, self.layout.scripts)

    def test_accepts_a_string_and_a_tilde(self) -> None:
        layout = WorkspaceLayout.for_workspace(str(self.ws))
        self.assertEqual(layout.state, self.layout.state)
        home_layout = WorkspaceLayout.for_workspace("~")
        self.assertEqual(home_layout.root, Path.home().resolve())


class HomeCollision(unittest.TestCase):
    def test_workspace_containing_the_global_dir_is_routed_aside(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp).resolve()
            with mock.patch.dict(os.environ, {"YUYUTSAVA_HOME": str(ws / ".yuyutsava")}):
                layout = WorkspaceLayout.for_workspace(ws)
                self.assertEqual(layout.state, state_dir() / "workspaces" / "home")
                self.assertEqual(layout.sandbox, layout.state / "sandbox")

    def test_ordinary_workspace_is_not_affected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp).resolve()
            with mock.patch.dict(os.environ, {"YUYUTSAVA_HOME": str(ws / "elsewhere")}):
                layout = WorkspaceLayout.for_workspace(ws)
                self.assertEqual(layout.state, ws / ".yuyutsava")


class Ensure(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        self.layout = WorkspaceLayout.for_workspace(self.ws)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_creates_working_dirs_but_never_the_sandbox(self) -> None:
        self.layout.ensure()
        for d in (self.layout.state, self.layout.scripts, self.layout.outputs, self.layout.tmp):
            self.assertTrue(d.is_dir(), d)
        self.assertFalse(self.layout.sandbox.exists())
        self.assertFalse(self.layout.skills.exists())  # created by whoever writes a skill

    def test_gitignore_ignores_everything(self) -> None:
        self.layout.ensure()
        self.assertEqual(self.layout.gitignore.read_text(encoding="utf-8"), "*\n")
        self.assertEqual(WORKSPACE_GITIGNORE, "*\n")

    def test_knowledge_files_are_seeded_with_their_format(self) -> None:
        self.layout.ensure()
        changelog = self.layout.changelog.read_text(encoding="utf-8")
        self.assertTrue(changelog.startswith("# ChangeLog"))
        self.assertIn("## <ISO-8601 UTC> · <agent role> · <task gist", changelog)
        assumptions = self.layout.assumptions.read_text(encoding="utf-8")
        self.assertIn("- ASSUMED: <what> — because <why>", assumptions)
        memory = self.layout.memory.read_text(encoding="utf-8")
        self.assertTrue(memory.startswith("# Workspace memory"))
        self.assertIn("- <YYYY-MM-DD> [layout|conventions|gotchas|decisions] <fact>", memory)

    def test_ensure_is_idempotent_and_never_rewrites(self) -> None:
        self.layout.ensure()
        self.layout.changelog.write_text("# ChangeLog\n\n## entry\n- x — y (file)\n", encoding="utf-8")
        self.layout.gitignore.write_text("custom\n", encoding="utf-8")
        self.layout.ensure()
        self.assertIn("## entry", self.layout.changelog.read_text(encoding="utf-8"))
        self.assertEqual(self.layout.gitignore.read_text(encoding="utf-8"), "custom\n")

    def test_seed_files_can_be_skipped(self) -> None:
        self.layout.ensure(seed_files=False)
        self.assertTrue(self.layout.state.is_dir())
        self.assertFalse(self.layout.changelog.exists())
        self.assertFalse(self.layout.gitignore.exists())


if __name__ == "__main__":
    unittest.main()
