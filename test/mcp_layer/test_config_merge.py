"""``MCPConfig`` — the two-level loader.

Pins the contract between the global ``~/.yuyutsava/mcp_config.json`` and a
workspace's ``<ws>/.yuyutsava/mcp_config.json``:

* a workspace server overrides a global one of the same name; scopes and
  ``default_scope`` are unioned per agent, in order;
* ``$VAR`` / ``${YUYUTSAVA_WORKSPACE}`` expand in command, args, url and env;
* the workspace file is honoured only for a trusted workspace — otherwise it is
  logged and skipped, so a checkout cannot spawn processes on its own say-so;
* ``source`` is bookkeeping and never makes two otherwise-equal specs differ
  (hot_reload diffs specs by equality).

Run:  .venv/bin/python test/mcp_layer/test_config_merge.py
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yuyutsava.mcp.config import MCPConfig, MCPServerSpec, global_mcp_config_path
from yuyutsava.storage.paths import WorkspaceLayout


def _write(path: Path, body: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


class _Env(unittest.TestCase):
    """Fresh YUYUTSAVA_HOME + workspace per test."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name).resolve()
        self.home = root / "home"
        self.ws = root / "project"
        self.ws.mkdir()
        self._env = mock.patch.dict(
            os.environ, {"YUYUTSAVA_HOME": str(self.home), "DEMO_TOKEN": "sekrit"},
        )
        self._env.start()
        self.global_path = global_mcp_config_path()
        self.ws_path = WorkspaceLayout.for_workspace(self.ws).mcp_config

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()


class FromFile(_Env):
    def test_missing_file_is_empty(self) -> None:
        cfg = MCPConfig.from_file()
        self.assertEqual(cfg.servers, {})
        self.assertEqual(cfg.trusted_workspaces, ())

    def test_expansion_everywhere_and_source(self) -> None:
        _write(self.global_path, {
            "mcpServers": {
                "echo": {
                    "command": "$DEMO_TOKEN-bin",
                    "args": ["${YUYUTSAVA_WORKSPACE}/.yuyutsava/scripts/echo.py", "$DEMO_TOKEN"],
                    "env": {"TOKEN": "${DEMO_TOKEN}", "WS": "$YUYUTSAVA_WORKSPACE"},
                },
                "remote": {"url": "http://host/${DEMO_TOKEN}/sse"},
            },
            "trusted_workspaces": [str(self.ws)],
        })
        cfg = MCPConfig.from_file(workspace=self.ws)
        echo = cfg.servers["echo"]
        self.assertEqual(echo.command, "sekrit-bin")
        self.assertEqual(echo.args, (f"{self.ws}/.yuyutsava/scripts/echo.py", "sekrit"))
        self.assertEqual(echo.env, {"TOKEN": "sekrit", "WS": str(self.ws)})
        self.assertEqual(cfg.servers["remote"].url, "http://host/sekrit/sse")
        self.assertEqual(echo.source, str(self.global_path))
        self.assertEqual(cfg.trusted_workspaces, (str(self.ws),))

    def test_invalid_entries_are_skipped(self) -> None:
        _write(self.global_path, {"mcpServers": {
            "both": {"command": "x", "url": "http://y"},
            "neither": {},
            "notadict": "x",
            "ok": {"command": "x"},
        }})
        self.assertEqual(list(MCPConfig.from_file().servers), ["ok"])

    def test_source_does_not_affect_equality(self) -> None:
        a = MCPServerSpec(name="s", command="x", source="/a")
        b = MCPServerSpec(name="s", command="x", source="/b")
        self.assertEqual(a, b)
        self.assertNotEqual(a, MCPServerSpec(name="s", command="y", source="/a"))


class Merge(unittest.TestCase):
    def test_overlay_wins_scopes_union(self) -> None:
        base = MCPConfig(
            servers={"a": MCPServerSpec(name="a", command="a1"), "b": MCPServerSpec(name="b", command="b")},
            scopes={"cli": ["a"], "orchestrator": ["b"]},
            default_scope=["a"],
            trusted_workspaces=("/x",),
        )
        overlay = MCPConfig(
            servers={"a": MCPServerSpec(name="a", command="a2"), "c": MCPServerSpec(name="c", url="http://c")},
            scopes={"cli": ["c", "a"], "tinker": ["c"]},
            default_scope=["c", "a"],
            trusted_workspaces=("/ignored",),
        )
        merged = MCPConfig.merge(base, overlay)
        self.assertEqual(merged.servers["a"].command, "a2")
        self.assertEqual(set(merged.servers), {"a", "b", "c"})
        self.assertEqual(merged.scopes, {"cli": ["a", "c"], "orchestrator": ["b"], "tinker": ["c"]})
        self.assertEqual(merged.default_scope, ["a", "c"])
        self.assertEqual(merged.trusted_workspaces, ("/x",))
        self.assertEqual(merged.servers_for("tinker"), ["c"])
        self.assertEqual(merged.servers_for("unknown"), ["a", "c"])


class Load(_Env):
    def _global(self, **extra) -> None:
        _write(self.global_path, {
            "mcpServers": {"g": {"command": "g"}},
            "scopes": {"cli": ["g"]},
            **extra,
        })

    def _workspace(self) -> None:
        _write(self.ws_path, {
            "mcpServers": {"w": {"command": "${YUYUTSAVA_WORKSPACE}/run.sh"}},
            "scopes": {"cli": ["w"]},
        })

    def test_no_workspace_returns_global(self) -> None:
        self._global()
        self.assertEqual(list(MCPConfig.load(None).servers), ["g"])

    def test_untrusted_workspace_file_is_skipped_with_a_warning(self) -> None:
        self._global()
        self._workspace()
        with self.assertLogs("yuyutsava.mcp.config", level="WARNING") as cm:
            cfg = MCPConfig.load(self.ws)
        self.assertEqual(list(cfg.servers), ["g"])
        self.assertIn("trusted_workspaces", cm.output[0])
        self.assertIn(str(self.ws_path), cm.output[0])

    def test_trusted_workspace_file_is_merged(self) -> None:
        self._global(trusted_workspaces=[str(self.ws)])
        self._workspace()
        cfg = MCPConfig.load(self.ws)
        self.assertEqual(set(cfg.servers), {"g", "w"})
        self.assertEqual(cfg.servers["w"].command, f"{self.ws}/run.sh")
        self.assertEqual(cfg.servers["w"].source, str(self.ws_path))
        self.assertEqual(cfg.servers_for("cli"), ["g", "w"])

    def test_trust_matches_after_normalisation(self) -> None:
        self._global(trusted_workspaces=[str(self.ws) + "/"])
        self._workspace()
        self.assertTrue(MCPConfig.load(self.ws).trusts(self.ws))
        self.assertIn("w", MCPConfig.load(self.ws).servers)

    def test_missing_workspace_file_is_fine(self) -> None:
        self._global(trusted_workspaces=[str(self.ws)])
        self.assertEqual(list(MCPConfig.load(self.ws).servers), ["g"])


if __name__ == "__main__":
    unittest.main()
