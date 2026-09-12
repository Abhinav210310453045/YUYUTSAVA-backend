"""Where sandbox commands and scripts actually run: the host, or the container.

``TaskRunnerAgent`` decides *whether* an operation may run (zones,
permissions, consent). This module decides *where* the two sandbox-cwd
operations run once they are allowed:

* ``tr_execute_in_sandbox`` — a shell command with cwd = the sandbox
* ``tr_run_python``         — a Python script with cwd = the sandbox

Everything else — ``tr_read_file``/``tr_write_file``/``tr_grep``/…, the host
shell ``tr_execute``, elevated runs — is host-side by definition and never
comes through here.

:class:`HostExecBackend` is the pre-existing behaviour, unchanged.
:class:`DockerExecBackend` maps host paths onto the container's two bind
mounts — ``<ws>/.yuyutsava`` → ``/yuyutsava`` (read-write) and ``<ws>`` →
``/workspace`` (read-only) — and runs ``docker exec -w <cwd> …`` through the
loop-agnostic :func:`yuyutsava.platform.process.run_capture`: argv form, no
stdin, no second shell layer, and it works on the Windows Selector loop where
``asyncio.create_subprocess_exec`` does not. Both backends return the
``{stdout, stderr, exit_code}`` dict the executor already produces, so the
tool result is built the same way either side.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from yuyutsava.agents.task_runner import executor as _exec
from yuyutsava.platform.process import run_capture
from yuyutsava.storage.paths import (
    CONTAINER_STATE_MOUNT,
    CONTAINER_WORKSPACE_MOUNT,
    WorkspaceLayout,
)


class ExecBackend(Protocol):
    """The two sandbox-cwd primitives ``TaskRunnerAgent`` dispatches to."""

    async def run(self, command: str, cwd: Path, timeout: int) -> dict: ...

    async def run_python(self, script: Path, cwd: Path, timeout: int) -> dict: ...


class HostExecBackend:
    """Run on the host — the default; byte-identical to the executor calls."""

    async def run(self, command: str, cwd: Path, timeout: int) -> dict:
        return await _exec.execute_run(command, cwd, timeout)

    async def run_python(self, script: Path, cwd: Path, timeout: int) -> dict:
        return await _exec.execute_python(script, cwd, timeout)


class DockerExecBackend:
    """Run inside the sandbox container, over its two bind mounts.

    *backend* is the ``DockerSandboxBackend`` owning the container (duck-typed:
    only ``exec_argv`` is used). *layout* tells which host paths are visible
    inside — the state dir at ``state_mount`` and the workspace at
    ``workspace_mount`` — and therefore how to translate them.
    """

    def __init__(
        self,
        backend: Any,
        layout: WorkspaceLayout,
        *,
        state_mount: str = CONTAINER_STATE_MOUNT,
        workspace_mount: str = CONTAINER_WORKSPACE_MOUNT,
    ) -> None:
        self._backend = backend
        self._layout = layout
        self._state_mount = state_mount
        self._workspace_mount = workspace_mount

    def map_path(self, host: Path) -> str:
        """Host path → container path, via whichever mount contains it.

        The state dir is checked first (it normally sits inside the
        workspace). A path under neither mount cannot exist in the container,
        so it is an error rather than a silent host-side fallback.
        """
        p = Path(host).expanduser().resolve()
        for root, mount in (
            (self._layout.state, self._state_mount),
            (self._layout.root, self._workspace_mount),
        ):
            try:
                rel = p.relative_to(root)
            except ValueError:
                continue
            return str(PurePosixPath(mount, *rel.parts))
        raise ValueError(
            f"{p} is outside the mounted dirs ({self._layout.state} → "
            f"{self._state_mount}, {self._layout.root} → {self._workspace_mount}); "
            "only files there are reachable inside the container"
        )

    async def run(self, command: str, cwd: Path, timeout: int) -> dict:
        # Host-side mkdir, exactly like execute_run: the dir is created by the
        # host user (a container-side mkdir would leave it root-owned on Linux).
        cwd.mkdir(parents=True, exist_ok=True)
        argv = [*self._backend.exec_argv(cwd=self.map_path(cwd)), "sh", "-c", command]
        return await _capture(argv, cwd, timeout)

    async def run_python(self, script: Path, cwd: Path, timeout: int) -> dict:
        cwd.mkdir(parents=True, exist_ok=True)
        argv = [
            *self._backend.exec_argv(cwd=self.map_path(cwd)),
            "python3", self.map_path(script),
        ]
        return await _capture(argv, cwd, timeout)


async def _capture(argv: list[str], host_cwd: Path, timeout: int) -> dict:
    """``run_capture`` → the executor's ``{stdout, stderr, exit_code}`` shape."""
    out, err, exit_code = await run_capture(argv, cwd=str(host_cwd), timeout=timeout)
    return {
        "stdout": out.decode(errors="replace").strip(),
        "stderr": err.decode(errors="replace").strip(),
        "exit_code": exit_code,
    }


__all__ = ["DockerExecBackend", "ExecBackend", "HostExecBackend"]
