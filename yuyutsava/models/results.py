"""
Typed result payloads for completed filesystem/shell operations.

These replace the bare `result: Any` field on OperationResponse with a
discriminated union so callers always know exactly what fields are available.

  ShellResult   — stdout/stderr/exit_code from tr_execute_in_sandbox
  WriteResult   — path confirmation from tr_write_file / CREATE
  DeleteResult  — path confirmation from tr_delete_file
  ReadResult    — raw text content from tr_read_file
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class ShellResult(BaseModel):
    """Result of a shell command execution (tr_execute_in_sandbox)."""

    kind:      Literal["shell"]  = "shell"
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
    """Result of a file read operation (tr_read_file)."""

    kind:    Literal["read"] = "read"
    content: str  # raw text content of the file
