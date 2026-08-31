"""
Typed result payloads for completed filesystem/shell operations.

These replace the bare `result: Any` field on OperationResponse with a
discriminated union so callers always know exactly what fields are available.

  ShellResult   — stdout/stderr/exit_code from tr_execute_in_sandbox
  WriteResult   — path confirmation from tr_write_file / CREATE
  DeleteResult  — path confirmation from tr_delete_file
  ReadResult    — text content from tr_read_file (supports pagination)
  ListResult    — directory entries from tr_ls / tr_glob

When a result field would overflow the LLM context, the system replaces the
bulk content with a ``SuppressedContentNotice`` (see models/tool_messages.py).
The notice is embedded in the same JSON structure so the LLM always gets a
consistent, actionable response — never a silent truncation.
"""

from __future__ import annotations

from typing import Literal, Union

from pydantic import BaseModel

from yuyutsava.models.tool_messages import SuppressedContentNotice


class ShellResult(BaseModel):
    """Result of a shell command execution (tr_execute_in_sandbox)."""

    kind:      Literal["shell"] = "shell"
    stdout:    str
    stderr:    str
    exit_code: int


class WriteResult(BaseModel):
    """Result of a file write or create operation (tr_write_file)."""

    kind:       Literal["write"] = "write"
    written_to: str  # absolute path that was written


class DeleteResult(BaseModel):
    """Result of a file/directory delete operation (tr_delete_file)."""

    kind:    Literal["delete"] = "delete"
    deleted: str  # absolute path that was deleted


class ReadResult(BaseModel):
    """Result of a file read operation (tr_read_file).

    Supports pagination: if the file is larger than the per-read limit the
    content field contains the requested slice and a SuppressedContentNotice
    is embedded in ``truncation_notice`` so the LLM knows how to read the rest.

    Pagination fields
    -----------------
    offset : int
        0-based line number of the first line returned (matches the ``offset``
        param passed to tr_read_file).
    limit : int | None
        Maximum lines requested. None means "read to end of file".
    returned_lines : int
        Number of lines actually returned in ``content``.
    total_lines : int
        Total line count of the full file (independent of offset/limit).
    has_more : bool
        True when (offset + returned_lines) < total_lines — i.e. there are
        more lines after the current chunk.
    truncation_notice : SuppressedContentNotice | None
        Populated when has_more is True.  Contains recovery hints so the LLM
        knows exactly how to read the next chunk.
    """

    kind:               Literal["read"] = "read"
    content:            str
    offset:             int = 0
    limit:              int | None = None
    returned_lines:     int = 0
    total_lines:        int = 0
    has_more:           bool = False
    truncation_notice:  SuppressedContentNotice | None = None


class DirEntry(BaseModel):
    """Single entry in a directory listing or glob match."""

    name: str   # base name (e.g. "README.md")
    path: str   # absolute real path
    type: Literal["file", "dir", "symlink", "other"]
    size: int | None = None  # bytes for files; None for dirs/symlinks


class ListResult(BaseModel):
    """Result of a directory list (tr_ls) or glob match (tr_glob).

    ``entries`` holds up to ``returned`` items; ``total`` is the unfiltered
    count. When ``has_more`` is True the listing was truncated to keep the
    LLM payload small — the LLM should narrow the path or pattern.
    """

    kind:     Literal["list"] = "list"
    root:     str               # absolute real path of the directory searched
    pattern:  str | None = None # glob pattern (None for plain tr_ls)
    entries:  list[DirEntry] = []
    returned: int = 0
    total:    int = 0
    has_more: bool = False
