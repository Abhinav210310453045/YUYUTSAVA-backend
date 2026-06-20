"""
LangChain @tool wrappers that expose TaskRunnerAgent as callable tools.

Use ``bind_tools(workspace_root)`` to get the four tools bound to a specific
workspace. The factory is the only public API here — callers should never
import the tool functions directly, because they are closures that need a
workspace_root to function correctly.

The module keeps a registry so the same TaskRunnerAgent instance is reused
for each workspace_root path, avoiding redundant instantiation.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shlex
import uuid
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, tool
from langgraph.types import interrupt

from yuyutsava.agents.task_runner.agent import TaskRunnerAgent
from yuyutsava.models.operations import OperationRequest, OperationType

_log = logging.getLogger("yuyutsava.agents.task_runner.tools")


def _resolve_path(raw: str, workspace_root: Path) -> str:
    """Normalize a tr_* path argument.

    Contract: tr_* tools take REAL absolute paths. Relative paths are
    resolved against workspace_root explicitly (not against process cwd).
    Non-existent absolute paths surface as clean FileNotFoundError downstream
    rather than being silently rewritten.
    """
    if not raw:
        return raw
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        expanded = str(workspace_root.resolve() / expanded)
    return os.path.normpath(expanded)


# ---------------------------------------------------------------------------
# Download verification (used by tr_fetch_url)
# ---------------------------------------------------------------------------

# Browser-ish UA so plain hosts that 403 the curl default still serve the file.
_FETCH_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# Leading magic bytes per file type. Empty list = no strict signature (accept any
# non-empty, non-HTML body). mp4 is special-cased (ftyp box at offset 4).
_MAGIC: dict[str, list[bytes]] = {
    "zip":  [b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"],
    "pdf":  [b"%PDF-"],
    "jpg":  [b"\xff\xd8\xff"],
    "jpeg": [b"\xff\xd8\xff"],
    "png":  [b"\x89PNG\r\n\x1a\n"],
    "gif":  [b"GIF87a", b"GIF89a"],
    "mp3":  [b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"\xff\xfa"],
    "gz":   [b"\x1f\x8b"],
    "wav":  [b"RIFF"],
    "ogg":  [b"OggS"],
    "flac": [b"fLaC"],
    "7z":   [b"7z\xbc\xaf\x27\x1c"],
    "rar":  [b"Rar!\x1a\x07"],
    "doc":  [b"\xd0\xcf\x11\xe0"],  # legacy OLE (.doc/.xls/.ppt)
    "mp4":  [],
}


def _looks_like_html(head: bytes) -> bool:
    """Heuristic: does *head* look like an HTML page (incl. Cloudflare/login walls)?"""
    low = head.lstrip().lower()
    if low.startswith((b"<!doctype html", b"<html", b"<head")):
        return True
    return (
        b"just a moment" in low
        or b"cf-browser-verification" in low
        or b"attention required" in low
        or b"enable javascript and cookies" in low
    )


def _verify_download(path: str, expected_type: str) -> tuple[bool, str, str | None]:
    """Verify a downloaded file is real: non-empty and matching its expected type.

    Returns ``(ok, reason, detected_type)``. Catches the common failure where a
    server returns a ``200 OK`` HTML interstitial (Cloudflare "Just a moment…")
    or a 0-byte body that ``curl`` happily saves as e.g. ``sample.zip``.
    """
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return False, f"file missing after download: {exc}", None
    if size == 0:
        return False, "downloaded file is empty (0 bytes)", None

    with open(path, "rb") as fh:
        head = fh.read(1024)

    ext = (expected_type or "").lower()
    if ext in ("", "auto"):
        ext = os.path.splitext(path)[1].lstrip(".").lower()

    is_html = _looks_like_html(head)

    if ext in _MAGIC:
        if is_html:
            return False, (
                f"expected a {ext} file but received an HTML page "
                f"(likely a Cloudflare/login interstitial)"
            ), "html"
        if ext == "mp4":
            ok = head[4:8] == b"ftyp"
        elif not _MAGIC[ext]:
            ok = True
        else:
            ok = any(head.startswith(sig) for sig in _MAGIC[ext])
        if not ok:
            return False, f"content does not look like a valid {ext} (magic-byte check failed)", "unknown"
        return True, "", ext

    # Text / unknown types: reject only an obvious HTML interstitial when HTML
    # was not what we asked for.
    if ext not in ("html", "htm") and is_html:
        return False, (
            f"expected {ext or 'a data file'} but received an HTML page (likely an interstitial)"
        ), "html"
    return True, "", ext or "binary"


# ---------------------------------------------------------------------------
# TaskRunnerAgent registry — one instance per resolved workspace root
# ---------------------------------------------------------------------------

_registry: dict[str, TaskRunnerAgent] = {}
_default_policy: object | None = None  # PermissionsPolicy; set by daemon at boot
_default_consent: object | None = None  # consent.ConsentRegistry; set at boot


def _validation_error_json(exc: Exception) -> str:
    """Return a structured JSON error so the LLM sees `status: error` instead of a
    langchain `Error invoking tool ...` string it tends to ignore.
    Attached as ``handle_validation_error=`` on every tr_* @tool so pydantic
    arg-validation failures (missing reason=, wrong type, etc.) become a normal
    OperationResponse-shaped result the model can react to.
    """
    msg = str(exc).replace("\n", " ")
    return json.dumps({
        "status": "error",
        "error_code": "TR000_VALIDATION",
        "error": f"Tool arguments invalid: {msg}",
        "hint": (
            "All tr_* tools require a non-empty `reason` string describing why the "
            "operation is needed. Re-call the tool with every required argument."
        ),
    })


def set_default_policy(policy: object | None) -> None:
    """Install a permissions policy for every TaskRunnerAgent the registry mints.

    The daemon calls this once at startup. Already-cached agents are updated
    in place so a hot reload picks up the new policy without rebuilding tools.
    """
    global _default_policy
    _default_policy = policy
    for agent in _registry.values():
        agent._policy = policy  # type: ignore[attr-defined]


def set_default_consent(consent: object | None) -> None:
    """Install the consent (allowlist) registry for every minted TaskRunnerAgent.

    Mirrors :func:`set_default_policy`: the daemon/CLI calls this once at startup
    and already-cached agents are updated in place so the allowlist is shared by
    every ``tr_*`` tool call.
    """
    global _default_consent
    _default_consent = consent
    for agent in _registry.values():
        agent._consent = consent  # type: ignore[attr-defined]


def _get_or_create_agent(workspace_root: Path, sandbox_root: Path | None = None) -> TaskRunnerAgent:
    ws = str(workspace_root.resolve())
    sb = str(sandbox_root.resolve()) if sandbox_root is not None else ""
    key = f"{ws}|{sb}"
    if key not in _registry:
        _registry[key] = TaskRunnerAgent(
            workspace_root, sandbox_root=sandbox_root,
            policy=_default_policy, consent=_default_consent,
        )
    return _registry[key]


# ---------------------------------------------------------------------------
# Tool factory — creates the 4 tools bound to workspace_root
# ---------------------------------------------------------------------------


def bind_tools(
    workspace_root: Path,
    sandbox_root: Path | None = None,
    *,
    agent_name: str = "agent",
) -> list[BaseTool]:
    """
    Return the TaskRunner tools bound to *workspace_root*.

    The tools are:
      - tr_read_file          — read any file (zone-checked)
      - tr_write_file         — write/create a file (zone-checked)
      - tr_delete_file        — delete a file or directory (zone-checked)
      - tr_execute_in_sandbox — run a shell command inside the sandbox zone
      - tr_ask_user           — ask the user a question and get their text response

    Each file/shell tool returns a JSON string (OperationResponse.model_dump_json())
    so the calling LLM sees a structured, parseable result in its ToolMessage.

    ``agent_name`` flows into ``OperationRequest.requesting_agent`` and is used
    by the HITL machinery to append ``/<agent_name>`` to ``agent_path`` in
    every interrupt payload — so the UI knows which subagent is asking.
    """
    agent = _get_or_create_agent(workspace_root, sandbox_root)

    # ------------------------------------------------------------------ #
    # tr_read_file                                                         #
    # ------------------------------------------------------------------ #

    @tool
    async def tr_read_file(
        path: str,
        reason: str,
        offset: int = 0,
        limit: int | None = None,
    ) -> str:
        """Read a file (zone-checked). Returns JSON {content, has_more, truncation_notice, total_lines}.

        Paginate large files via offset/limit; result.has_more + result.truncation_notice
        give the next offset. After tr_grep, feed the matched line number as offset.

        Args:
            path:   Absolute real path (convert virtual ls/glob paths first).
            reason: Specific purpose shown to the user in permission prompts.
            offset: 0-based line to start from (default 0).
            limit:  Max lines per call (None = read to EOF).
        """
        real_path = _resolve_path(path, workspace_root)
        _log.debug("[tr_read_file] path=%s offset=%s limit=%s", real_path, offset, limit)
        request = OperationRequest(
            request_id=str(uuid.uuid4()),
            requesting_agent=agent_name,
            task_id=str(uuid.uuid4()),
            task_description=reason,
            operation=OperationType.READ,
            paths=[real_path],
            reason=reason,
            additional_context={"offset": offset, "limit": limit},
        )
        response = await agent.handle(request)
        _log.debug("[tr_read_file] status=%s", response.status)
        return response.model_dump_json()

    # ------------------------------------------------------------------ #
    # tr_write_file                                                        #
    # ------------------------------------------------------------------ #

    @tool
    async def tr_write_file(path: str, content: str, reason: str) -> str:
        """Write/create a file (zone-checked, creates parent dirs). Returns JSON {status, result: {written_to}, error}.

        First write into the sandbox creates the sandbox dir.
        Deliverables → output_dir (from system prompt); scratch → sandbox.

        Args:
            path: Absolute real path to write.
            content: Text content to write.
            reason: Specific purpose shown to user in permission prompts.
        """
        real_path = _resolve_path(path, workspace_root)
        _log.debug("[tr_write_file] path=%s bytes=%d", real_path, len(content.encode()))
        request = OperationRequest(
            request_id=str(uuid.uuid4()),
            requesting_agent=agent_name,
            task_id=str(uuid.uuid4()),
            task_description=reason,
            operation=OperationType.WRITE,
            paths=[real_path],
            reason=reason,
            additional_context={"content": content},
        )
        response = await agent.handle(request)
        _log.debug("[tr_write_file] status=%s", response.status)
        return response.model_dump_json()

    # ------------------------------------------------------------------ #
    # tr_delete_file                                                       #
    # ------------------------------------------------------------------ #

    @tool
    async def tr_delete_file(path: str, reason: str) -> str:
        """Delete a file or directory (zone-checked). Returns JSON {status, result: {deleted}, error}.

        Use to clean up temp scripts after tr_execute_in_sandbox.
        WORKSPACE zone prompts the user; SANDBOX is auto-allowed.

        Args:
            path: Absolute real path to delete.
            reason: Specific purpose shown to user in permission prompts.
        """
        real_path = _resolve_path(path, workspace_root)
        _log.debug("[tr_delete_file] path=%s", real_path)
        request = OperationRequest(
            request_id=str(uuid.uuid4()),
            requesting_agent=agent_name,
            task_id=str(uuid.uuid4()),
            task_description=reason,
            operation=OperationType.DELETE,
            paths=[real_path],
            reason=reason,
        )
        response = await agent.handle(request)
        _log.debug("[tr_delete_file] status=%s", response.status)
        return response.model_dump_json()

    # ------------------------------------------------------------------ #
    # tr_execute_in_sandbox                                                #
    # ------------------------------------------------------------------ #

    @tool
    async def tr_execute_in_sandbox(
        command: str,
        reason: str,
        timeout: int = 120,
    ) -> str:
        """Run a shell command in the sandbox (auto-allowed, cwd=_sandbox/). Returns JSON {status, result: {stdout, stderr, exit_code}, error}.

        No network. CWD = sandbox dir; use relative paths.
        Sandbox dir is created by the first tr_write_file — do not call this before any write.
        Script lifecycle: tr_write_file → this → read result.stdout → tr_delete_file.
        Do NOT tr_read_file a script you just wrote; read the execution result.

        Args:
            command: Shell command to run.
            reason: Why you are running this command.
            timeout: Max seconds (default 120).
        """
        sandbox_path = str(agent.sandbox_root)
        _log.debug("[tr_execute_in_sandbox] cmd=%s", command[:200])
        request = OperationRequest(
            request_id=str(uuid.uuid4()),
            requesting_agent=agent_name,
            task_id=str(uuid.uuid4()),
            task_description=reason,
            operation=OperationType.EXECUTE,
            paths=[sandbox_path],
            reason=reason,
            additional_context={
                "command": command,
                "timeout": timeout,
                "cwd": sandbox_path,
            },
        )
        response = await agent.handle(request)
        _log.debug("[tr_execute_in_sandbox] status=%s", response.status)
        return response.model_dump_json()

    # ------------------------------------------------------------------ #
    # tr_grep                                                              #
    # ------------------------------------------------------------------ #

    @tool
    async def tr_grep(
        pattern: str,
        path: str,
        reason: str,
        context_lines: int = 3,
        case_insensitive: bool = False,
        max_matches: int = 100,
    ) -> str:
        """Search a regex pattern in a file or directory. Returns JSON with stdout (matches + line numbers).

        Use this, NOT the built-in grep (which only works on virtual paths).
        Pass real absolute paths; returned line numbers feed tr_read_file offset.

        Args:
            pattern:          Regex or fixed string to search for.
            path:             Real absolute path to a file or directory.
            reason:           Specific purpose shown to the user in permission prompts.
            context_lines:    Lines of context before/after each match (default 3).
            case_insensitive: Case-insensitive matching (default False).
            max_matches:      Stop after this many matches (default 100).
        """
        real_path = _resolve_path(path, workspace_root)
        _log.debug("[tr_grep] pattern=%r path=%s", pattern, real_path)
        flags = "-rn"
        if case_insensitive:
            flags += "i"
        cmd = (
            f"grep {flags} -C {context_lines} -m {max_matches} "
            f"--color=never -- {json.dumps(pattern)} {json.dumps(real_path)}"
        )
        sandbox_path = str(agent.sandbox_root)
        request = OperationRequest(
            request_id=str(uuid.uuid4()),
            requesting_agent=agent_name,
            task_id=str(uuid.uuid4()),
            task_description=reason,
            operation=OperationType.EXECUTE,
            paths=[sandbox_path],
            reason=reason,
            additional_context={
                "command": cmd,
                "timeout": 30,
                "cwd": sandbox_path,
            },
        )
        response = await agent.handle(request)
        return response.model_dump_json()

    # ------------------------------------------------------------------ #
    # tr_ask_user                                                          #
    # ------------------------------------------------------------------ #

    @tool
    async def tr_ask_user(
        question: str,
        options: list[str] | None = None,
    ) -> str:
        """Ask the user a question and return their text response.

        Use this when you need clarification, a choice between approaches, or
        explicit confirmation before an irreversible action. The question is
        shown to the user on their terminal and their answer is returned.

        Args:
            question: The question to show the user.
            options: Optional list of suggested responses shown as hints (not enforced).

        Returns:
            JSON: {status, result: {response: <user's answer>}}
        """
        from yuyutsava.core.agent_context import current_context

        ctx = current_context()
        parent_path = ctx.get("agent_path") or "orchestrator"
        # Override agent_path so the UI knows which subagent (not just "orchestrator")
        # is the asker. When agent_name is the default "agent" (no subagent), keep
        # the parent path unchanged.
        if agent_name and agent_name != "agent" and not parent_path.endswith(f"/{agent_name}"):
            ctx = {**ctx, "agent_path": f"{parent_path}/{agent_name}"}
        payload = {
            "type": "user_question",
            "question": question,
            "options": options or [],
            **ctx,
        }
        response: str = interrupt(payload)
        return json.dumps({"status": "success", "result": {"response": response}})

    # ------------------------------------------------------------------ #
    # tr_ls                                                                #
    # ------------------------------------------------------------------ #

    @tool
    async def tr_ls(path: str, reason: str, max_entries: int = 500) -> str:
        """List directory entries at a real absolute path (zone-checked).

        Returns JSON {entries: [{name, path, type, size}], total, has_more}.
        Use this for ANY directory listing, including inside the workspace —
        the built-in ls is not available. WORKSPACE/SANDBOX zones auto-allow;
        EXTERNAL paths prompt the user once.

        Args:
            path:        Real absolute path of the directory to list.
            reason:      Specific purpose shown to the user in permission prompts.
            max_entries: Cap on returned entries (default 500).
        """
        real_path = _resolve_path(path, workspace_root)
        _log.debug("[tr_ls] path=%s max=%d", real_path, max_entries)
        request = OperationRequest(
            request_id=str(uuid.uuid4()),
            requesting_agent=agent_name,
            task_id=str(uuid.uuid4()),
            task_description=reason,
            operation=OperationType.LIST,
            paths=[real_path],
            reason=reason,
            additional_context={"max_entries": max_entries},
        )
        response = await agent.handle(request)
        _log.debug("[tr_ls] status=%s", response.status)
        return response.model_dump_json()

    # ------------------------------------------------------------------ #
    # tr_glob                                                              #
    # ------------------------------------------------------------------ #

    @tool
    async def tr_glob(
        pattern: str,
        path: str,
        reason: str,
        max_entries: int = 500,
    ) -> str:
        """Glob files matching a pattern under a real absolute path (zone-checked).

        Returns JSON {entries: [{name, path, type, size}], total, has_more, pattern, root}.
        Patterns use pathlib semantics: '*.pdf' shallow, '**/*.pdf' recursive.
        Use this for ANY pattern match, including inside the workspace — the
        built-in glob is not available. EXTERNAL paths prompt the user once.

        Args:
            pattern:     Glob pattern (e.g. '*.pdf', '**/*.py', 'README*').
            path:        Real absolute path of the root directory to search.
            reason:      Specific purpose shown to the user in permission prompts.
            max_entries: Cap on returned entries (default 500).
        """
        real_path = _resolve_path(path, workspace_root)
        _log.debug("[tr_glob] pattern=%r path=%s max=%d", pattern, real_path, max_entries)
        request = OperationRequest(
            request_id=str(uuid.uuid4()),
            requesting_agent=agent_name,
            task_id=str(uuid.uuid4()),
            task_description=reason,
            operation=OperationType.GLOB,
            paths=[real_path],
            reason=reason,
            additional_context={"pattern": pattern, "max_entries": max_entries},
        )
        response = await agent.handle(request)
        _log.debug("[tr_glob] status=%s", response.status)
        return response.model_dump_json()

    # ------------------------------------------------------------------ #
    # tr_execute                                                           #
    # ------------------------------------------------------------------ #

    @tool
    async def tr_execute(
        command: str,
        reason: str,
        timeout: int = 120,
    ) -> str:
        """Run a shell command on the host (workspace cwd, full network, asks user every time).

        Use for internet-required commands (curl/wget/API). For local-only,
        use tr_execute_in_sandbox (no approval prompt, no network).

        Args:
            command: Shell command to run (e.g. "curl -s https://example.com").
            reason:  Why you need to run this (shown to user in permission prompt).
            timeout: Max seconds (default 120).
        """
        _log.debug("[tr_execute] cmd=%s", command[:200])
        # Use "/host" as the sentinel path — it is outside workspace and sandbox,
        # so classify_zone() returns EXTERNAL, and EXTERNAL + EXECUTE = PROMPT.
        # This forces a user permission check before every execution.
        request = OperationRequest(
            request_id=str(uuid.uuid4()),
            requesting_agent=agent_name,
            task_id=str(uuid.uuid4()),
            task_description=reason,
            operation=OperationType.EXECUTE,
            paths=["/host"],
            reason=reason,
            additional_context={
                "command": command,
                "timeout": timeout,
                "cwd": str(workspace_root),
            },
        )
        response = await agent.handle(request)
        _log.debug("[tr_execute] status=%s", response.status)
        return response.model_dump_json()

    @tool
    async def tr_fetch_url(
        url: str,
        dest_path: str,
        reason: str,
        expected_type: str = "auto",
        timeout: int = 120,
    ) -> str:
        """Download a URL to a workspace file AND verify it is a real file.

        Prefer this over tr_execute with raw curl/wget for downloads. It follows
        redirects, sends a browser User-Agent, and after downloading checks the
        bytes (non-empty + magic-byte / HTML-interstitial check). A Cloudflare
        "Just a moment…" page or a 0-byte body is reported as status=error and the
        partial file is removed — so you retry a different source instead of
        leaving a corrupt "downloaded" file behind.

        Args:
            url:           File URL to download.
            dest_path:     Where to save it (absolute, or relative to the workspace).
            reason:        Why you need it (shown in the permission prompt).
            expected_type: Type to verify, e.g. "zip"/"pdf"/"jpg"/"mp3"/"csv".
                           "auto" (default) infers from the dest_path extension.
            timeout:       Max seconds (default 120).
        """
        dest = _resolve_path(dest_path, workspace_root)
        command = (
            f"curl -fSL --retry 2 --max-time {int(timeout)} "
            f"-A {shlex.quote(_FETCH_UA)} "
            f"-o {shlex.quote(dest)} {shlex.quote(url)}"
        )
        request = OperationRequest(
            request_id=str(uuid.uuid4()),
            requesting_agent=agent_name,
            task_id=str(uuid.uuid4()),
            task_description=reason,
            operation=OperationType.EXECUTE,
            paths=["/host"],
            reason=reason,
            additional_context={"command": command, "timeout": timeout, "cwd": str(workspace_root)},
        )
        response = await agent.handle(request)
        # Denied / rule error — pass the structured response straight through.
        if response.status != "success":
            return response.model_dump_json()

        result = response.result
        exit_code = int(getattr(result, "exit_code", 0) or 0)
        if exit_code != 0:
            stderr = (getattr(result, "stderr", "") or "")[:300]
            with contextlib.suppress(OSError):
                if os.path.exists(dest):
                    os.remove(dest)
            return json.dumps({
                "status": "error",
                "error_code": "TR_FETCH_HTTP",
                "error": f"Download failed (curl exit {exit_code}). {stderr}".strip(),
                "url": url,
                "hint": "URL may be dead, blocked, or need auth. Try a direct/raw host "
                        "(e.g. raw.githubusercontent.com, archive.org).",
            })

        ok, why, detected = _verify_download(dest, expected_type)
        if not ok:
            with contextlib.suppress(OSError):
                os.remove(dest)
            return json.dumps({
                "status": "error",
                "error_code": "TR_FETCH_INVALID",
                "error": f"Downloaded file failed verification: {why}",
                "url": url,
                "detected": detected,
                "hint": "Server returned an interstitial or wrong content. Pick a "
                        "direct-download source and retry.",
            })

        size = None
        with contextlib.suppress(OSError):
            size = os.path.getsize(dest)
        _log.debug("[tr_fetch_url] ok url=%s dest=%s bytes=%s", url[:120], dest, size)
        return json.dumps({
            "status": "success",
            "path": dest,
            "bytes": size,
            "verified_as": detected,
            "url": url,
        })

    all_tools: list[BaseTool] = [
        tr_read_file, tr_write_file, tr_delete_file,
        tr_execute_in_sandbox, tr_grep, tr_ls, tr_glob,
        tr_ask_user, tr_execute, tr_fetch_url,
    ]
    # Convert pydantic arg-validation failures (missing reason=, wrong type, etc.)
    # into a structured JSON ToolMessage instead of langchain's opaque
    # "Error invoking tool ..." string the LLM tends to ignore.
    for t in all_tools:
        t.handle_validation_error = _validation_error_json
    return all_tools
