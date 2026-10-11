"""The first chat on a machine without ``models.toml`` starts with the setup wizard.

Offered, not forced: someone whose ``.env`` works can keep it, and a chat started
from a script — no terminal to answer from — is never stopped to ask. Saying
"keep using the environment" is remembered beside ``models.toml``, so it is
asked once; ``yuyutsava models setup`` is there whenever they change their mind.

Runs before the chat's event loop starts: the wizard's prompts run their own.

With ``models.toml``, the chat's start instead names the environment's model
variables still set: the file decides, so they are not read (:func:`warn_leftovers`).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from yuyutsava.ansi import DIM, KEY, RESET, WARN
from yuyutsava.cli.render.ansi_wrap import strip_ansi
from yuyutsava.config.env import env_flag
from yuyutsava.llm.config.detect import current_setup, where_set
from yuyutsava.llm.config.leftovers import env_files, leftovers_on_disk, say_leftovers
from yuyutsava.llm.config.messages import type_label
from yuyutsava.storage.paths import models_config_path, secrets_path

#: Beside ``models.toml``: the owner chose the environment over a setup.
DECLINED = ".models-setup-declined"

#: The wizard's own question: set up now, not now, or never ask.
Ask = Callable[[], str]


def setup_declined_path():
    """Where "keep using the environment" is remembered."""
    return models_config_path().with_name(DECLINED)


def offer_setup(
    *,
    ask: Ask | None = None,
    run_setup: Callable[[], int] | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> bool:
    """Offer the setup wizard if this is a first run at a terminal; whether it was offered.

    Returns when it is done or declined.
    """
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    if models_config_path().exists() or setup_declined_path().exists():
        return False
    if env_flag("YUYUTSAVA_SKIP_SETUP", true=("1", "true", "yes")):
        return False
    if not (stdin.isatty() and stdout.isatty()):
        return False
    choice = (ask or _ask)()
    if choice == "now":
        (run_setup or _run_setup)()
    elif choice == "never":
        path = setup_declined_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("models come from the environment until `yuyutsava models setup`\n")
        print(f"{DIM}ok — run `yuyutsava models setup` any time.{RESET}", file=sys.stderr)
    return True


def warn_leftovers(*, stderr: TextIO | None = None) -> None:
    """Name the environment's model variables ``models.toml`` made unnecessary, if any are set."""
    found = leftovers_on_disk(models_config_path(), secrets_path())
    if not found:
        return
    stream = stderr or sys.stderr
    line = (
        f"{WARN}!{RESET} {say_leftovers(found)} "
        f"{DIM}· `yuyutsava models clean-env` removes them{RESET}"
    )
    print(line if stream.isatty() else strip_ansi(line), file=stream)


@dataclass(frozen=True)
class Offer:
    """The first-run question, as a person reads it."""

    #: What YUYUTSAVA knows now, a line each.
    lines: tuple[str, ...]
    question: str
    #: ``(answer, label, hint)`` per choice.
    choices: tuple[tuple[str, str, str], ...]


def setup_offer(environ: Mapping[str, str], dotenv_files: Sequence[Path]) -> Offer:
    """What the first chat says and asks: the model in use today and where it is set, if any."""
    now = (
        "now",
        "Yes, set it up now",
        "a few questions — what is already on this computer is offered first",
    )
    later = ("later", "Not now", "ask me again next time")
    found = current_setup(environ, dotenv_files)
    if not found:
        return Offer(
            ("YUYUTSAVA doesn't know which AI model to use yet.",),
            "Set one up now? It takes a minute.",
            (now, later),
        )
    setup = found[0]
    where = where_set("LLM_PROVIDER", dotenv_files)
    keep_what = "those settings" if "terminal" in where else "that .env"
    return Offer(
        (
            "YUYUTSAVA needs to know which AI model to use.",
            f"Right now it uses {type_label(setup.type)} · {setup.model} — {where} "
            f"(LLM_PROVIDER={environ.get('LLM_PROVIDER', '').strip()}).",
        ),
        "Set up your models now? Then the desktop app and every chat use the same ones, "
        "and you can switch models in a chat.",
        (
            now,
            later,
            (
                "never",
                f"No, keep using {keep_what}",
                "don't ask again — you can change your mind with `yuyutsava models setup`",
            ),
        ),
    )


def _ask() -> str:
    from yuyutsava.cli.setup_wizard.prompter import (  # noqa: PLC0415 — first run only
        Option,
        TerminalPrompter,
    )

    offer = setup_offer(os.environ, env_files(models_config_path().parent))
    for line in offer.lines:
        print(f"{KEY}{line}{RESET}" if line is offer.lines[0] else line, file=sys.stderr)
    try:
        return TerminalPrompter().choose(
            offer.question, [Option(value, label, hint) for value, label, hint in offer.choices]
        )
    except (KeyboardInterrupt, EOFError):
        return "later"


def _run_setup() -> int:
    from yuyutsava.cli.setup_wizard import run_setup  # noqa: PLC0415 — first run only

    return run_setup()


__all__ = [
    "DECLINED",
    "Offer",
    "offer_setup",
    "setup_declined_path",
    "setup_offer",
    "warn_leftovers",
]
