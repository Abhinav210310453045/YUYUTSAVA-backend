"""The setup wizard: ``yuyutsava models setup``, ``yuyutsava models add`` and the first run.

The flow is :mod:`.flow`, how it asks is :mod:`.prompter`, and :mod:`.terminal`
puts them together with the real store, detection and provider listing.
"""

from __future__ import annotations

from yuyutsava.cli.setup_wizard.terminal import run_add, run_setup

__all__ = ["run_add", "run_setup"]
