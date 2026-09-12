"""``exec_backend`` — where sandbox commands and scripts run.

* ``DockerExecBackend.map_path`` translates host paths onto the container's two
  mounts (state dir first, then the read-only workspace) and refuses anything
  outside them — a script the container cannot see must not silently run on
  the host.
* ``run`` / ``run_python`` build ``docker exec -w <container cwd> … sh -c`` /
  ``python3 <container script>`` argv, create the host cwd first, and return
  the executor's ``{stdout, stderr, exit_code}`` shape.
* ``HostExecBackend`` really runs a script with this interpreter — the
  pre-existing behaviour ``TaskRunnerAgent`` falls back to.

Run:  .venv/bin/python test/task_runner/test_exec_backend.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yuyutsava.agents.task_runner import exec_backend
from yuyutsava.agents.task_runner.exec_backend import DockerExecBackend, HostExecBackend
from yuyutsava.storage.paths import WorkspaceLayout


class _FakeContainer:
    """Just enough of DockerSandboxBackend: exec_argv."""

    def exec_argv(self, *, cwd: str | None = None) -> list[str]:
        return ["docker", "exec", "-w", cwd or "/yuyutsava", "cid123"]


class MapPath(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        self.layout = WorkspaceLayout.for_workspace(self.ws)
        self.backend = DockerExecBackend(_FakeContainer(), self.layout)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_state_dir_maps_to_the_rw_mount(self) -> None:
        self.assertEqual(self.backend.map_path(self.layout.sandbox), "/yuyutsava/sandbox")
        self.assertEqual(
            self.backend.map_path(self.layout.scripts / "x.py"), "/yuyutsava/scripts/x.py"
        )
        self.assertEqual(self.backend.map_path(self.layout.state), "/yuyutsava")

    def test_workspace_maps_to_the_ro_mount(self) -> None:
        self.assertEqual(self.backend.map_path(self.ws / "src" / "a.txt"), "/workspace/src/a.txt")
        self.assertEqual(self.backend.map_path(self.ws), "/workspace")

    def test_outside_both_mounts_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as other:
            with self.assertRaises(ValueError) as ctx:
                self.backend.map_path(Path(other) / "script.py")
        self.assertIn("outside the mounted dirs", str(ctx.exception))

    def test_custom_mount_points(self) -> None:
        backend = DockerExecBackend(
            _FakeContainer(), self.layout, state_mount="/st", workspace_mount="/ws",
        )
        self.assertEqual(backend.map_path(self.layout.outputs), "/st/outputs")
        self.assertEqual(backend.map_path(self.ws / "f"), "/ws/f")


class RunArgv(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        self.layout = WorkspaceLayout.for_workspace(self.ws)
        self.backend = DockerExecBackend(_FakeContainer(), self.layout)
        self.calls: list[tuple[list[str], dict]] = []

        async def fake_run_capture(argv, **kw):
            self.calls.append((list(argv), kw))
            return b"out\n", b"warn\n", 0

        self._patch = mock.patch.object(exec_backend, "run_capture", fake_run_capture)
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self._tmp.cleanup()

    def test_run_execs_sh_in_the_container_sandbox(self) -> None:
        res = asyncio.run(self.backend.run("echo hi", self.layout.sandbox, 30))
        argv, kw = self.calls[0]
        self.assertEqual(
            argv, ["docker", "exec", "-w", "/yuyutsava/sandbox", "cid123", "sh", "-c", "echo hi"],
        )
        self.assertEqual(kw["timeout"], 30)
        self.assertTrue(self.layout.sandbox.is_dir())  # host-side mkdir, like execute_run
        self.assertEqual(res, {"stdout": "out", "stderr": "warn", "exit_code": 0})

    def test_run_python_maps_the_script(self) -> None:
        script = self.layout.scripts / "job.py"
        asyncio.run(self.backend.run_python(script, self.layout.sandbox, 10))
        argv, _ = self.calls[0]
        self.assertEqual(
            argv,
            ["docker", "exec", "-w", "/yuyutsava/sandbox", "cid123",
             "python3", "/yuyutsava/scripts/job.py"],
        )

    def test_unmapped_script_never_reaches_docker(self) -> None:
        with tempfile.TemporaryDirectory() as other:
            with self.assertRaises(ValueError):
                asyncio.run(self.backend.run_python(Path(other) / "x.py", self.layout.sandbox, 10))
        self.assertEqual(self.calls, [])


class HostBackend(unittest.TestCase):
    def test_run_python_uses_this_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "s.py"
            script.write_text("import sys; print(sys.executable)", encoding="utf-8")
            res = asyncio.run(HostExecBackend().run_python(script, Path(tmp) / "sb", 30))
        self.assertEqual(res["exit_code"], 0)
        self.assertEqual(res["stdout"], sys.executable)


if __name__ == "__main__":
    unittest.main()
