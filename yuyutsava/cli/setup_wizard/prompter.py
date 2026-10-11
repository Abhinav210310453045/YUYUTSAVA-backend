"""How the setup wizard asks: six questions, answered at a terminal or by a test.

The wizard's flow (:mod:`.flow`) talks only to :class:`Prompter`, so its every
branch can be driven by a script of answers; :class:`TerminalPrompter` is the
prompt_toolkit one people see, in the chat's colours.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from prompt_toolkit import prompt
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.shortcuts import choice
from prompt_toolkit.styles import Style

from yuyutsava.ansi import CHROME, DIM, KEY, OK, RESET, WARN, palette


@dataclass(frozen=True)
class Option:
    """One answer to pick: what it returns, how it reads, and a quieter hint beside it."""

    value: Any
    label: str
    hint: str = ""


class Prompter(Protocol):
    """The questions the wizard asks."""

    def say(self, text: str = "", *, tone: str = "") -> None:
        """Show a line; *tone* is ``""``, ``"ok"``, ``"warn"``, ``"dim"`` or ``"title"``."""
        ...

    def choose(self, question: str, options: Sequence[Option], *, default: Any = None) -> Any:
        """One option's value."""
        ...

    def choose_many(
        self, question: str, options: Sequence[Option], *, chosen: Sequence[Any] = ()
    ) -> list[Any]:
        """The values of any number of options; *chosen* are picked to begin with."""
        ...

    def ask(self, question: str, *, default: str = "") -> str:
        """A line of text; *default* when the answer is empty."""
        ...

    def secret(self, question: str) -> str:
        """A line of text that is never shown or kept in history."""
        ...

    def confirm(self, question: str, *, default: bool = True) -> bool:
        """Yes or no."""
        ...


_TONES = {"ok": OK, "warn": WARN, "dim": DIM, "title": KEY, "": ""}


class TerminalPrompter:
    """:class:`Prompter` at a terminal: arrow-key pickers, typed answers, hidden secrets."""

    def __init__(self) -> None:

        colours = palette()
        self._style = Style.from_dict(
            {
                "selected-option": f"bold {colours['you']}",
                "number": colours["chrome"],
                "frame.border": colours["chrome"],
                "question": f"bold {colours['key']}",
            }
        )

    def say(self, text: str = "", *, tone: str = "") -> None:
        colour = _TONES.get(tone, "")
        print(f"{colour}{text}{RESET}" if colour else text, file=sys.stderr)

    def choose(self, question: str, options: Sequence[Option], *, default: Any = None) -> Any:

        return choice(
            message=[("class:question", question)],
            options=[(o.value, self._label(o)) for o in options],
            default=default,
            style=self._style,
            symbol="❯",
        )

    def choose_many(
        self, question: str, options: Sequence[Option], *, chosen: Sequence[Any] = ()
    ) -> list[Any]:
        print(f"{KEY}{question}{RESET}", file=sys.stderr)
        for number, option in enumerate(options, 1):
            mark = f"{OK}●{RESET}" if option.value in chosen else f"{CHROME}○{RESET}"
            hint = f"  {DIM}{option.hint}{RESET}" if option.hint else ""
            print(f"  {mark} {CHROME}{number}.{RESET} {option.label}{hint}", file=sys.stderr)
        preset = ",".join(str(i) for i, o in enumerate(options, 1) if o.value in chosen)
        while True:
            answer = self.ask(
                "Type the numbers, e.g. 1 or 1,3 — Enter keeps the ones marked ●, 0 for none",
                default=preset,
            )
            picked = _numbers(answer, len(options))
            if picked is not None:
                return [options[i - 1].value for i in picked]
            self.say(f"  Please type numbers from 1 to {len(options)}, like 1 or 1,3", tone="warn")

    def ask(self, question: str, *, default: str = "") -> str:

        shown = f" {DIM}[{default}]{RESET}" if default else ""

        answer = prompt(ANSI(f"{KEY}{question}{RESET}{shown} {CHROME}❯{RESET} "))
        return answer.strip() or default

    def secret(self, question: str) -> str:
        return prompt(
            ANSI(f"{KEY}{question}{RESET} {DIM}(hidden as you type){RESET} {CHROME}❯{RESET} "),
            is_password=True,
        ).strip()

    def confirm(self, question: str, *, default: bool = True) -> bool:
        return self.choose(
            question,
            [Option(True, "yes"), Option(False, "no")],
            default=default,
        )

    @staticmethod
    def _label(option: Option) -> Any:
        if not option.hint:
            return option.label
        return [("", option.label), ("class:number", f"  {option.hint}")]


def _numbers(answer: str, count: int) -> list[int] | None:
    """``"1, 3"`` as ``[1, 3]``; ``"0"`` or nothing as ``[]``; ``None`` when out of range."""
    if answer.strip() in ("", "0"):
        return []
    try:
        numbers = sorted({int(part) for part in answer.replace(" ", "").split(",") if part})
    except ValueError:
        return None
    return numbers if all(1 <= n <= count for n in numbers) else None


__all__ = ["Option", "Prompter", "TerminalPrompter"]
