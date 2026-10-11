"""The setup wizard: from "nothing configured" to a working ``models.toml``, one question at a time.

In order (design §3):

1. **What is already here.** Logins and keys this machine has
   (:mod:`yuyutsava.llm.config.detect`) — the current ``.env`` setup first,
   picked to begin with.
2. **A provider by hand**, when nothing was found or more is wanted: the type,
   then only what that type needs, then how to log in — only the methods it
   accepts, each said in plain words.
3. **Check the login.** Free: the provider's model list
   (:func:`~yuyutsava.llm.providers.registry.discover`). A failure is said
   with its fix, and the provider can be fixed, kept anyway or dropped.
4. **The default model**, picked from what the providers list as ready.
5. **Tasks** — every task follows the default, unless some should not.
6. **Write** ``models.toml`` once, at the end, through the store — nothing is
   half-written if the wizard is left early. Secrets typed along the way are
   kept in ``secrets.toml`` as they are typed.
7. **Tidy the environment**: the model variables a ``.env`` still sets, which
   ``models.toml`` now replaces, are named and offered for removal
   (:mod:`yuyutsava.llm.config.leftovers`).

The flow asks through a :class:`~.prompter.Prompter`, so tests drive it with a
script of answers.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from yuyutsava.cli.setup_wizard.prompter import Option, Prompter
from yuyutsava.llm.config.detect import Finding
from yuyutsava.llm.config.edits import (
    ConfigEditError,
    Edit,
    add_model,
    add_provider,
    combine,
    keep_keys_in_secrets,
    use,
)
from yuyutsava.llm.config.guide import provider_form, provider_forms, secret_name
from yuyutsava.llm.config.leftovers import Leftover, remove_leftovers
from yuyutsava.llm.config.loader import ModelsConfigError
from yuyutsava.llm.config.messages import type_label
from yuyutsava.llm.config.secrets import read_secrets_file
from yuyutsava.llm.config.store import ABSENT, ConfigStore, StaleWrite
from yuyutsava.llm.config.watch import snapshot_of
from yuyutsava.llm.discovery import Availability, Discovery
from yuyutsava.llm.target import ProviderAccess, access_for
from yuyutsava.llm.tasks import TASKS
from yuyutsava.platform.user_presence import PresenceNotConfirmed

#: Models a provider lists that are offered for the default, at most.
_OFFERED = 12


def _no_leftovers() -> Sequence[Leftover]:
    return ()


@dataclass
class Services:
    """What the wizard works with — the real ones at a terminal, doubles in a test."""

    store: ConfigStore
    detect: Callable[[], list[Finding]]
    discover: Callable[[ProviderAccess], Discovery]
    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)
    #: The environment's model variables ``models.toml`` makes unnecessary, read
    #: once it is written (:func:`~yuyutsava.llm.config.leftovers.leftovers_on_disk`).
    leftovers: Callable[[], Sequence[Leftover]] = _no_leftovers
    #: Removes leftovers from their ``.env`` files; the files changed.
    remove_leftovers: Callable[[Sequence[Leftover]], list[Path]] = remove_leftovers


@dataclass
class _Provider:
    name: str
    connection: dict[str, Any]
    #: A model to suggest, from a finding.
    model: str | None = None
    #: Settings that model runs with today (a thinking budget). Kept by giving
    #: it an alias that carries them — dropped, the import would quietly change
    #: how the model runs.
    model_settings: dict[str, Any] = field(default_factory=dict)

    @property
    def suggestion(self) -> str | None:
        """The reference to offer for :attr:`model`: its alias when it has settings."""
        if not self.model:
            return None
        return _alias_for(self.model) if self.model_settings else f"{self.name}/{self.model}"


class SetupWizard:
    """One run of the wizard: providers gathered, checked, then written once."""

    def __init__(self, ui: Prompter, services: Services) -> None:
        self.ui = ui
        self.s = services
        self._providers: list[_Provider] = []
        self._listed: dict[str, Discovery] = {}

    # ─── the two ways in ─────────────────────────────────────────────────

    def setup(self) -> bool:
        """The whole setup: what is here, more by hand, the default, the tasks, written."""
        revision = self.s.store.read()
        self.ui.say("Welcome to YUYUTSAVA — let's connect a model.", tone="title")
        if revision.digest != ABSENT:
            self.ui.say(
                "models.toml exists already; what you set up here is added to it.", tone="dim"
            )
        self._from_this_machine()
        if not self._providers or self.ui.confirm(
            "Set up a provider yourself as well?", default=False
        ):
            self._by_hand()
        while self.ui.confirm("Add another provider?", default=False):
            self._by_hand()
        return self._finish(revision.digest, choose_default=True)

    def add(self) -> bool:
        """One more provider, and the choice to make one of its models the default."""
        revision = self.s.store.read()
        if revision.digest == ABSENT:
            return self.setup()
        self._by_hand()
        return self._finish(
            revision.digest,
            choose_default=self.ui.confirm("Make one of its models the default?", default=False),
        )

    # ─── providers ───────────────────────────────────────────────────────

    def _from_this_machine(self) -> None:
        found = self.s.detect()
        if not found:
            self.ui.say("Nothing set up on this machine yet — that's fine.", tone="dim")
            return
        picked = self.ui.choose_many(
            "Found on this machine — use which?",
            [Option(f, f"{type_label(f.type)}", f.origin) for f in found],
            chosen=[found[0]],
        )
        for finding in picked:
            connection = dict(finding.connection)
            if _env_keys(connection) and self.ui.confirm(
                f"Keep the {type_label(finding.type)} key in secrets.toml, so the app and "
                "the daemon find it too?",
                default=True,
            ):
                try:
                    connection = keep_keys_in_secrets(
                        connection,
                        provider=self._free_name(finding.name),
                        store_secret=self.s.store.store_secret,
                        environ=self.s.environ,
                    )
                except PresenceNotConfirmed as exc:
                    self.ui.say(
                        f"  the key stays in your environment, not secrets.toml: {exc}",
                        tone="warn",
                    )
            self._checked(
                _Provider(
                    self._free_name(finding.name),
                    connection,
                    finding.model,
                    dict(finding.model_settings),
                )
            )

    def _by_hand(self) -> None:
        kind = self.ui.choose(
            "Which provider?",
            [Option(f.type, f.label, f.introduction) for f in provider_forms()],
        )
        name = self._free_name(self.ui.ask("Name it", default=kind))
        connection = self._settings_for(kind, name)
        self._checked(_Provider(name, connection))

    def _settings_for(self, kind: str, name: str) -> dict[str, Any]:
        form = provider_form(kind)
        connection: dict[str, Any] = {"type": kind}
        for field in form.settings:
            connection[field.name] = self._required(field.question, field.default)
        method = (
            form.logins[0].method
            if len(form.logins) == 1
            else self.ui.choose(
                "How do you log in?", [Option(f.method, f.label) for f in form.logins]
            )
        )
        login = next(f for f in form.logins if f.method == method)
        auth: dict[str, Any] = {"method": login.method}
        for field in login.fields:
            if field.secret:
                secret = secret_name(name, field.name)
                value = self.ui.secret(f"{field.question} for {name}")
                if value:
                    try:
                        self.s.store.store_secret(secret, value)
                    except PresenceNotConfirmed as exc:
                        self.ui.say(f"  ✗ the key was not stored: {exc}", tone="warn")
                auth[field.name] = f"secret:{secret}"
            else:
                auth[field.name] = self._required(field.question, field.default)
        connection["auth"] = auth
        return connection

    def _required(self, question: str, default: str) -> str:
        while True:
            answer = self.ui.ask(question, default=default)
            if answer:
                return answer
            self.ui.say("  this one is needed", tone="warn")

    def _free_name(self, wanted: str) -> str:
        taken = {p.name for p in self._providers} | self._existing_providers()
        name, n = wanted, 2
        while name in taken:
            name, n = f"{wanted}-{n}", n + 1
        return name

    def _existing_providers(self) -> set[str]:
        try:
            return set(self.s.store.preview(lambda document: None).providers)
        except (ModelsConfigError, ValueError):
            return set()

    # ─── checking a login ────────────────────────────────────────────────

    def _checked(self, provider: _Provider) -> None:
        """Log in to *provider* (free) and keep it, fixed, as it is, or not at all."""
        while True:
            self.ui.say(f"Checking {provider.name} …", tone="dim")
            listed = self._discover(provider)
            if listed.ok:
                ready = sum(m.availability is Availability.AVAILABLE for m in listed.models)
                self.ui.say(
                    f"✓ {provider.name}: logged in — {len(listed.models)} models, {ready} ready",
                    tone="ok",
                )
                self._listed[provider.name] = listed
                self._providers.append(provider)
                return
            self.ui.say(f"✗ {provider.name}: {listed.problem}", tone="warn")
            if listed.fix:
                self.ui.say(f"  {listed.fix}", tone="dim")
            what = self.ui.choose(
                "What now?",
                [
                    Option("retry", "try again", "after fixing it outside"),
                    Option("redo", "change the settings"),
                    Option("keep", "keep it anyway", "and fix it later"),
                    Option("drop", "leave it out"),
                ],
            )
            if what == "keep":
                self._providers.append(provider)
                return
            if what == "drop":
                return
            if what == "redo":
                provider = _Provider(
                    provider.name,
                    self._settings_for(provider.connection["type"], provider.name),
                    provider.model,
                )

    def _discover(self, provider: _Provider) -> Discovery:
        edit = combine(add_provider(provider.name, provider.connection), use(f"{provider.name}/-"))
        try:
            config = self.s.store.preview(edit)
        except (ModelsConfigError, ConfigEditError) as exc:
            return Discovery(provider.name, problem=str(exc))
        snapshot = snapshot_of(config, read_secrets_file(self.s.store.secrets_path))
        access = access_for(
            config, provider.name, secrets=snapshot.secrets, unavailable=snapshot.unavailable
        )
        return self.s.discover(access)

    # ─── models and writing ──────────────────────────────────────────────

    def _finish(self, expected: str, *, choose_default: bool) -> bool:
        if not self._providers:
            self.ui.say("Nothing to write — no provider was kept.", tone="warn")
            return False
        edits: list[Edit] = [add_provider(p.name, p.connection) for p in self._providers]
        edits += [
            add_model(_alias_for(p.model), provider=p.name, model_id=p.model, **p.model_settings)
            for p in self._providers
            if p.model and p.model_settings
        ]
        if choose_default:
            default = self._choose_model("Which model should YUYUTSAVA use by default?")
            edits.append(use(default))
            edits.extend(self._task_edits(default))
        try:
            self.s.store.apply(combine(*edits), expected=expected)
        except (ConfigEditError, ModelsConfigError, StaleWrite) as exc:
            self.ui.say(f"✗ nothing was written: {exc}", tone="warn")
            return False
        self.ui.say(f"✓ written to {self.s.store.models_path}", tone="ok")
        self.ui.say(
            "Change it any time: `yuyutsava models use <model>`, /model in a chat, or edit the "
            "file — a running YUYUTSAVA picks it up.",
            tone="dim",
        )
        self._offer_to_tidy_the_environment()
        return True

    def _offer_to_tidy_the_environment(self) -> None:
        removable = [leftover for leftover in self.s.leftovers() if leftover.files]
        if not removable:
            return
        names = ", ".join(leftover.name for leftover in removable)
        files = ", ".join(sorted({str(path) for item in removable for path in item.files}))
        self.ui.say(
            f"Not read any more, now that models.toml decides: {names} (in {files}).", tone="dim"
        )
        if self.ui.confirm("Remove them from there?", default=False):
            for path in self.s.remove_leftovers(removable):
                self.ui.say(f"✓ removed from {path}", tone="ok")

    def _choose_model(self, question: str) -> str:
        options = self._model_options()
        options.append(Option("", "type a model id myself"))
        chosen = self.ui.choose(question, options)
        if chosen:
            return chosen
        names = [p.name for p in self._providers]
        provider = (
            names[0]
            if len(names) == 1
            else self.ui.choose("On which provider?", [Option(n, n) for n in names])
        )
        return f"{provider}/{self._required('model id', '')}"

    def _model_options(self) -> list[Option]:
        seen: set[str] = set()
        options: list[Option] = []
        for provider in self._providers:
            if provider.suggestion:
                reference = provider.suggestion
                seen.add(reference)
                seen.add(f"{provider.name}/{provider.model}")
                hint = "what this machine uses now" + (
                    f" — {provider.model} with its settings" if provider.model_settings else ""
                )
                options.append(Option(reference, reference, hint))
            listed = self._listed.get(provider.name)
            ready = [
                m
                for m in (listed.models if listed else ())
                if m.availability is Availability.AVAILABLE
            ]
            for model in ready[:_OFFERED]:
                reference = f"{provider.name}/{model.id}"
                if reference not in seen:
                    seen.add(reference)
                    window = f"{model.context_window // 1000}k" if model.context_window else ""
                    options.append(Option(reference, reference, window))
        return options

    def _task_edits(self, default: str) -> list[Edit]:
        if self.ui.confirm(f"Use {default} for every task?", default=True):
            return []
        tasks = self.ui.choose_many(
            "Which tasks should use another model?",
            [Option(t.name, t.name, t.label) for t in TASKS],
        )
        if not tasks:
            return []
        other = self._choose_model(f"Which model for {', '.join(tasks)}?")
        return [use(other, tasks=tasks)]


def _alias_for(model_id: str) -> str:
    """A ``[models]`` name for *model_id*: the id itself, with what a name cannot hold replaced."""
    name = re.sub(r"[^A-Za-z0-9_.-]", "-", model_id).strip("-._") or "model"
    return name if name[0].isalnum() else f"m-{name}"


def _env_keys(connection: Mapping[str, Any]) -> bool:
    auth = connection.get("auth", {})
    return any(isinstance(v, str) and v.startswith("env:") for v in auth.values())


__all__ = ["Services", "SetupWizard"]
