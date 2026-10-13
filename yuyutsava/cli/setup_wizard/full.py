"""The whole first-run setup: models, and everything else YUYUTSAVA needs.

``yuyutsava models setup`` has always connected a model and stopped there, so a
first run could finish, say ``Saved``, and still leave a chat that cannot start
— no storage, no web search, no diagram renderer. This runs the same model
wizard and then the rest, in sections a person can take or leave.

Three rules hold everywhere here, because they are what makes a setup
trustworthy rather than merely thorough:

**Nothing is nameless.** Every question names the file, container, port or
variable it is about. Not "remove them from that file" but "remove them from
C:\\Users\\you\\.yuyutsava\\.env". A person cannot consent to something they
cannot see.

**Nothing happens without a yes.** A check that spends a credit says so first;
a container is started only when asked for. A no is never fatal: it prints the
exact command and the reason, and setup moves on.

**A section is data, not code.** The questions come from
:mod:`yuyutsava.config.catalog` — a setting with a ``question`` is asked, one
with a ``check`` is verified, one with a ``service`` can offer to start it. So
the next setting is one catalog entry, not another branch in here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from yuyutsava.cli.setup_wizard.prompter import Option, Prompter
from yuyutsava.config import checks
from yuyutsava.config.catalog import ConfigGroup, ConfigVar, config_groups, group
from yuyutsava.config.checks import COSTS_A_CREDIT, Check
from yuyutsava.config.env_file import EnvFile
from yuyutsava.config.env_file import choices as env_choices
from yuyutsava.config.source import (
    Source,
    active_source,
    env_source,
    settings_source,
)

#: The sections this offers, in order, by catalog group slug. Models come first
#: because nothing else matters without one.
SECTIONS: tuple[str, ...] = ("models", "search", "diagrams", "storage", "observability")

#: How each section reads in the menu: what it is for, in a person's terms.
HEADINGS: dict[str, tuple[str, str]] = {
    "models": ("Models", "which AI model answers you"),
    "search": ("Web search", "lets the model search the web and read pages"),
    "diagrams": ("Diagrams", "renders Mermaid, PlantUML and D2 pictures"),
    "storage": ("Storage", "where your history and memory are kept"),
    "observability": ("Tracing", "records model calls so a run can be read back"),
}


class Containers(Protocol):
    """Starting the containers a setting needs — :mod:`yuyutsava.setup.containers`."""

    def engine(self) -> Check:
        """Whether the container engine is running."""

    def engine_launch_command(self) -> str:
        """The command that would start Docker Desktop; empty where there is none."""

    def launch_engine(self) -> Check:
        """Start Docker Desktop and wait for its engine to answer."""

    def start(self, service: str) -> Check:
        """Start *service* and wait until it really works."""

    def command(self, service: str) -> str:
        """The exact command that starts *service*, for someone who says no."""

    def purpose(self, service: str) -> str:
        """What *service* is for, for someone deciding whether to start it."""


def _containers() -> Containers:
    # noqa comment below: imported here so `yuyutsava config list` pays nothing for it.
    from yuyutsava.setup.containers import DockerContainers  # noqa: PLC0415

    return DockerContainers()


@dataclass
class Services:
    """What the setup works with — the real ones at a terminal, doubles in a test."""

    #: Runs the models wizard; whether ``models.toml`` was written.
    models: Callable[[], bool]
    #: Where settings are kept now.
    source: Callable[[], Source] = active_source
    #: The ``.env`` files a person may choose between.
    env_files: Callable[[], list[EnvFile]] = env_choices
    #: Runs one probe by name.
    check: Callable[..., Check] = checks.run
    #: Starts the containers a setting needs.
    containers: Containers = field(default_factory=_containers)
    #: The catalog, so a test can hand in its own.
    groups: Callable[[], list[ConfigGroup]] = config_groups


class FullSetup:
    """Every section of the first run, over a :class:`~.prompter.Prompter`."""

    def __init__(self, ui: Prompter, services: Services) -> None:
        self.ui = ui
        self.s = services
        #: What this run wrote and where, so the end can say it in one place.
        self._written: dict[str, str] = {}
        #: Whether the models wizard wrote models.toml this run.
        self._models_written = False
        #: What was answered this run, so a declined service can name what it
        #: would be instead.
        self._chosen: dict[str, str] = {}
        #: Services the person declined, named again at the end with their command.
        self._declined: list[tuple[str, str]] = []

    # --- the whole run -----------------------------------------------------

    def run(self, sections: Sequence[str] = ()) -> bool:
        """Every section, or just the ones named; whether anything was written."""
        wanted = tuple(sections) or SECTIONS
        if not sections:
            self.ui.say(
                "Let's set YUYUTSAVA up. Each part can be skipped and run again later "
                "with `yuyutsava setup --section <name>`.",
                tone="title",
            )
            self._where_settings_live()
        for name in wanted:
            if name == "models":
                self._models()
            else:
                self.section(name)
        self._summary()
        return bool(self._written or self._models_written)

    # --- section 0: which file holds the settings --------------------------

    def _where_settings_live(self) -> None:
        """Ask once where settings go; the answer is which file holds them."""
        current = self.s.source()
        if current.exists:
            self.ui.say(
                f"Your settings are kept in {current.label}. "
                "Move them with `yuyutsava config move --to settings` or `--to env`.",
                tone="dim",
            )
            return
        options = [
            Option(
                settings_source(),
                f"{settings_source().label}",
                "checked before it is saved, and you can keep more than one version",
            )
        ]
        options += [
            Option(env_source(found.path), str(found.path), found.description)
            for found in self.s.env_files()
        ]
        chosen = self.ui.choose("Where should YUYUTSAVA keep these settings?", options)
        self._source = chosen
        self.ui.say(f"  Settings will be written to {chosen.label}", tone="dim")

    def _target(self) -> Source:
        return getattr(self, "_source", None) or self.s.source()

    # --- section 1: models -------------------------------------------------

    def _models(self) -> None:
        self._heading("models")
        self._models_written = self.s.models()

    # --- every other section, from the catalog -----------------------------

    def section(self, name: str) -> None:
        """One catalog group, asked and checked."""
        try:
            found = group(name)
        except KeyError:
            self.ui.say(f"There is no setup section called {name}.", tone="warn")
            return
        asked = [var for var in found.vars if var.question]
        if not asked:
            return
        self._heading(name)
        if not self._wanted(name, asked):
            return
        values = self._target().values()
        changes: dict[str, str] = {}
        for var in asked:
            answer = self._answer(var, values.get(var.key, ""))
            if answer is None:
                continue
            self._chosen[var.key] = answer
            if answer:
                answer = self._verify(var, answer) or answer
            if answer != values.get(var.key, ""):
                changes[var.key] = answer
        if changes:
            self._write(changes)

    def _wanted(self, name: str, asked: list[ConfigVar]) -> bool:
        """Whether to go into this section at all; required ones are not asked."""
        if any(var.required for var in asked):
            return True
        _, purpose = HEADINGS.get(name, (name, ""))
        return self.ui.confirm(
            f"Set up {HEADINGS.get(name, (name, ''))[0].lower()}? It {purpose}.", default=False
        )

    def _answer(self, var: ConfigVar, current: str) -> str | None:
        """Ask for one setting the way its kind is asked for."""
        question = var.question
        if var.help:
            self.ui.say(f"  {var.help}", tone="dim")
        if var.type == "select":
            return self.ui.choose(
                question,
                [Option(choice, choice) for choice in var.options],
                default=current or var.default,
            )
        if var.type == "toggle":
            on = self.ui.confirm(question, default=(current or var.default) not in ("", "0"))
            return "1" if on else "0"
        if var.secret:
            if current:
                self.ui.say(f"  {var.key} is already set; Enter keeps it.", tone="dim")
            typed = self.ui.secret(
                f"{question} — paste it here; it stays on this computer, readable only by you"
            )
            return typed or current or None
        return self.ui.ask(question, default=current or var.default)

    # --- checking, and the containers a setting needs ----------------------

    def _verify(self, var: ConfigVar, value: str) -> str | None:
        """Run the setting's check and offer what would make it pass.

        Returns a value to use instead, when the only working answer is a
        different one.
        """
        if not var.check:
            return None
        if var.check in COSTS_A_CREDIT and not self.ui.confirm(
            f"Check the {var.label} by making one real search? It spends one search credit.",
            default=False,
        ):
            self.ui.say("  Not checked; nothing was sent.", tone="dim")
            return None
        self.ui.say(f"Checking {var.check.capitalize()} …", tone="dim")
        live = {"live": True} if var.check in COSTS_A_CREDIT else {}
        result = self.s.check(var.check, **live)
        if result.ok:
            self.ui.say(
                f"✓ {result.name} works{(' — ' + result.note) if result.note else ''}", tone="ok"
            )
            return None
        self.ui.say(f"✗ {result.name}: {result.problem}", tone="warn")
        if result.fix:
            self.ui.say(f"  {result.fix}", tone="dim")
        return self._offer_service(var, value) if var.service else None

    def _engine_running(self, service: str) -> bool:
        """Make sure the container engine is up, asking before starting anything."""
        engine = self.s.containers.engine()
        if engine.ok:
            return True
        self.ui.say(f"✗ {engine.name}: {engine.problem}", tone="warn")
        self.ui.say(f"  {engine.fix}", tone="dim")
        launch = self.s.containers.engine_launch_command()
        if launch and self.ui.confirm(
            f"Start Docker Desktop now? This runs: {launch}", default=True
        ):
            started = self.s.containers.launch_engine()
            if started.ok:
                self.ui.say("✓ Docker is running", tone="ok")
                return True
            self.ui.say(f"✗ {started.problem}", tone="warn")
            if started.fix:
                self.ui.say(f"  {started.fix}", tone="dim")
        elif self.ui.confirm(f"Start Docker yourself and try {service} again now?", default=False):
            return self.s.containers.engine().ok
        return False

    def _offer_service(self, var: ConfigVar, value: str) -> str | None:
        """Offer to start the container this setting needs; a safer value if declined."""
        service = var.service
        command = self.s.containers.command(service)
        if not self._engine_running(service):
            return self._decline(service, command, var)
        purpose = self.s.containers.purpose(service)
        if purpose:
            self.ui.say(f"  {service} {purpose}", tone="dim")
        if not self.ui.confirm(f"Start {service} now? This runs: {command}", default=True):
            return self._decline(service, command, var)
        self.ui.say(f"Starting {service} …", tone="dim")
        started = self.s.containers.start(service)
        if started.ok:
            self.ui.say(
                f"✓ {service} is running{(' — ' + started.note) if started.note else ''}", tone="ok"
            )
            if var.check:
                again = self.s.check(var.check)
                tone, mark = ("ok", "✓") if again.ok else ("warn", "✗")
                self.ui.say(f"{mark} {again.name}: {again.problem or 'works'}", tone=tone)
            return None
        self.ui.say(f"✗ {service} did not start: {started.problem}", tone="warn")
        if started.fix:
            self.ui.say(f"  {started.fix}", tone="dim")
        return self._decline(service, command, var)

    def _decline(self, service: str, command: str, var: ConfigVar) -> str | None:
        """Record a service left unstarted; a safer value for the setting, if there is one.

        Leaving the setting pointing at something that is not running is how a
        first run ends in a chat that cannot start. Where the setting has
        another choice that needs nothing — SQLite instead of Postgres — offer
        it rather than leaving a setup that is known to be broken.
        """
        self._declined.append((service, command))
        self.ui.say(f"  Leaving {service} alone. Start it yourself with:  {command}", tone="dim")
        others = [choice for choice in var.options if choice != self._chosen.get(var.key)]
        if len(others) != 1:
            return None
        instead = others[0]
        if self.ui.confirm(
            f"Use {instead} for now, so YUYUTSAVA works until {service} is running?",
            default=True,
        ):
            self.ui.say(
                f"  {var.key} stays {instead}; change it with "
                f"`yuyutsava config set {var.key} {self._chosen.get(var.key)}`.",
                tone="dim",
            )
            return instead
        return None

    # --- writing and saying what happened ----------------------------------

    def _write(self, changes: dict[str, str]) -> None:
        target = self._target()
        try:
            written = target.write(changes)
        except Exception as exc:  # noqa: BLE001 — a boundary: every writer failure reads the same
            self.ui.say(f"✗ Nothing was saved to {target.label}: {exc}", tone="warn")
            return
        for key in written:
            self._written[key] = target.label
        self.ui.say(f"✓ Saved {', '.join(written)} to {target.label}", tone="ok")

    def _heading(self, name: str) -> None:
        title, purpose = HEADINGS.get(name, (name.title(), ""))
        self.ui.say()
        self.ui.say(f"{title} — {purpose}" if purpose else title, tone="title")

    def _summary(self) -> None:
        self.ui.say()
        if self._models_written:
            self.ui.say("✓ Your models are set up", tone="ok")
        if not self._written:
            if not self._models_written:
                self.ui.say("Nothing was changed.", tone="dim")
        else:
            where = sorted(set(self._written.values()))
            self.ui.say(f"✓ Settings written to {', '.join(where)}", tone="ok")
            self.ui.say(
                "Change any of it later with `yuyutsava config set <name> <value>`, "
                "or run `yuyutsava setup` again.",
                tone="dim",
            )
        for service, command in self._declined:
            self.ui.say(f"! {service} is not running. Start it with:  {command}", tone="warn")


def overlapping(ui: Prompter, environ: Mapping[str, str] | None = None) -> None:
    """Say which variables both sources set, since one of them is being ignored."""
    settings, env = settings_source(), env_source()
    if not (settings.exists and env.exists):
        return
    both = settings.overlaps(env)
    if not both:
        return
    ui.say(
        f"! {', '.join(both)} are set in both {settings.label} and {env.label}. "
        f"{settings.label} wins; the ones in {env.label} are ignored.",
        tone="warn",
    )
