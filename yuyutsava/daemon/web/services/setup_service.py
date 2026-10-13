"""Setting YUYUTSAVA up, for the desktop app: the same catalog, checks and containers.

The terminal wizard (:mod:`yuyutsava.cli.setup_wizard.full`) and this answer the
same questions from the same places — :mod:`yuyutsava.config.catalog` for what a
setting is, :mod:`yuyutsava.config.checks` for whether it works,
:mod:`yuyutsava.setup.containers` for starting what it needs. Neither reimplements
the other; if they ever disagree, one of them has stopped reading the catalog.

Everything here is blocking by nature — a probe waits on a socket, starting a
container waits on Docker — so each runs in a thread rather than on the
daemon's event loop, which is answering a chat at the same time.
"""

from __future__ import annotations

import asyncio
import logging

from yuyutsava.config import checks
from yuyutsava.config.catalog import config_groups, known_keys
from yuyutsava.config.checks import COSTS_A_CREDIT, Check
from yuyutsava.config.env_file import choices as env_choices
from yuyutsava.config.source import (
    ENV,
    SETTINGS,
    Source,
    active_source,
    env_source,
    move_settings,
    settings_source,
)
from yuyutsava.daemon.web.exceptions import NotFoundError, ValidationError
from yuyutsava.setup.containers import SERVICES, DockerContainers

logger = logging.getLogger("yuyutsava.daemon.web.services.setup")


class SetupService:
    """What the app needs to set YUYUTSAVA up, and the actions it may take."""

    def __init__(self, containers: DockerContainers | None = None) -> None:
        self._containers = containers or DockerContainers()

    # --- what is configured now -------------------------------------------

    def sources(self) -> dict[str, object]:
        """Which file settings are kept in, and what else they could be kept in."""
        active = active_source()
        settings, env = settings_source(), env_source()
        choices = [_source(settings)]
        choices += [_source(env_source(found.path)) for found in env_choices()]
        seen: list[dict[str, object]] = []
        for choice in choices:
            if choice["path"] not in {one["path"] for one in seen}:
                seen.append(choice)
        overlapping = settings.overlaps(env) if settings.exists and env.exists else []
        return {"active": _source(active), "choices": seen, "overlapping": overlapping}

    def state(self) -> dict[str, object]:
        """Everything the setup screen needs that ``/config/schema`` does not say.

        Deliberately cheap: it reads files and asks Docker for its version, and
        runs no probe. A screen that waited eight seconds for Postgres before
        it could draw would be a worse answer than one that says "not checked".
        """
        settings, env = settings_source().values(), env_source().values()
        states = []
        for group in config_groups():
            for var in group.vars:
                where = ""
                if var.key in settings:
                    where = str(settings_source().path)
                elif var.key in env:
                    where = str(env_source().path)
                states.append({"key": var.key, "set": bool(where), "source": where})
        engine = self._containers.engine()
        return {
            "source": self.sources(),
            "settings": states,
            "services": self._services(),
            "engine": _check(engine),
            "engine_launch_command": self._containers.engine_launch_command(),
            "models_configured": _models_configured(),
        }

    def _services(self) -> list[dict[str, object]]:
        needed: dict[str, list[str]] = {}
        for group in config_groups():
            for var in group.vars:
                if var.service:
                    needed.setdefault(var.service, []).append(var.key)
        return [
            {
                "name": name,
                "command": self._containers.command(name),
                "purpose": self._containers.purpose(name),
                "needed_by": needed.get(name, []),
            }
            for name in SERVICES
        ]

    # --- actions -----------------------------------------------------------

    async def check(self, name: str, *, live: bool = False) -> dict[str, object]:
        """Run one probe. Only a probe that was asked for may cost anything."""
        if name not in checks.PROBES:
            raise NotFoundError(f"there is no check called {name}")
        if live and name not in COSTS_A_CREDIT:
            live = False
        options = {"live": True} if live else {}
        found = await asyncio.to_thread(checks.run, name, **options)
        return _check(found)

    async def start(self, name: str) -> dict[str, object]:
        """Start a container and wait until the thing inside it answers."""
        if name not in SERVICES:
            raise NotFoundError(f"there is nothing called {name} to start")
        found = await asyncio.to_thread(self._containers.start, name)
        return _check(found)

    async def launch_engine(self) -> dict[str, object]:
        """Start Docker Desktop and wait for its engine."""
        found = await asyncio.to_thread(self._containers.launch_engine)
        return _check(found)

    def write(self, values: dict[str, str]) -> dict[str, object]:
        """Write settings to whichever file is in force; what was written, and where."""
        known = known_keys()
        unknown = [key for key in values if key not in known]
        if unknown:
            raise ValidationError(f"{', '.join(sorted(unknown))} are not settings YUYUTSAVA knows")
        target = active_source()
        written = target.write(dict(values))
        return {"written": written, "path": target.label}

    def move(self, kind: str, *, keep: bool = False) -> dict[str, object]:
        """Move every setting into the other file."""
        if kind not in (SETTINGS, ENV):
            raise ValidationError(f"settings are kept in {SETTINGS} or {ENV}, not {kind!r}")
        _, moved = move_settings(kind, keep=keep)
        return {"moved": moved, "source": self.sources()}


def _source(source: Source) -> dict[str, object]:
    return {
        "kind": source.kind,
        "path": str(source.path),
        "exists": source.exists,
        "label": source.label,
    }


def _check(found: Check) -> dict[str, object]:
    return {
        "name": found.name,
        "ok": found.ok,
        "problem": found.problem,
        "fix": found.fix,
        "note": found.note,
    }


def _models_configured() -> bool:
    """Whether ``models.toml`` is there and loads — the one thing with no default."""
    from yuyutsava.storage.paths import models_config_path  # noqa: PLC0415 — start-up cost

    return models_config_path().is_file()
