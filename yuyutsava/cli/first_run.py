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

import sys
from collections.abc import Callable
from typing import TextIO

from yuyutsava.ansi import DIM, KEY, RESET, WARN
from yuyutsava.cli.render.ansi_wrap import strip_ansi
from yuyutsava.config.env import env_flag
from yuyutsava.llm.config.leftovers import leftovers_on_disk, say_leftovers
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
) -> None:
    """Offer the setup wizard if this is a first run at a terminal; returns when it is done or declined."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    if models_config_path().exists() or setup_declined_path().exists():
        return
    if env_flag("YUYUTSAVA_SKIP_SETUP", true=("1", "true", "yes")):
        return
    if not (stdin.isatty() and stdout.isatty()):
        return
    choice = (ask or _ask)()
    if choice == "now":
        (run_setup or _run_setup)()
    elif choice == "never":
        path = setup_declined_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("models come from the environment until `yuyutsava models setup`\n")
        print(f"{DIM}ok — run `yuyutsava models setup` any time.{RESET}", file=sys.stderr)


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


def _ask() -> str:
    from yuyutsava.cli.setup_wizard.prompter import (  # noqa: PLC0415 — first run only
        Option,
        TerminalPrompter,
    )

    print(f"{KEY}No models.toml yet.{RESET} {DIM}{models_config_path()}{RESET}", file=sys.stderr)
    try:
        return TerminalPrompter().choose(
            "Set up which models YUYUTSAVA uses?",
            [
                Option(
                    "now", "set up now", "a few questions; what this machine has is offered first"
                ),
                Option("later", "not now", "ask next time"),
                Option("never", "keep using my environment", ".env / LLM_PROVIDER"),
            ],
        )
    except (KeyboardInterrupt, EOFError):
        return "later"


def _run_setup() -> int:
    from yuyutsava.cli.setup_wizard import run_setup  # noqa: PLC0415 — first run only

    return run_setup()


__all__ = ["DECLINED", "offer_setup", "setup_declined_path", "warn_leftovers"]
