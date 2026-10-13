"""Getting a new install from "nothing is configured" to "it works".

The pieces a first run needs that are not configuration itself: starting the
containers a setting depends on, and waiting until they really answer. The
questions live in :mod:`yuyutsava.cli.setup_wizard.full`; what a setting *is*
lives in :mod:`yuyutsava.config.catalog`.
"""

from __future__ import annotations

from yuyutsava.setup.containers import SERVICES, DockerContainers, Service

__all__ = ["SERVICES", "DockerContainers", "Service"]
