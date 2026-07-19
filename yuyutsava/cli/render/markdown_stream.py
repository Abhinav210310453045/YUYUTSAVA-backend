"""Block-committing markdown streamer for the rich REPL.

Streams assistant prose the way aider's ``mdstream`` does: tokens
accumulate in a buffer, and whenever a markdown *block* completes (a
blank-line boundary outside an open ``` fence) that block is permanently
printed with ``console.print(Markdown(...))``. Committed output is pure
append — scrollback stays intact — while the in-progress tail is exposed
for the renderer's transient Live region.

Chosen over ``Live(Markdown(whole_message))`` (which can't exceed the
terminal height and flickers on long answers) and over "re-render at the
end" (which needs fragile erase-N-lines cursor math).
"""

from __future__ import annotations

from rich.console import Console
from rich.markdown import Markdown


def _committable_length(buf: str) -> int:
    """Chars of ``buf`` that form complete blocks safe to commit.

    A block boundary is a blank line (``\\n\\n``) that is not inside an
    open ``` fence. Returns 0 when no complete block exists yet.
    """
    commit_end = 0
    in_fence = False
    pos = 0
    for line in buf.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
        pos += len(line)
        # A terminated blank line outside a fence closes the block that
        # precedes it (the blank line itself is committed too, keeping
        # the remainder's parsing identical to the full text's).
        if not in_fence and stripped == "" and line.endswith("\n"):
            commit_end = pos
    return commit_end


class MarkdownStream:
    """Feed tokens in, get committed markdown blocks + a live tail out."""

    def __init__(self, console: Console) -> None:
        self._console = console
        self._buf = ""
        self.started = False  # True once any prose was fed this turn

    def feed(self, text: str) -> None:
        if not text:
            return
        self.started = True
        self._buf += text
        end = _committable_length(self._buf)
        if end:
            self._commit(self._buf[:end])
            self._buf = self._buf[end:]

    def tail(self, max_lines: int = 3) -> str:
        """Last few lines of the uncommitted buffer (for the Live region)."""
        lines = self._buf.rstrip("\n").splitlines()
        return "\n".join(lines[-max_lines:])

    def flush(self) -> None:
        """Commit everything buffered (e.g. before a tool-call line)."""
        if self._buf.strip():
            self._commit(self._buf)
        self._buf = ""

    def finish(self) -> None:
        """End of turn: commit the remainder and reset for the next turn."""
        self.flush()
        self.started = False

    def _commit(self, text: str) -> None:
        if not text.strip():
            return
        self._console.print(Markdown(text))
