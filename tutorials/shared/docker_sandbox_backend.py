"""
Local Docker sandbox for Deep Agents: extends ``BaseSandbox`` so file tools and
``execute`` run inside a container. Mount host workspace (and optional export dir)
with ``docker run -v``; use ``download_files`` / ``upload_files`` or bind mounts for artifacts.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Literal

from deepagents.backends.protocol import (
    EditResult,
    ExecuteResponse,
    FileDownloadResponse,
    FileInfo,
    FileUploadResponse,
    WriteResult,
)
from deepagents.backends.sandbox import BaseSandbox

DEFAULT_DOCKER_TIMEOUT = 120
DEFAULT_MAX_OUTPUT_BYTES = 100_000


def _host_path_for_docker(p: Path) -> str:
    """Absolute path string suitable for ``docker -v`` on the current OS."""
    return str(p.resolve())


class DockerSandboxBackend(BaseSandbox):
    """``SandboxBackendProtocol`` backed by ``docker run`` + ``docker exec``.

    The container must include ``python3`` and ``grep`` (see ``tutorials/docker_sandbox/Dockerfile``).
    """

    def __init__(
        self,
        *,
        image: str,
        workspace_host: Path,
        container_workdir: str = "/workspace",
        export_host: Path | None = None,
        container_export_path: str = "/output",
        network: Literal["bridge", "none"] = "bridge",
        timeout: int = DEFAULT_DOCKER_TIMEOUT,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ) -> None:
        if timeout <= 0:
            msg = f"timeout must be positive, got {timeout}"
            raise ValueError(msg)
        self._image = image
        self._workspace_host = workspace_host.resolve()
        self._container_workdir = container_workdir
        self._export_host = export_host.resolve() if export_host else None
        self._container_export_path = container_export_path
        self._network = network
        self._default_timeout = timeout
        self._max_output_bytes = max_output_bytes
        self._container_id: str | None = None
        self._sandbox_id = f"docker-{uuid.uuid4().hex[:12]}"
        self._start_container()

    def _real_path(self, virtual_path: str) -> str:
        """Map agent virtual paths (``/foo``) to paths inside the container."""
        p = virtual_path.strip()
        out_mount = self._container_export_path.rstrip("/")
        export_virtual = "/output"
        if self._export_host is not None and (
            p == export_virtual or p.startswith(export_virtual + "/")
        ):
            rest = p[len(export_virtual) :].lstrip("/")
            if not rest:
                return out_mount
            return f"{out_mount}/{rest}"
        if not p.startswith("/"):
            return f"{self._container_workdir}/{p}" if p else self._container_workdir
        if p == "/":
            return self._container_workdir
        return f"{self._container_workdir}{p}"

    def ls_info(self, path: str) -> list[FileInfo]:
        return super().ls_info(self._real_path(path))

    def read(
        self,
        file_path: str,
        offset: int = 0,
        limit: int = 2000,
    ) -> str:
        return super().read(self._real_path(file_path), offset, limit)

    def write(self, file_path: str, content: str) -> WriteResult:
        return super().write(self._real_path(file_path), content)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,  # noqa: FBT001, FBT002
    ) -> EditResult:
        return super().edit(
            self._real_path(file_path),
            old_string,
            new_string,
            replace_all=replace_all,
        )

    def grep_raw(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> list | str:
        mapped = self._real_path(path) if path else None
        return super().grep_raw(pattern, mapped, glob)

    def glob_info(self, pattern: str, path: str = "/") -> list[FileInfo]:
        return super().glob_info(pattern, self._real_path(path))

    def _docker_cmd(self, *args: str) -> list[str]:
        return ["docker", *args]

    def _start_container(self) -> None:
        ws = _host_path_for_docker(self._workspace_host)
        vols: list[str] = ["-v", f"{ws}:{self._container_workdir}"]
        if self._export_host is not None:
            exp = _host_path_for_docker(self._export_host)
            self._export_host.mkdir(parents=True, exist_ok=True)
            vols.extend(["-v", f"{exp}:{self._container_export_path}"])

        run_cmd = self._docker_cmd(
            "run",
            "-d",
            "--rm",
            *vols,
            "-w",
            self._container_workdir,
            "--network",
            self._network,
            self._image,
            "sleep",
            "infinity",
        )
        try:
            proc = subprocess.run(  # noqa: S603
                run_cmd,
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except FileNotFoundError as e:
            msg = "docker CLI not found; install Docker and ensure it is on PATH."
            raise RuntimeError(msg) from e
        except subprocess.CalledProcessError as e:
            err = (e.stderr or e.stdout or "").strip() or str(e)
            msg = f"docker run failed: {err}"
            raise RuntimeError(msg) from e

        cid = proc.stdout.strip()
        if not cid:
            msg = "docker run returned empty container id"
            raise RuntimeError(msg)
        self._container_id = cid

    @property
    def id(self) -> str:
        return self._sandbox_id

    @property
    def container_id(self) -> str:
        if not self._container_id:
            msg = "container not started"
            raise RuntimeError(msg)
        return self._container_id

    def stop(self) -> None:
        """Stop and remove the sandbox container."""
        cid = self._container_id
        if not cid:
            return
        try:
            subprocess.run(  # noqa: S603
                self._docker_cmd("rm", "-f", cid),
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
        finally:
            self._container_id = None

    def __enter__(self) -> DockerSandboxBackend:
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        if not command or not isinstance(command, str):
            return ExecuteResponse(
                output="Error: Command must be a non-empty string.",
                exit_code=1,
                truncated=False,
            )
        cid = self.container_id
        effective_timeout = timeout if timeout is not None else self._default_timeout
        if effective_timeout <= 0:
            msg = f"timeout must be positive, got {effective_timeout}"
            raise ValueError(msg)

        exec_cmd = self._docker_cmd(
            "exec",
            "-i",
            "-w",
            self._container_workdir,
            cid,
            "sh",
            "-s",
        )
        try:
            proc = subprocess.run(  # noqa: S603
                exec_cmd,
                input=command,
                text=True,
                capture_output=True,
                timeout=effective_timeout,
                env=os.environ.copy(),
            )
        except subprocess.TimeoutExpired:
            if timeout is not None:
                msg = (
                    f"Error: Command timed out after {effective_timeout} seconds "
                    "(custom timeout). The command may be stuck or require more time."
                )
            else:
                msg = (
                    f"Error: Command timed out after {effective_timeout} seconds. "
                    "For long-running commands, re-run using the timeout parameter."
                )
            return ExecuteResponse(output=msg, exit_code=124, truncated=False)
        except Exception as e:  # noqa: BLE001
            return ExecuteResponse(
                output=f"Error executing command ({type(e).__name__}): {e}",
                exit_code=1,
                truncated=False,
            )

        output_parts: list[str] = []
        if proc.stdout:
            output_parts.append(proc.stdout)
        if proc.stderr:
            stderr_lines = proc.stderr.strip().split("\n")
            output_parts.extend(f"[stderr] {line}" for line in stderr_lines)

        output = "\n".join(output_parts) if output_parts else "<no output>"

        truncated = False
        if len(output) > self._max_output_bytes:
            output = output[: self._max_output_bytes]
            output += f"\n\n... Output truncated at {self._max_output_bytes} bytes."
            truncated = True

        if proc.returncode != 0:
            output = f"{output.rstrip()}\n\nExit code: {proc.returncode}"

        return ExecuteResponse(
            output=output,
            exit_code=proc.returncode,
            truncated=truncated,
        )

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        cid = self.container_id
        results: list[FileUploadResponse] = []
        for path, data in files:
            if not path.startswith("/"):
                results.append(
                    FileUploadResponse(path=path, error="invalid_path"),
                )
                continue
            cpath = self._real_path(path)
            parent = os.path.dirname(cpath) or "/"
            mk = self.execute(f"mkdir -p {shlex.quote(parent)}")
            if mk.exit_code != 0:
                results.append(
                    FileUploadResponse(path=path, error="permission_denied"),
                )
                continue
            tmp_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(delete=False) as tf:
                    tf.write(data)
                    tmp_path = Path(tf.name)
                dest = f"{cid}:{cpath}"
                cp = subprocess.run(  # noqa: S603
                    self._docker_cmd("cp", str(tmp_path), dest),
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                if cp.returncode != 0:
                    err_msg = (cp.stderr or cp.stdout or "").strip()
                    results.append(
                        FileUploadResponse(
                            path=path,
                            error="permission_denied" if "Permission" in err_msg else "invalid_path",
                        ),
                    )
                else:
                    results.append(FileUploadResponse(path=path, error=None))
            except OSError:
                results.append(FileUploadResponse(path=path, error="permission_denied"))
            finally:
                if tmp_path is not None and tmp_path.is_file():
                    tmp_path.unlink(missing_ok=True)
        return results

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        cid = self.container_id
        results: list[FileDownloadResponse] = []
        for path in paths:
            if not path.startswith("/"):
                results.append(
                    FileDownloadResponse(path=path, content=None, error="invalid_path"),
                )
                continue
            cpath = self._real_path(path)
            tmp_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(delete=False) as tf:
                    tmp_path = Path(tf.name)
                src = f"{cid}:{cpath}"
                cp = subprocess.run(  # noqa: S603
                    self._docker_cmd("cp", src, str(tmp_path)),
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                if cp.returncode != 0:
                    err = (cp.stderr or cp.stdout or "").strip().lower()
                    if "directory" in err:
                        results.append(
                            FileDownloadResponse(path=path, content=None, error="is_directory"),
                        )
                    else:
                        results.append(
                            FileDownloadResponse(path=path, content=None, error="file_not_found"),
                        )
                    continue
                content = tmp_path.read_bytes()
                results.append(FileDownloadResponse(path=path, content=content, error=None))
            except OSError:
                results.append(
                    FileDownloadResponse(path=path, content=None, error="permission_denied"),
                )
            finally:
                if tmp_path is not None and tmp_path.is_file():
                    tmp_path.unlink(missing_ok=True)
        return results


def pull_virtual_paths_to_host(
    backend: DockerSandboxBackend,
    virtual_paths: list[str],
    host_dir: Path,
) -> list[Path]:
    """Copy files out of the container via ``download_files`` into ``host_dir`` (mirrors virtual path layout)."""
    root = host_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for resp in backend.download_files(virtual_paths):
        if resp.error or resp.content is None:
            continue
        safe = resp.path.lstrip("/").replace("..", "_").strip("/")
        if not safe:
            continue
        dest = root / safe
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
        written.append(dest)
    return written
