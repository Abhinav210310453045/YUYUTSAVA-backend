"""The setup wizard at a terminal: the real store, detection, provider listing and prompts.

The flow is :mod:`.flow`; how it asks is :mod:`.prompter`. These two functions
put them together with the real store, detection and provider listing. Imported
only when the wizard runs, so the provider SDKs it pulls in cost a normal CLI
start nothing.
"""

from __future__ import annotations

from yuyutsava.cli.setup_wizard.flow import Services, SetupWizard
from yuyutsava.cli.setup_wizard.prompter import TerminalPrompter
from yuyutsava.llm.config.detect import detect
from yuyutsava.llm.config.leftovers import leftovers_on_disk
from yuyutsava.llm.config.store import ConfigStore
from yuyutsava.llm.providers.registry import discover
from yuyutsava.storage.paths import models_config_path, secrets_path


def _wizard() -> SetupWizard:
    return SetupWizard(
        TerminalPrompter(),
        Services(
            store=ConfigStore(models_config_path(), secrets_path()),
            detect=detect,
            discover=discover,
            leftovers=lambda: leftovers_on_disk(models_config_path(), secrets_path()),
        ),
    )


def run_setup() -> int:
    """The whole setup; exit status 0 when ``models.toml`` was written."""
    try:
        return 0 if _wizard().setup() else 1
    except (KeyboardInterrupt, EOFError):
        print("\nsetup left — nothing written to models.toml")
        return 130


def run_add() -> int:
    """One more provider; exit status 0 when it was written."""
    try:
        return 0 if _wizard().add() else 1
    except (KeyboardInterrupt, EOFError):
        print("\nleft — nothing written to models.toml")
        return 130


__all__ = ["run_add", "run_setup"]
