"""What an allowed operation runs.

The gateway (:class:`~yuyutsava.agents.task_runner.agent.TaskRunnerAgent`)
decides *whether*; this decides *what*: one executor call per operation type,
and for EXECUTE, whatever the tool put in ``additional_context`` — a grep, a
fetch, a Python script, or a shell command (sandboxed through the exec backend,
or on the host).
"""

from __future__ import annotations

from pathlib import Path

from yuyutsava.agents.task_runner import executor as _exec
from yuyutsava.agents.task_runner.exec_backend import ExecBackend
from yuyutsava.models.operations import OperationRequest, OperationType
from yuyutsava.models.results import (
    DeleteResult,
    DirEntry,
    ListResult,
    ReadResult,
    ShellResult,
    WriteResult,
)
from yuyutsava.models.tool_messages import SuppressedContentNotice


def _shell_result(raw: dict) -> ShellResult:
    return ShellResult(stdout=raw["stdout"], stderr=raw["stderr"], exit_code=raw["exit_code"])


def _max_entries(ctx: dict) -> int:
    value = ctx.get("max_entries", 500)
    return int(value) if isinstance(value, (int, float, str)) else 500


def _list_result(path: Path, pattern: str | None, data: dict) -> ListResult:
    entries = [DirEntry(**e) for e in data["entries"]]
    return ListResult(
        root=str(path),
        pattern=pattern,
        entries=entries,
        returned=len(entries),
        total=data["total"],
        has_more=data["has_more"],
    )


async def run_operation(
    request: OperationRequest, *, exec_backend: ExecBackend, sandbox_root: Path
) -> ShellResult | WriteResult | DeleteResult | ReadResult | ListResult:
    """Run an allowed *request* and return its typed result.

    ``exec_backend`` decides where sandboxed commands and scripts run;
    ``sandbox_root`` is their default working directory. Executor errors
    propagate — the gateway turns them into an error response.
    """
    path = Path(request.paths[0])
    ctx = request.additional_context or {}

    match request.operation:
        case OperationType.READ:
            return await _read(path, ctx)
        case OperationType.WRITE | OperationType.CREATE:
            content = str(ctx.get("content", ""))
            await _exec.execute_write(path, content, append=bool(ctx.get("append", False)))
            return WriteResult(written_to=str(path))
        case OperationType.DELETE:
            await _exec.execute_delete(path)
            return DeleteResult(deleted=str(path))
        case OperationType.EXECUTE:
            return _shell_result(await _run_execute(ctx, exec_backend, sandbox_root))
        case OperationType.LIST:
            data = await _exec.execute_list(path, max_entries=_max_entries(ctx))
            return _list_result(path, None, data)
        case OperationType.GLOB:
            pattern = str(ctx.get("pattern", "*"))
            data = await _exec.execute_glob(path, pattern, max_entries=_max_entries(ctx))
            return _list_result(path, pattern, data)
        case _:
            raise NotImplementedError(
                f"Operation '{request.operation.value}' execution not implemented."
            )


async def _read(path: Path, ctx: dict) -> ReadResult:
    """A window of the file, with a notice when more remains."""
    offset = int(ctx.get("offset", 0))
    limit = ctx.get("limit")
    limit = int(limit) if limit is not None else None
    data = await _exec.execute_read(path, offset=offset, limit=limit)

    notice = None
    if data["has_more"]:
        notice = SuppressedContentNotice.file_too_large(
            tool="tr_read_file",
            path=str(path),
            original_size_chars=len(data["content"]),
            total_lines=data["total_lines"],
            shown_lines=data["offset"] + data["returned_lines"],
        )
    return ReadResult(
        content=data["content"],
        offset=data["offset"],
        limit=limit,
        returned_lines=data["returned_lines"],
        total_lines=data["total_lines"],
        has_more=data["has_more"],
        truncation_notice=notice,
    )


async def _run_execute(ctx: dict, exec_backend: ExecBackend, sandbox_root: Path) -> dict:
    """Run an EXECUTE request; ``additional_context`` says what it is.

    tr_grep / tr_fetch_url route pure-Python ops through EXECUTE so the
    zone/permission path is byte-identical to a shell command.
    """
    if "search" in ctx:
        sc = ctx["search"]
        return await _exec.execute_grep(
            str(sc["pattern"]),
            Path(str(sc["path"])),
            context_lines=int(sc.get("context_lines", 3)),
            case_insensitive=bool(sc.get("case_insensitive", False)),
            max_matches=int(sc.get("max_matches", 100)),
        )
    if "fetch" in ctx:
        fc = ctx["fetch"]
        return await _exec.execute_fetch(
            str(fc["url"]),
            Path(str(fc["dest"])),
            user_agent=str(fc["user_agent"]),
            timeout=int(fc.get("timeout", 120)),
        )
    if "python" in ctx:
        pc = ctx["python"]
        # tr_run_python: the exec backend decides WHERE (host or the Docker
        # container); cwd is always the sandbox.
        return await exec_backend.run_python(
            Path(str(pc["script_path"])),
            Path(str(pc.get("cwd", sandbox_root))),
            int(pc.get("timeout", 120)),
        )
    command = str(ctx.get("command", ""))
    _timeout = ctx.get("timeout", 120)
    timeout = int(_timeout) if isinstance(_timeout, (int, float, str)) else 120
    _cwd = ctx.get("cwd")
    cwd = Path(str(_cwd)) if _cwd is not None else sandbox_root
    elevated = bool(ctx.get("elevated", False))
    if ctx.get("sandboxed") and not elevated:
        # tr_execute_in_sandbox: sandbox-cwd shell command — the exec
        # backend decides WHERE it runs.
        return await exec_backend.run(command, cwd, timeout)
    # tr_execute: the host's native shell (L3 channel), and any elevated
    # run — host-side by definition.
    return await _exec.execute_run(command, cwd, timeout, elevated=elevated)
