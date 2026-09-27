"""The Docker sandbox: the container it asks for, and what the agent gets back (R-TST-06).

A fake ``docker`` on ``PATH`` records every invocation and plays the CLI's part,
so this runs without Docker. What must hold: the container is locked down
(read-only root, no new privileges, the configured network and limits, extra
mounts read-only when asked), a command's output reaches the agent with stderr
tagged and the exit code appended, a timeout says so, and a missing or failing
``docker`` is a clear error.

Run:  .venv/bin/python -m pytest test/docker_sandbox/test_backend.py
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from yuyutsava.config.execution import DockerSettings
from yuyutsava.docker_sandbox.backend import DockerSandboxBackend

FAKE_DOCKER = textwrap.dedent(
    """\
    #!{python}
    import json, os, sys, time
    log = os.environ["FAKE_DOCKER_LOG"]
    with open(log, "a") as fh:
        fh.write(json.dumps(sys.argv[1:]) + "\\n")
    mode = os.environ.get("FAKE_DOCKER_MODE", "")
    if sys.argv[1] == "run":
        if mode == "run-fails":
            sys.stderr.write("image not found\\n")
            sys.exit(125)
        print("c0ffee1234567890")
    elif sys.argv[1] == "exec":
        script = sys.stdin.read()
        if "sleep" in script:
            time.sleep(10)
        print("ran: " + script.strip())
        sys.stderr.write("a warning\\n")
        sys.exit(3 if "fail" in script else 0)
    """
)


@unittest.skipIf(sys.platform == "win32", "the fake docker is a POSIX script")
class _FakeDocker(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text(FAKE_DOCKER.format(python=sys.executable))
        docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
        self.log = root / "docker.log"
        self.workspace = root / "ws"
        self.env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "FAKE_DOCKER_LOG": str(self.log),
        }
        self._env = mock.patch.dict(os.environ, self.env)
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def sandbox(self, **kw) -> DockerSandboxBackend:
        settings = kw.pop("settings", DockerSettings(network="none", pids_limit=64))
        return DockerSandboxBackend(settings, workspace_host=self.workspace, **kw)


class TheContainerIsLockedDown(_FakeDocker):
    def test_docker_run_asks_for_the_locked_down_container(self) -> None:
        box = self.sandbox(extra_mounts=[(Path(self._tmp.name) / "skills", "/skills", True)])
        (run,) = self.calls()
        self.assertEqual(run[:3], ["run", "-d", "--rm"])
        for flag in ("--read-only", "no-new-privileges"):
            self.assertIn(flag, run)
        self.assertEqual(run[run.index("--network") + 1], "none")
        self.assertEqual(run[run.index("--pids-limit") + 1], "64")
        self.assertIn("/tmp:rw,size=64m", run)
        self.assertIn(f"{self.workspace.resolve()}:/workspace", run)
        self.assertTrue(any(v.endswith(":/skills:ro") for v in run), run)
        self.assertEqual(run[-2:], ["sleep", "infinity"])
        self.assertEqual(box.container_id, "c0ffee1234567890")

    def test_the_workspace_exists_before_docker_mounts_it(self) -> None:
        self.sandbox()
        self.assertTrue(self.workspace.is_dir(), "docker would create it as root")


class Commands(_FakeDocker):
    def test_output_stderr_and_exit_code_reach_the_agent(self) -> None:
        res = self.sandbox().execute("echo fail")
        self.assertEqual(res.exit_code, 3)
        self.assertIn("ran: echo fail", res.output)
        self.assertIn("[stderr] a warning", res.output)
        self.assertTrue(res.output.rstrip().endswith("Exit code: 3"))
        exec_call = self.calls()[-1]
        self.assertEqual(exec_call[:2], ["exec", "-i"])
        self.assertIn("c0ffee1234567890", exec_call)

    def test_output_is_cut_at_the_limit(self) -> None:
        res = self.sandbox(max_output_bytes=10).execute("echo hello-world-and-more")
        self.assertTrue(res.truncated)
        self.assertIn("Output truncated at 10 bytes", res.output)

    def test_a_timeout_says_so(self) -> None:
        res = self.sandbox().execute("sleep 30", timeout=1)
        self.assertEqual(res.exit_code, 124)
        self.assertIn("timed out after 1 seconds", res.output)

    def test_an_empty_command_is_refused_without_calling_docker(self) -> None:
        box = self.sandbox()
        before = len(self.calls())
        self.assertEqual(box.execute("").exit_code, 1)
        self.assertEqual(len(self.calls()), before)


class WhenDockerIsNotThere(_FakeDocker):
    def test_a_failing_run_names_dockers_own_error(self) -> None:
        with mock.patch.dict(os.environ, {"FAKE_DOCKER_MODE": "run-fails"}):
            with self.assertRaisesRegex(RuntimeError, "docker run failed: image not found"):
                self.sandbox()

    def test_no_docker_cli_is_a_clear_error(self) -> None:
        with mock.patch.dict(os.environ, {"PATH": str(Path(self._tmp.name) / "nowhere")}):
            with self.assertRaisesRegex(RuntimeError, "docker CLI not found"):
                self.sandbox()


if __name__ == "__main__":
    unittest.main()
