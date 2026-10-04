"""``models.toml`` for the app: what it says, what can be set up, and changing it.

The same store, detection, setup forms and provider listing the CLI uses
(:mod:`yuyutsava.llm.config`), shaped for JSON. Every change names the revision
it was based on, so an edit made by hand or in another window since is never
overwritten (``StaleWrite`` → 409); a change that would not load is refused with
the loader's own words (→ 422). The daemon's watcher picks the written file up,
so the next turn of every conversation runs on it.

Secrets typed into the app are written to ``secrets.toml`` the moment they are
checked — never into ``models.toml``, never into a response.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from collections.abc import Callable, Mapping
from typing import Any

from yuyutsava.daemon.web.services.stream_service import StreamModelsItem
from yuyutsava.llm.config.detect import Finding
from yuyutsava.llm.config.edits import Edit, add_model, add_provider, combine, use
from yuyutsava.llm.config.guide import provider_forms, secret_name
from yuyutsava.llm.config.messages import type_label
from yuyutsava.llm.config.secrets import read_secrets_file
from yuyutsava.llm.config.store import ABSENT, ConfigStore
from yuyutsava.llm.config.watch import ConfigSource, snapshot_of
from yuyutsava.llm.discovery import Discovery
from yuyutsava.llm.selection import reference_for
from yuyutsava.llm.target import ProviderAccess, access_for
from yuyutsava.llm.tasks import TASKS


class ModelsService:
    """Reads and changes ``models.toml`` on the daemon's behalf."""

    def __init__(
        self,
        store: ConfigStore,
        source: ConfigSource,
        *,
        discover: Callable[[ProviderAccess], Discovery],
        detect: Callable[[], list[Finding]],
        setup_mode: bool = False,
    ) -> None:
        self.store = store
        self.source = source
        self._discover = discover
        self._detect = detect
        self.setup_mode = setup_mode

    # ─── reading ─────────────────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        """Everything the app shows about the models, and the revision to change it from.

        As the source last loaded it — the daemon's watcher keeps it current — so
        it is safe to build from inside the source's own change notification.
        """
        revision = self.store.read()
        state = self.source.current()
        base = {
            "configured": state.snapshot is not None,
            "setup_mode": self.setup_mode,
            "path": str(self.store.models_path),
            "revision": revision.digest,
            "exists": revision.digest != ABSENT,
            "problem": None,
            "issues": [],
            "default": None,
            "tasks": [],
            "providers": [],
            "models": [],
            "secrets_missing": [],
        }
        if state.problem is not None:
            base["problem"] = str(state.problem)
            base["issues"] = [
                {"path": i.path, "problem": i.problem, "fix": i.fix, "line": i.line}
                for i in state.problem.issues
            ]
        snapshot = state.snapshot
        if snapshot is None:
            return base
        config = snapshot.config
        base["default"] = config.defaults.model
        base["tasks"] = [
            {
                "name": task.name,
                "label": task.label,
                "reference": reference_for(config, task.name),
                "follows_default": task.name not in config.tasks,
            }
            for task in TASKS
        ] + [
            {
                "name": name,
                "label": f"Subagent {name.removeprefix('subagent.')}",
                "reference": ref,
                "follows_default": False,
            }
            for name, ref in config.tasks.items()
            if name.startswith("subagent.")
        ]
        base["providers"] = [
            {
                "name": name,
                "type": connection.type,
                "label": type_label(connection.type),
                "login": connection.auth.method,
                "settings": {
                    k: v
                    for k, v in connection.model_dump(exclude_none=True).items()
                    if k not in ("type", "auth")
                },
            }
            for name, connection in config.providers.items()
        ]
        base["models"] = [
            {
                "name": alias,
                "provider": entry.provider,
                "id": entry.id,
                "settings": entry.model_dump(exclude_none=True, exclude={"provider", "id"}),
            }
            for alias, entry in config.models.items()
        ]
        base["secrets_missing"] = [
            {"reference": ref, "problem": issue.problem, "fix": issue.fix}
            for ref, issue in snapshot.unavailable.items()
        ]
        return base

    @staticmethod
    def catalog() -> list[dict[str, Any]]:
        """How each provider type is set up: what it is, its settings, its logins."""
        return [dataclasses.asdict(form) for form in provider_forms()]

    def detected(self) -> list[dict[str, Any]]:
        """What this machine already has, ready to check and save."""
        return [
            {
                "name": f.name,
                "type": f.type,
                "label": type_label(f.type),
                "origin": f.origin,
                "connection": dict(f.connection),
                "model": f.model,
                "model_settings": dict(f.model_settings),
            }
            for f in self._detect()
        ]

    def check(
        self, name: str, connection: Mapping[str, Any], secrets: Mapping[str, str]
    ) -> dict[str, Any]:
        """Log in to a provider not yet saved — free — after keeping its typed secrets.

        Returns the connection as it would be written (secrets as references) and
        what the provider lists, or the problem and its fix.
        """
        written = self._with_secrets(name, connection, secrets)
        edit = combine(add_provider(name, written), use(f"{name}/-"))
        try:
            config = self.store.preview(edit)
        except ValueError as exc:  # ModelsConfigError and ConfigEditError are both ValueErrors
            return {"connection": written, **_discovery(Discovery(name, problem=str(exc)))}
        snapshot = snapshot_of(config, read_secrets_file(self.store.secrets_path))
        access = access_for(
            config, name, secrets=snapshot.secrets, unavailable=snapshot.unavailable
        )
        return {"connection": written, **_discovery(self._discover(access))}

    def provider_models(self, name: str) -> dict[str, Any]:
        """What a saved provider lists now."""
        state = self.source.current()
        if state.snapshot is None or name not in state.snapshot.config.providers:
            raise KeyError(name)
        snapshot = state.snapshot
        access = access_for(
            snapshot.config, name, secrets=snapshot.secrets, unavailable=snapshot.unavailable
        )
        return _discovery(self._discover(access))

    # ─── changing ────────────────────────────────────────────────────────

    def write(self, edit: Edit, revision: str) -> dict[str, Any]:
        """Apply *edit* to the revision the client read; the new status."""
        self.store.apply(edit, expected=revision)
        self.source.reload()  # this request's answer, before the watcher catches up
        return self.status()

    @staticmethod
    def setup_edit(body: Mapping[str, Any]) -> Edit:
        """The one edit a finished setup makes: providers, aliases, the default, tasks."""
        edits: list[Edit] = [
            add_provider(p["name"], p["connection"]) for p in body.get("providers", [])
        ]
        edits += [
            add_model(
                a["name"],
                provider=a["provider"],
                model_id=a["id"],
                **{k: v for k, v in a.items() if k not in ("name", "provider", "id")},
            )
            for a in body.get("aliases", [])
        ]
        if body.get("default"):
            edits.append(use(body["default"]))
        for task, reference in (body.get("tasks") or {}).items():
            edits.append(use(reference, tasks=[task]))
        return combine(*edits)

    def _with_secrets(
        self, name: str, connection: Mapping[str, Any], secrets: Mapping[str, str]
    ) -> dict[str, Any]:
        written = {k: v for k, v in connection.items() if k != "auth"}
        auth = dict(connection.get("auth") or {})
        for field, value in secrets.items():
            if value and value.strip():
                secret = secret_name(name, field)
                self.store.store_secret(secret, value)
                auth[field] = f"secret:{secret}"
        written["auth"] = auth
        return written


logger = logging.getLogger("yuyutsava.daemon.web.services.models")


def broadcast_model_changes(source: ConfigSource, service: ModelsService, hub: Any) -> None:
    """Put every change *source* sees on the stream as a ``models`` item.

    The watcher reloads on its own thread; the item is built there and handed to
    the event loop that is running now. A change made here, in the CLI or by hand
    reaches every window the same way.
    """
    loop = asyncio.get_running_loop()

    def on_change(_state: Any) -> None:
        try:
            item = StreamModelsItem(models=service.status(), ts=time.time())
        except Exception:  # noqa: BLE001 — a status that cannot be read must not stop the reload
            logger.exception("models: could not build the change notice")
            return
        loop.call_soon_threadsafe(lambda: loop.create_task(hub.broadcast(item)))

    source.subscribe(on_change)


def _discovery(found: Discovery) -> dict[str, Any]:
    return {
        "ok": found.ok,
        "problem": found.problem,
        "fix": found.fix,
        "note": found.note,
        "checked_at": time.time(),
        "models": [
            {
                "id": m.id,
                "label": m.label,
                "context_window": m.context_window,
                "availability": m.availability.value,
                "note": m.note,
            }
            for m in found.models
        ],
    }


__all__ = ["ModelsService", "broadcast_model_changes"]
