"""
Standardized structured messages embedded in tool results.

Tool results are JSON objects (OperationResponse). When a result field
contains content that cannot or should not be passed to the LLM in full
(too large, binary, etc.) the system replaces that field with a typed
``ToolNotice``.  Future notice types (pagination cursors, warnings,
confirmations) can be added here following the same pattern.

Using a typed model instead of ad-hoc strings means:
  - The LLM always sees the same JSON structure and can act on it reliably.
  - Middleware can detect and react to notice types programmatically,
    e.g. auto-retry with pagination instead of forwarding a suppression notice.
  - Log aggregation can count and categorize notices consistently.
  - New notice types are easy to add without changing call sites.

Notice types
------------
SuppressedContentNotice
    Content was too large or non-renderable.  Includes recovery hints so the
    LLM knows exactly what to do next (paginate, grep, redirect to file, etc.).

RecoveryHint
    A single concrete action the LLM can take to recover from a notice.
    Listed in priority order (first = recommended, rest = fallbacks).
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Shared building blocks
# ---------------------------------------------------------------------------


class RecoveryHint(BaseModel):
    """A single concrete recovery action for the LLM to act on."""

    action:      str             # short imperative label: "paginate", "grep", "redirect_to_file"
    description: str             # plain-English instruction
    example:     str | None = None  # optional tool-call snippet or shell command


# ---------------------------------------------------------------------------
# Notice types
# ---------------------------------------------------------------------------


class SuppressedReason(str, Enum):
    FILE_TOO_LARGE   = "file_too_large"    # tr_read_file: file exceeds per-read char limit
    STDOUT_TOO_LARGE = "stdout_too_large"  # tr_execute_in_sandbox: stdout overflow
    BINARY_CONTENT   = "binary_content"   # non-text / non-UTF-8 file
    UNKNOWN          = "unknown"           # catch-all for unexpected cases


class SuppressedContentNotice(BaseModel):
    """
    Replaces bulk content in a tool result when the output exceeds safe limits.

    The ``notice_type`` field lets middleware and the LLM distinguish this from
    other notice types without inspecting every field.
    """

    notice_type:         Literal["suppressed_content"] = "suppressed_content"
    suppressed:          Literal[True] = True   # fast boolean check for middleware
    reason:              SuppressedReason
    original_size_chars: int
    tool:                str
    recovery:            list[RecoveryHint]
    human_message:       str

    # ── Factories ──────────────────────────────────────────────────────────

    @classmethod
    def file_too_large(
        cls,
        *,
        tool: str,
        path: str,
        original_size_chars: int,
        total_lines: int,
        shown_lines: int,
    ) -> "SuppressedContentNotice":
        """Notice for tr_read_file when the file content exceeds the per-read limit."""
        return cls(
            reason=SuppressedReason.FILE_TOO_LARGE,
            original_size_chars=original_size_chars,
            tool=tool,
            human_message=(
                f"File content truncated: returned {shown_lines:,} of {total_lines:,} lines "
                f"({original_size_chars:,} chars total). Use offset/limit to read the rest."
            ),
            recovery=[
                RecoveryHint(
                    action="paginate",
                    description=(
                        f"Call tr_read_file again with offset={shown_lines} to read the next "
                        f"chunk. Repeat until offset >= {total_lines}."
                    ),
                    example=f'tr_read_file(path="{path}", offset={shown_lines}, reason="read next chunk")',
                ),
                RecoveryHint(
                    action="grep",
                    description=(
                        "Use tr_grep to search for specific symbols or patterns without "
                        "reading the entire file."
                    ),
                    example=f'tr_grep(pattern="def ", path="{path}", reason="find function definitions")',
                ),
            ],
        )

    @classmethod
    def stdout_too_large(
        cls,
        *,
        tool: str,
        command: str,
        original_size_chars: int,
    ) -> "SuppressedContentNotice":
        """Notice for tr_execute_in_sandbox when stdout overflows the context limit."""
        safe_cmd = command[:120] + ("…" if len(command) > 120 else "")
        return cls(
            reason=SuppressedReason.STDOUT_TOO_LARGE,
            original_size_chars=original_size_chars,
            tool=tool,
            human_message=(
                f"Command stdout was {original_size_chars:,} chars — too large to pass to "
                f"the LLM context. Redirect output to a file and read it in chunks."
            ),
            recovery=[
                RecoveryHint(
                    action="redirect_to_file",
                    description=(
                        "Rerun the command redirecting stdout to a sandbox file, then use "
                        "tr_read_file with offset/limit to read it in chunks."
                    ),
                    example=f'tr_execute_in_sandbox(command="{safe_cmd} > output.txt", reason="...")',
                ),
                RecoveryHint(
                    action="pipe_to_head",
                    description="Pipe the command through head/tail/grep to reduce output size.",
                    example=f'tr_execute_in_sandbox(command="{safe_cmd} | head -200", reason="...")',
                ),
            ],
        )

    @classmethod
    def binary_content(
        cls,
        *,
        tool: str,
        path: str,
        original_size_chars: int,
    ) -> "SuppressedContentNotice":
        """Notice for binary or non-UTF-8 files that cannot be displayed as text."""
        return cls(
            reason=SuppressedReason.BINARY_CONTENT,
            original_size_chars=original_size_chars,
            tool=tool,
            human_message=(
                f"'{path}' contains binary or non-UTF-8 content and cannot be rendered as "
                f"text ({original_size_chars:,} chars). Inspect the file type first."
            ),
            recovery=[
                RecoveryHint(
                    action="inspect_type",
                    description="Run `file <path>` in the sandbox to identify the file type.",
                    example=f'tr_execute_in_sandbox(command="file \'{path}\'", reason="identify file type")',
                ),
            ],
        )


# ---------------------------------------------------------------------------
# Union of all notice types (extend here as new types are added)
# ---------------------------------------------------------------------------

ToolNotice = SuppressedContentNotice


def is_tool_notice(value: Any) -> bool:
    """Return True if *value* is a serialized ToolNotice dict (any notice type)."""
    if isinstance(value, dict):
        return "notice_type" in value
    if isinstance(value, SuppressedContentNotice):
        return True
    return False
