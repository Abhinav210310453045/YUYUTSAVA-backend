"""One line that says what a chat's start is doing, so the terminal never looks frozen.

From the moment ``yuyutsava chat`` is typed until the banner, the start loads
the code, opens storage, starts the background helper and builds the agent —
seconds with nothing on screen, which reads as a hang. This draws one line on
the terminal with a spinner and the step in plain words ("Opening your saved
conversations…"), and clears it before the banner. A step that runs long gets
a short hint after a few seconds.

* Only on a terminal (stderr is a TTY); a pipe or a script gets nothing.
* It writes to its own copy of the terminal's stderr, taken when it starts, so
  the build's ``_suppress_stdio`` (which silences the LangGraph host's own
  output) does not silence it too.
* It animates from its own thread (Rich's live display), so it keeps moving
  while the start is busy importing or waiting on a subprocess.
* ``pause``/``resume`` step aside for anything that asks a question (the setup
  offer, the wizard); ``done`` clears it.
* Every step's duration is logged at DEBUG (``-v``), so a slow start names its
  slow step instead of leaving it to guesswork.

One per process: :data:`status`.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import IO, Any

from rich.console import Console
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text

logger = logging.getLogger("yuyutsava.cli.startup_status")

#: How long a step runs before the line adds that it is still working.
SLOW_AFTER_SECONDS = 4.0

#: Said under a step that runs long, unless the step brings its own.
_STILL_WORKING = "still working — this can take a few seconds the first time"


class StartupStatus:
    """The start-up line: one per process (:data:`status`)."""

    def __init__(self, *, stream: IO[str] | None = None, clock: Any = time.monotonic) -> None:
        self._stream = stream
        self._clock = clock
        self._live: Any = None
        self._text = ""
        self._hint = ""
        self._step_started = 0.0
        self._started = 0.0
        self._paused = False

    @property
    def active(self) -> bool:
        return self._live is not None

    def start(self, text: str) -> None:
        """Show the line, if stderr is a terminal and it is not showing already."""
        if self._live is not None:
            self.step(text)
            return
        stream = self._stream or _terminal_copy()
        if stream is None:
            self._started = self._step_started = self._clock()
            self._text = text
            return
        self._started = self._step_started = self._clock()
        self._text = text
        console = Console(file=stream, force_terminal=True)
        self._live = Live(
            _Line(self),
            console=console,
            transient=True,
            refresh_per_second=10,
            redirect_stdout=False,
            redirect_stderr=False,
        )
        self._live.start()

    def step(self, text: str, *, slow_hint: str = "") -> None:
        """Say what the start is doing now."""
        now = self._clock()
        if self._text:
            logger.debug("start-up: %s %.1fs", self._text.rstrip("…"), now - self._step_started)
        self._text, self._hint, self._step_started = text, slow_hint, now

    def pause(self) -> None:
        """Step aside: something is about to ask a question or print."""
        if self._live is not None and not self._paused:
            self._live.stop()
            self._paused = True

    def resume(self) -> None:
        if self._live is not None and self._paused:
            self._live.start()
            self._paused = False

    def done(self) -> None:
        """Clear the line for good: the chat is ready."""
        now = self._clock()
        if self._text:
            logger.debug("start-up: %s %.1fs", self._text.rstrip("…"), now - self._step_started)
            logger.debug("start-up: ready after %.1fs", now - self._started)
        self._text = ""
        if self._live is not None:
            if not self._paused:
                self._live.stop()
            self._live = None
            self._paused = False

    # What _Line draws.
    def _render(self) -> Any:
        line = Text(self._text)
        if self._clock() - self._step_started > SLOW_AFTER_SECONDS:
            line.append(f"  {self._hint or _STILL_WORKING}", style="dim")
        return Spinner("dots", text=line)


class _Line:
    """Rich asks this for the line on every refresh, so the hint appears on time."""

    def __init__(self, owner: StartupStatus) -> None:
        self._owner = owner

    def __rich__(self) -> Any:
        return self._owner._render()


def _terminal_copy() -> IO[str] | None:
    """A copy of the terminal's stderr that survives the build silencing fd 2; None off a terminal."""
    try:
        if not os.isatty(sys.stderr.fileno()):
            return None
        return os.fdopen(os.dup(sys.stderr.fileno()), "w", buffering=1)
    except (OSError, ValueError, AttributeError):
        return None


#: This process's start-up line.
status = StartupStatus()

__all__ = ["SLOW_AFTER_SECONDS", "StartupStatus", "status"]
