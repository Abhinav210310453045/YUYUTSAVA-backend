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
        #: How each model reference offered is shown: its model id.
        self._shown: dict[str, str] = {}
        #: Secrets this run stored, so leaving a provider out takes its key with it.
        self._stored: set[str] = set()

    # ─── the two ways in ─────────────────────────────────────────────────

    def setup(self) -> bool:
        """The whole setup: what is here, more by hand, the default, the tasks, written."""
        revision = self.s.store.read()
        self.ui.say(
            "Let's connect YUYUTSAVA to an AI model. It takes a minute; "
            "nothing is saved until the end.",
            tone="title",
        )
        if revision.digest != ABSENT:
            self.ui.say(
                f"You already have a model setup ({self.s.store.models_path}); "
                "what you add here joins it.",
                tone="dim",
            )
        self._from_this_machine()
        if not self._providers or self.ui.confirm(
            "Connect another provider too? (You can also add one later: `yuyutsava models add`)",
            default=False,
        ):
            self._by_hand()
        while self.ui.confirm("Connect one more?", default=False):
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
            choose_default=self.ui.confirm(
                "Use one of its models for everything from now on?", default=False
            ),
        )

    # ─── providers ───────────────────────────────────────────────────────

    def _from_this_machine(self) -> None:
        found = self.s.detect()
        if not found:
            self.ui.say(
                "Nothing is set up on this computer yet — that's fine, let's add a provider.",
                tone="dim",
            )
            return
        picked = self.ui.choose_many(
            "Found on this computer — which should YUYUTSAVA use?",
            [Option(f, f"{type_label(f.type)}", f.origin) for f in found],
            chosen=[found[0]],
        )
        for finding in picked:
            connection = dict(finding.connection)
            variables = _env_keys(connection)
            if variables and self.ui.confirm(
                f"Save your {type_label(finding.type)} key for YUYUTSAVA on this computer, "
                "readable only by you? Otherwise it works only while "
                f"{' and '.join(variables)} is set where YUYUTSAVA starts.",
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
                        f"  Not saved ({exc}); YUYUTSAVA keeps reading it from "
                        f"{' and '.join(variables)}.",
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
            "Which provider do you want to connect?",
            [Option(f.type, _capitalised(f.label), f.introduction) for f in provider_forms()],
        )
        # Named for the person, not by them: the name only tells two of a kind apart.
        name = self._free_name(kind)
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
                f"How should YUYUTSAVA log in to {form.label}?",
                [Option(f.method, f.label) for f in form.logins],
            )
        )
        login = next(f for f in form.logins if f.method == method)
        auth: dict[str, Any] = {"method": login.method}
        for field in login.fields:
            if field.secret:
                secret = secret_name(name, field.name)
                value = self.ui.secret(
                    f"{field.question} — paste it here; it stays on this computer, "
                    "readable only by you"
                )
                if value:
                    try:
                        self.s.store.store_secret(secret, value)
                        self._stored.add(secret)
                    except PresenceNotConfirmed as exc:
                        self.ui.say(f"  ✗ Not saved: {exc}", tone="warn")
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
            self.ui.say("  This one is needed — please type it.", tone="warn")

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
        label = type_label(provider.connection["type"])
        while True:
            self.ui.say(
                f"Checking your {label} login (free — nothing is sent to a model) …", tone="dim"
            )
            listed = self._discover(provider)
            if listed.ok:
                ready = sum(m.availability is Availability.AVAILABLE for m in listed.models)
                named = (
                    ""
                    if provider.name == provider.connection["type"]
                    else (f" (saved as {provider.name})")
                )
                found = len(listed.models)
                self.ui.say(
                    f"✓ {label} works{named}: {found} model{'' if found == 1 else 's'} found, "
                    f"{ready} ready for your account",
                    tone="ok",
                )
                self._listed[provider.name] = listed
                self._providers.append(provider)
                return
            self.ui.say(f"✗ {label} didn't work: {listed.problem}", tone="warn")
            if listed.fix:
                self.ui.say(f"  {listed.fix}", tone="dim")
            what = self.ui.choose(
                "What would you like to do?",
                [
                    Option("retry", "Try again", "after you've fixed it"),
                    Option("redo", "Change what I typed"),
                    Option("keep", "Keep it anyway", "and fix it later"),
                    Option("drop", "Leave it out"),
                ],
            )
            if what == "keep":
                self._providers.append(provider)
                return
            if what == "drop":
                self._forget_keys_of(provider)
                return
            if what == "redo":
                provider = _Provider(
                    provider.name,
                    self._settings_for(provider.connection["type"], provider.name),
                    provider.model,
                )

    def _forget_keys_of(self, provider: _Provider) -> None:
        """Remove the keys this run stored for *provider*, now left out."""
        for value in provider.connection.get("auth", {}).values():
            name = value.removeprefix("secret:") if isinstance(value, str) else ""
            if name in self._stored:
                try:
                    self.s.store.forget_secret(name)
                except PresenceNotConfirmed:
                    continue  # kept; the presence check said no
                self._stored.discard(name)

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
            self.ui.say(
                "Nothing was saved — no provider was kept. "
                "Run `yuyutsava models setup` to try again.",
                tone="warn",
            )
            return False
        edits: list[Edit] = [add_provider(p.name, p.connection) for p in self._providers]
        edits += [
            add_model(_alias_for(p.model), provider=p.name, model_id=p.model, **p.model_settings)
            for p in self._providers
            if p.model and p.model_settings
        ]
        if choose_default:
            default = self._choose_model(
                "Which model should YUYUTSAVA use? (Change it any time with /model in a chat.)"
            )
            edits.append(use(default))
            edits.extend(self._task_edits(default))
        try:
            self.s.store.apply(combine(*edits), expected=expected)
        except (ConfigEditError, ModelsConfigError, StaleWrite) as exc:
            self.ui.say(f"✗ Nothing was saved: {exc}", tone="warn")
            return False
        self.ui.say(f"✓ Saved. Your models are in {self.s.store.models_path}", tone="ok")
        self.ui.say(
            "Change them any time: /model in a chat, `yuyutsava models`, or edit that file — "
            "a running YUYUTSAVA picks the change up.",
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
            f"These settings in {files} aren't used any more, now that your models are set "
            f"up: {names}.",
            tone="dim",
        )
        if self.ui.confirm(
            "Remove them from that file? (Everything else in it stays as it is.)", default=False
        ):
            for path in self.s.remove_leftovers(removable):
                self.ui.say(f"✓ Removed from {path}", tone="ok")

    def _choose_model(self, question: str) -> str:
        options = self._model_options()
        options.append(Option("", "Another model — I'll type its name"))
        chosen = self.ui.choose(question, options)
        if chosen:
            return chosen
        providers = self._providers
        provider = (
            providers[0].name
            if len(providers) == 1
            else self.ui.choose(
                "Which provider is it on?",
                [Option(p.name, type_label(p.connection["type"]), p.name) for p in providers],
            )
        )
        model = self._required(
            "The model's name, as the provider writes it (e.g. gemini-3.5-flash)", ""
        )
        reference = f"{provider}/{model}"
        self._shown[reference] = model
        return reference

    def _model_options(self) -> list[Option]:
        seen: set[str] = set()
        options: list[Option] = []
        for provider in self._providers:
            label = type_label(provider.connection["type"])
            if provider.suggestion:
                reference = provider.suggestion
                seen.add(reference)
                seen.add(f"{provider.name}/{provider.model}")
                hint = "what YUYUTSAVA uses today"
                self._shown[reference] = provider.model
                options.append(Option(reference, f"{provider.model} · {label}", hint))
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
                    self._shown[reference] = model.id
                    options.append(
                        Option(reference, f"{model.id} · {label}", _window(model.context_window))
                    )
        return options

    def _task_edits(self, default: str) -> list[Edit]:
        shown = self._shown.get(default, default)
        if self.ui.confirm(
            f"Use {shown} for everything — chats, background work, summaries? (recommended)",
            default=True,
        ):
            return []
        labels = {t.name: t.label for t in TASKS}
        tasks = self.ui.choose_many(
            "Which jobs should use a different model?",
            [Option(t.name, t.label) for t in TASKS],
        )
        if not tasks:
            return []
        other = self._choose_model(
            f"Which model for {', '.join(labels[t].lower() for t in tasks)}?"
        )
        return [use(other, tasks=tasks)]


def _alias_for(model_id: str) -> str:
    """A ``[models]`` name for *model_id*: the id itself, with what a name cannot hold replaced."""
    name = re.sub(r"[^A-Za-z0-9_.-]", "-", model_id).strip("-._") or "model"
    return name if name[0].isalnum() else f"m-{name}"


def _env_keys(connection: Mapping[str, Any]) -> list[str]:
    """The environment variables *connection*'s login reads its keys from."""
    auth = connection.get("auth", {})
    return [
        value.removeprefix("env:")
        for value in auth.values()
        if isinstance(value, str) and value.startswith("env:")
    ]


def _capitalised(label: str) -> str:
    """ "an OpenAI-compatible server" as the start of a line: "An OpenAI-compatible server"."""
    return label[:1].upper() + label[1:]


def _window(tokens: int | None) -> str:
    """``1048576`` as "1M context", ``131072`` and ``128000`` as "128k context"; "" when unknown."""
    if not tokens:
        return ""
    unit = 1024 if tokens % 1024 == 0 else 1000
    if tokens >= unit * unit:
        return f"{tokens // (unit * unit)}M context"
    return f"{tokens // unit}k context"


__all__ = ["Services", "SetupWizard"]
