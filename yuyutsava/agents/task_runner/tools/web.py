"""TaskRunner tools: Fetching things over HTTP.

Split out of the 609-line ``bind_tools`` (`R-RDB-02`); the factory in
``__init__`` assembles the families.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import uuid

from langchain_core.tools import BaseTool, tool

from yuyutsava.agents.task_runner.tools.support import (
    ToolContext,
    resolve_path,
)
from yuyutsava.models.operations import OperationRequest, OperationType

_log = logging.getLogger("yuyutsava.agents.task_runner.tools.web")


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
    "zip": [b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"],
    "pdf": [b"%PDF-"],
    "jpg": [b"\xff\xd8\xff"],
    "jpeg": [b"\xff\xd8\xff"],
    "png": [b"\x89PNG\r\n\x1a\n"],
    "gif": [b"GIF87a", b"GIF89a"],
    "mp3": [b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"\xff\xfa"],
    "gz": [b"\x1f\x8b"],
    "wav": [b"RIFF"],
    "ogg": [b"OggS"],
    "flac": [b"fLaC"],
    "7z": [b"7z\xbc\xaf\x27\x1c"],
    "rar": [b"Rar!\x1a\x07"],
    "doc": [b"\xd0\xcf\x11\xe0"],  # legacy OLE (.doc/.xls/.ppt)
    "mp4": [],
}


def looks_like_html(head: bytes) -> bool:
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


def verify_download(path: str, expected_type: str) -> tuple[bool, str, str | None]:
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

    is_html = looks_like_html(head)

    if ext in _MAGIC:
        if is_html:
            return (
                False,
                (
                    f"expected a {ext} file but received an HTML page "
                    f"(likely a Cloudflare/login interstitial)"
                ),
                "html",
            )
        if ext == "mp4":
            ok = head[4:8] == b"ftyp"
        elif not _MAGIC[ext]:
            ok = True
        else:
            ok = any(head.startswith(sig) for sig in _MAGIC[ext])
        if not ok:
            return (
                False,
                f"content does not look like a valid {ext} (magic-byte check failed)",
                "unknown",
            )
        return True, "", ext

    # Text / unknown types: reject only an obvious HTML interstitial when HTML
    # was not what we asked for.
    if ext not in ("html", "htm") and is_html:
        return (
            False,
            (f"expected {ext or 'a data file'} but received an HTML page (likely an interstitial)"),
            "html",
        )
    return True, "", ext or "binary"


def make_web_tools(ctx: ToolContext) -> list[BaseTool]:
    """Build the web tools bound to one workspace."""
    agent, workspace_root, agent_name = ctx.agent, ctx.workspace_root, ctx.agent_name

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
        dest = resolve_path(dest_path, workspace_root)
        # Pure-Python download (executor.execute_fetch via httpx) — routed through
        # EXECUTE on the /host sentinel so it still prompts the user once; no curl,
        # no shell quoting. The verification below is unchanged.
        request = OperationRequest(
            request_id=str(uuid.uuid4()),
            requesting_agent=agent_name,
            task_id=str(uuid.uuid4()),
            task_description=reason,
            operation=OperationType.EXECUTE,
            paths=["/host"],
            reason=reason,
            additional_context={
                "fetch": {
                    "url": url,
                    "dest": dest,
                    "user_agent": _FETCH_UA,
                    "timeout": timeout,
                },
                "cwd": str(workspace_root),
            },
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
            return json.dumps(
                {
                    "status": "error",
                    "error_code": "TR_FETCH_HTTP",
                    "error": f"Download failed (exit {exit_code}). {stderr}".strip(),
                    "url": url,
                    "hint": "URL may be dead, blocked, or need auth. Try a direct/raw host "
                    "(e.g. raw.githubusercontent.com, archive.org).",
                }
            )

        ok, why, detected = verify_download(dest, expected_type)
        if not ok:
            with contextlib.suppress(OSError):
                os.remove(dest)
            return json.dumps(
                {
                    "status": "error",
                    "error_code": "TR_FETCH_INVALID",
                    "error": f"Downloaded file failed verification: {why}",
                    "url": url,
                    "detected": detected,
                    "hint": "Server returned an interstitial or wrong content. Pick a "
                    "direct-download source and retry.",
                }
            )

        size = None
        with contextlib.suppress(OSError):
            size = os.path.getsize(dest)
        _log.debug("[tr_fetch_url] ok url=%s dest=%s bytes=%s", url[:120], dest, size)
        return json.dumps(
            {
                "status": "success",
                "path": dest,
                "bytes": size,
                "verified_as": detected,
                "url": url,
            }
        )

    return [
        tr_fetch_url,
    ]
