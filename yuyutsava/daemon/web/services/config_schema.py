"""The config-variable catalog, as the web layer has always imported it.

The catalog moved to :mod:`yuyutsava.config.catalog` when the first-run wizard
and ``yuyutsava config`` began reading the same entries: it describes the
product's settings, not the web API. This module keeps the import path the
router and its tests already use.
"""

from __future__ import annotations

from yuyutsava.config.catalog import (
    HOT,
    RESTART_NO_RESUME,
    RESTART_RESUME,
    ConfigGroup,
    ConfigVar,
    config_groups,
)

__all__ = [
    "HOT",
    "RESTART_NO_RESUME",
    "RESTART_RESUME",
    "ConfigGroup",
    "ConfigVar",
    "config_groups",
]
