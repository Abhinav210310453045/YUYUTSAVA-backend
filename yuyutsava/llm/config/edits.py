"""The changes a person makes to ``models.toml``, written once for every surface.

The setup wizard, ``yuyutsava models …``, the app and the agent's tools all
change the file through these. Each returns an :data:`Edit` for
:meth:`~yuyutsava.llm.config.store.ConfigStore.apply`, which keeps the person's
comments and layout, validates the result and refuses a stale write — so an edit
here only has to say *what* changes.

An edit that would leave a dangling reference — removing a provider a model
still names — is refused with a :class:`ConfigEditError` that says which
references hold it and how to free it, rather than written and left for the
loader to reject.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

import tomlkit
from tomlkit import TOMLDocument
from tomlkit.items import Null, Table, Whitespace

from yuyutsava.llm.config.schema import parse_model_ref
from yuyutsava.llm.config.secrets import NAME_PATTERN
from yuyutsava.llm.tasks import is_task

Edit = Callable[[TOMLDocument], None]
"""Changes a parsed ``models.toml`` in place."""

_NAME = re.compile(NAME_PATTERN)

#: The comment a new ``models.toml`` opens with.
FILE_HEADER = (
    "YUYUTSAVA models: which provider and model each task uses.",
    "Edit it by hand or with `yuyutsava models …`; a change applies while YUYUTSAVA runs.",
    "Keys never go here: write secret:<name> (see `yuyutsava models secrets set`),",
    "env:VARIABLE or file:<path>. Every setting: `yuyutsava models schema`.",
)


class ConfigEditError(ValueError):
    """An edit that cannot be made, with what to do instead."""

    def __init__(self, problem: str, fix: str) -> None:
        super().__init__(f"{problem} — {fix}")
        self.problem = problem
        self.fix = fix


def combine(*edits: Edit) -> Edit:
    """One edit that makes *edits* in order — written together, or not at all."""

    def edit(document: TOMLDocument) -> None:
        for each in edits:
            each(document)

    return edit


def add_provider(name: str, connection: Mapping[str, Any], *, replace: bool = False) -> Edit:
    """``[providers.<name>]`` set to *connection*; refused if it exists, unless *replace*."""
    _check_name(name, "provider")

    def edit(document: TOMLDocument) -> None:
        providers = _table(document, "providers")
        if name in providers and not replace:
            raise ConfigEditError(
                f"there is already a provider named {name!r}",
                "choose another name, or remove it first",
            )
        providers[name] = _provider_table(connection)

    return edit


def remove_provider(name: str) -> Edit:
    """``[providers.<name>]`` removed; refused while a model, task or the default uses it."""

    def edit(document: TOMLDocument) -> None:
        providers = _table(document, "providers")
        if name not in providers:
            raise ConfigEditError(
                f"there is no provider named {name!r}", "check `yuyutsava models list`"
            )
        users = [where for where, provider in _provider_uses(document) if provider == name]
        if users:
            raise ConfigEditError(
                f"provider {name!r} is still used by {', '.join(users)}",
                "point those at another model first (`yuyutsava models use …`)",
            )
        del providers[name]
        _drop_if_empty(document, "providers")

    return edit


def add_model(alias: str, *, provider: str, model_id: str, **settings: Any) -> Edit:
    """``[models.<alias>]`` naming *model_id* on *provider*, with any *settings* it overrides."""
    _check_name(alias, "model alias")

    def edit(document: TOMLDocument) -> None:
        if provider not in _table(document, "providers"):
            raise ConfigEditError(
                f"there is no provider named {provider!r}", "add the provider first"
            )
        table = tomlkit.table()
        table["provider"] = provider
        table["id"] = model_id
        for key, value in settings.items():
            if value is not None:
                table[key] = _plain(value)
        _table(document, "models")[alias] = table

    return edit


def remove_model(alias: str) -> Edit:
    """``[models.<alias>]`` removed; refused while a task or the default uses it."""

    def edit(document: TOMLDocument) -> None:
        models = _table(document, "models")
        if alias not in models:
            raise ConfigEditError(
                f"there is no model alias {alias!r}", "check `yuyutsava models list`"
            )
        users = [where for where, reference in _references(document) if reference == alias]
        if users:
            raise ConfigEditError(
                f"model {alias!r} is still used by {', '.join(users)}",
                "point those at another model first (`yuyutsava models use …`)",
            )
        del models[alias]
        _drop_if_empty(document, "models")

    return edit


def use(reference: str, *, tasks: Iterable[str] = ()) -> Edit:
    """*reference* as the default model, or as the model of each of *tasks*."""
    tasks = tuple(tasks)
    for task in tasks:
        if not is_task(task):
            raise ConfigEditError(
                f"{task!r} is not a task",
                "use chat, orchestrator, triage, subagent, subagent.<name>, compaction or tier_*",
            )

    def edit(document: TOMLDocument) -> None:
        if not tasks:
            _table(document, "defaults")["model"] = reference
            return
        assigned = _table(document, "tasks")
        for task in tasks:
            assigned[task] = reference

    return edit


def follow_default(tasks: Iterable[str]) -> Edit:
    """*tasks* back on the default model: their ``[tasks]`` entries removed."""
    tasks = tuple(tasks)

    def edit(document: TOMLDocument) -> None:
        assigned = document.get("tasks")
        if assigned is None:
            return
        for task in tasks:
            assigned.pop(task, None)
        _drop_if_empty(document, "tasks")

    return edit


def keep_keys_in_secrets(
    connection: Mapping[str, Any],
    *,
    provider: str,
    store_secret: Callable[[str, str], Any],
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """*connection* with each ``env:`` key copied into ``secrets.toml`` and referred to there.

    So the provider keeps working where that variable is not set — the app, a
    daemon started at login, a shell without the ``.env``. *store_secret* is
    :meth:`ConfigStore.store_secret`. A variable that is not set is left as it is.
    """
    environ = os.environ if environ is None else environ
    result = {key: value for key, value in connection.items() if key != "auth"}
    auth = dict(connection.get("auth", {}))
    for field, value in list(auth.items()):
        if isinstance(value, str) and value.startswith("env:"):
            secret = environ.get(value.removeprefix("env:"), "").strip()
            if secret:
                name = (
                    provider
                    if field in ("key", "token")
                    else f"{provider}-{field.replace('_', '-')}"
                )
                store_secret(name, secret)
                auth[field] = f"secret:{name}"
    if auth:
        result["auth"] = auth
    return result


# ─── helpers ─────────────────────────────────────────────────────────────


def _check_name(name: str, what: str) -> None:
    if not _NAME.match(name):
        raise ConfigEditError(
            f"{name!r} cannot name a {what}",
            "use letters, digits, '_', '-' and '.', starting with a letter or digit",
        )


def _table(document: TOMLDocument, key: str) -> Table:
    """``[key]``, created — after a new file's header — when it is missing."""
    if not document.body:
        # A new file: the header, then the default model first, where a reader
        # looks — the edits that follow fill it in and append the rest.
        for line in FILE_HEADER:
            document.add(tomlkit.comment(line))
        document.add(tomlkit.nl())
        document["version"] = 1
        document["defaults"] = tomlkit.table()
    if key not in document:
        document[key] = tomlkit.table(is_super_table=key in ("providers", "models"))
    return document[key]


