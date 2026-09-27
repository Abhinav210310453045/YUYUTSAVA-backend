"""What `execution_mode` changes about the CLI agent, and what it does not.

Docker mode swaps three things: where files and `execute` go (a sandbox
container instead of the host shell), which system prompt the agent reads, and
where the TaskRunner's exec tools run. Everything else — the tools offered, the
middleware stack, the subagents — is the same agent in both modes.

The agent fingerprint only builds local mode, so these are what pin docker
mode. The container is a stand-in: nothing here needs docker.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from deepagents.backends import CompositeBackend, LocalShellBackend

from test.support.agent_fingerprint import capture_cli, fingerprint_from_kwargs
from yuyutsava.agents.task_runner.exec_backend import DockerExecBackend
from yuyutsava.config.execution import DockerSettings, LocalSettings
from yuyutsava.core import engine
from yuyutsava.docker_sandbox.backend import DockerSandboxBackend
from yuyutsava.storage.paths import CONTAINER_STATE_MOUNT, CONTAINER_WORKSPACE_MOUNT


class _Sandbox:
    """Stands in for ``DockerSandboxBackend``; records how it was made."""

    def __init__(self, settings: DockerSettings, **kwargs: Any) -> None:
        self.settings = settings
        self.kwargs = kwargs
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def _docker(**overrides: Any) -> tuple[Any, dict[str, Any]]:
    with mock.patch.object(engine, "DockerSandboxBackend", _Sandbox):
        return capture_cli(execution_mode="docker", **overrides)


class DockerModeRunsInTheContainer(unittest.TestCase):
    def test_files_and_execute_go_to_the_sandbox(self) -> None:
        bundle, kwargs = _docker()
        backend = kwargs["backend"]
        self.assertIsInstance(backend, CompositeBackend)
        self.assertIsInstance(backend.default, _Sandbox)
        self.assertIs(backend.default, bundle.docker_backend)
        self.assertEqual(backend.artifacts_root, "/tmp")

    def test_the_exec_tools_are_bound_to_the_container(self) -> None:
        seen: list[Any] = []
        real = engine._bind_task_runner_tools

        def _spy(*args: Any, **kwargs: Any) -> Any:
            seen.append(kwargs.get("exec_backend"))
            return real(*args, **kwargs)

        with mock.patch.object(engine, "_bind_task_runner_tools", _spy):
            bundle, _ = _docker()
            capture_cli(execution_mode="local")
        docker_exec, local_exec = seen
        self.assertIsInstance(docker_exec, DockerExecBackend)
        self.assertIs(docker_exec._backend, bundle.docker_backend)
        self.assertIsNone(local_exec)

    def test_the_container_mounts_state_read_write_and_the_workspace_read_only(self) -> None:
        settings = DockerSettings(
            image="img:x", network="none", memory="1g", cpus="2", pids_limit=7
        )
        bundle, _ = _docker(docker_settings=settings)
        layout = bundle.layout
        made = bundle.docker_backend.kwargs
        self.assertEqual(made["workspace_host"], layout.state)
        self.assertEqual(made["container_workdir"], CONTAINER_STATE_MOUNT)
        self.assertEqual(made["extra_mounts"], [(layout.root, CONTAINER_WORKSPACE_MOUNT, True)])
        self.assertIs(bundle.docker_backend.settings, settings)

    def test_an_export_dir_is_mounted_resolved(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(DockerSandboxBackend, "_start_container"),
        ):
            backend = DockerSandboxBackend(
                DockerSettings(export_dir=Path("relative/out")), workspace_host=Path(tmp)
            )
        self.assertEqual(backend._export_host, Path("relative/out").resolve())

    def test_the_bundle_stops_the_container_on_close(self) -> None:
        bundle, _ = _docker()
        bundle.docker_backend.stop()
        self.assertTrue(bundle.docker_backend.stopped)

    def test_a_sandbox_outside_the_state_dir_is_warned_about(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outside = LocalSettings(sandbox_dir=Path(tmp))
            with self.assertLogs("yuyutsava", level="WARNING") as logs:
                _docker(local_settings=outside)
        self.assertTrue(any("not mounted in the container" in line for line in logs.output))


class LocalModeRunsOnTheHost(unittest.TestCase):
    def test_files_and_execute_go_to_the_host_shell(self) -> None:
        bundle, kwargs = capture_cli(execution_mode="local")
        backend = kwargs["backend"]
        self.assertIsInstance(backend, CompositeBackend)
        self.assertIsInstance(backend.default, LocalShellBackend)
        self.assertEqual(backend.artifacts_root, engine._SCRATCH_ROOT)
        self.assertIsNone(bundle.docker_backend)


class OnlyTheSandboxDiffers(unittest.TestCase):
    def test_same_tools_middleware_and_subagents(self) -> None:
        _, local = capture_cli(execution_mode="local")
        _, docker = _docker()
        lf, df = fingerprint_from_kwargs(local), fingerprint_from_kwargs(docker)
        for key in ("tools", "middleware", "order", "chains", "policies", "subagents"):
            with self.subTest(key=key):
                self.assertEqual(df[key], lf[key])

    def test_the_prompt_is_the_docker_one(self) -> None:
        _, local = capture_cli(execution_mode="local")
        _, docker = _docker()
        self.assertNotEqual(docker["system_prompt"], local["system_prompt"])
        self.assertIn(CONTAINER_WORKSPACE_MOUNT, docker["system_prompt"])


if __name__ == "__main__":
    unittest.main()