def _drop_if_empty(document: TOMLDocument, key: str) -> None:
    """``[key]`` removed once its last entry is — a comment written in it keeps it."""
    table = document.get(key)
    if table is not None and all(
        isinstance(item, (Null, Whitespace)) for _, item in table.value.body
    ):
        del document[key]


def _provider_table(connection: Mapping[str, Any]) -> Table:
    table = tomlkit.table()
    for key, value in connection.items():
        if key == "auth":
            inline = tomlkit.inline_table()
            inline.update(_plain(value))
            table["auth"] = inline
        else:
            table[key] = _plain(value)
    return table


def _plain(value: Any) -> Any:
    """*value* as plain TOML data — mappings and lists copied, nothing else touched."""
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _references(document: TOMLDocument) -> list[tuple[str, str]]:
    """``(where, reference)`` for the default model and each task's."""
    found = []
    defaults = document.get("defaults") or {}
    if "model" in defaults:
        found.append(("defaults.model", str(defaults["model"])))
    for task, reference in (document.get("tasks") or {}).items():
        found.append((f"tasks.{task}", str(reference)))
    return found


def _provider_uses(document: TOMLDocument) -> list[tuple[str, str]]:
    """``(where, provider)`` for every place a provider is used."""
    models = document.get("models") or {}
    uses = [(f"models.{alias}", str(entry.get("provider"))) for alias, entry in models.items()]
    for where, reference in _references(document):
        ref = parse_model_ref(reference)
        if ref.provider is not None:
            uses.append((where, ref.provider))
        elif ref.name in models:
            uses.append((where, str(models[ref.name].get("provider"))))
    return uses


__all__ = [
    "FILE_HEADER",
    "ConfigEditError",
    "Edit",
    "add_model",
    "add_provider",
    "combine",
    "follow_default",
    "keep_keys_in_secrets",
    "remove_model",
    "remove_provider",
    "use",
]
