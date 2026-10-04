"""``/model`` in a chat: which model this conversation runs on, and switching it.

* ``/model`` — the models ``models.toml`` names, numbered; ● marks this chat's.
* ``/model <n|name>`` — this conversation runs on it from your next message.
  The choice is the session's (``Session.models``), so it stays with the
  conversation when it is resumed, here or in the app.
* ``/model subagents all|<name> [<n|name>]`` — the subagents too, all or one
  (default: the chat's model); ``/model subagents reset`` puts them back on
  ``models.toml``.
* ``/model default <n|name>`` — every chat's default, written to ``models.toml``.
* ``/model reset`` — this conversation follows ``models.toml`` again.

After a switch, if the subagents still run on another model, it says so and
gives the command that changes them (design D7).

Not a picker that takes over the screen: the chat may be running in the split
view, which already owns the terminal. A numbered list and a typed choice work
in every view.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from yuyutsava.ansi import CHROME, DIM, KEY, OK, RESET, VALUE, WARN
from yuyutsava.llm.config.edits import ConfigEditError, use
from yuyutsava.llm.config.loader import ModelsConfigError
from yuyutsava.llm.config.store import ConfigStore, StaleWrite
from yuyutsava.llm.selection import TaskModels, reference_for
from yuyutsava.storage.models import ALL_SUBAGENTS, ModelChoice
from yuyutsava.storage.sessions.store import SessionStore

USAGE = (
    "/model [<n|name>] · /model subagents all|<name> [<n|name>] · "
    "/model default <n|name> · /model reset"
)


@dataclass
class ModelCommand:
    """``/model`` for one chat: what it reads and writes, nothing about the terminal."""

    models: TaskModels
    sessions: SessionStore
    store: ConfigStore
    #: The subagents a conversation's choice reaches (``AgentBundle.subagent_names``).
    subagents: tuple[str, ...] = ()

    async def run(self, session_id: str, args: list[str]) -> list[str]:
        """The lines to show for ``/model <args>``."""
        config = self.models.config
        if config is None:
            return [
                f"{DIM}models come from the environment — `yuyutsava models setup` makes a "
                f"models.toml, and then /model can switch them{RESET}"
            ]
        choice = (await self.sessions.get(session_id)).models
        if not args:
            return self._listing(choice)
        head, rest = args[0], args[1:]
        try:
            if head == "reset":
                await self.sessions.set_model_choice(session_id, ModelChoice())
                return [
                    f"  {OK}✓{RESET} this chat follows models.toml again, from your next message"
                ]
            if head == "default":
                return self._make_default(self._reference(rest))
            if head == "subagents":
                return await self._subagents(session_id, choice, rest)
            reference = self._reference(args)
        except ValueError as exc:
            return [f"  {WARN}✗{RESET} {exc}", f"  {DIM}{USAGE}{RESET}"]
        await self.sessions.set_model_choice(session_id, replace(choice, model=reference))
        return [
            f"  {OK}✓{RESET} this chat runs on {VALUE}{reference}{RESET} from your next message",
            *self._nudge(replace(choice, model=reference)),
        ]

    # ─── reading ─────────────────────────────────────────────────────────

    def _entries(self, choice: ModelChoice) -> list[str]:
        """Every model ``models.toml`` names, and this chat's choice, once each."""
        config = self.models.config
        seen: list[str] = []
        for reference in (
            *config.models,
            config.defaults.model,
            *config.tasks.values(),
            choice.model,
            *dict(choice.subagents).values(),
        ):
            if reference and reference not in seen:
                seen.append(reference)
        return seen

    def _listing(self, choice: ModelChoice) -> list[str]:
        config = self.models.config
        current = choice.model or reference_for(config, "chat")
        lines = [f"{KEY}models{RESET} {DIM}(● this chat){RESET}"]
        for number, reference in enumerate(self._entries(choice), 1):
            mark = f"{OK}●{RESET}" if reference == current else f"{CHROME}○{RESET}"
            notes = [n for n, on in (("default", reference == config.defaults.model),) if on]
            entry = config.models.get(reference)
            where = f"{entry.provider} · {entry.id}" if entry else ""
            tail = " · ".join(p for p in (where, *notes) if p)
            lines.append(
                f"  {mark} {CHROME}{number}.{RESET} {reference}"
                + (f"  {DIM}{tail}{RESET}" if tail else "")
            )
        if not choice.model:
            lines.append(f"  {DIM}this chat follows models.toml{RESET}")
        lines.append(f"  {DIM}{USAGE}{RESET}")
        return lines

    def _reference(self, args: list[str]) -> str:
        """The model *args* names — by number from the listing, or by name — checked."""
        if not args:
            raise ValueError("name a model, or its number from /model")
        wanted = args[0]
        if wanted.isdigit():
            entries = self._entries(ModelChoice())
            if not 1 <= int(wanted) <= len(entries):
                raise ValueError(f"there is no model {wanted} — /model lists 1 to {len(entries)}")
            return entries[int(wanted) - 1]
        try:
            self.models.resolve(wanted)
        except KeyError as exc:
            raise ValueError(
                f"models.toml has no model {wanted!r} — an alias, or <provider>/<model id>"
            ) from exc
        return wanted

    # ─── changing ────────────────────────────────────────────────────────

    def _make_default(self, reference: str) -> list[str]:
        try:
            self.store.apply(use(reference), expected=self.store.read().digest)
        except (ConfigEditError, ModelsConfigError, StaleWrite) as exc:
            return [f"  {WARN}✗{RESET} {exc}"]
        self.models.reload()
        return [
            f"  {OK}✓{RESET} every chat's default is now {VALUE}{reference}{RESET} (models.toml)"
        ]

    async def _subagents(self, session_id: str, choice: ModelChoice, args: list[str]) -> list[str]:
        if not args:
            raise ValueError("say which: /model subagents all, or a subagent's name")
        who = args[0]
        if who == "reset":
            await self.sessions.set_model_choice(session_id, replace(choice, subagents=()))
            return [f"  {OK}✓{RESET} this chat's subagents follow models.toml again"]
        if who != "all" and who not in self.subagents:
            known = ", ".join(self.subagents) or "none here"
            raise ValueError(f"there is no subagent {who!r} in this chat ({known})")
        reference = (
            self._reference(args[1:])
            if args[1:]
            else (choice.model or reference_for(self.models.config, "chat"))
        )
        chosen = dict(choice.subagents)
        if who == "all":
            chosen = {ALL_SUBAGENTS: reference}
        else:
            chosen[who] = reference
        await self.sessions.set_model_choice(
            session_id, replace(choice, subagents=tuple(sorted(chosen.items())))
        )
        whom = "all subagents" if who == "all" else who
        return [f"  {OK}✓{RESET} {whom} in this chat run on {VALUE}{reference}{RESET}"]

    def _nudge(self, choice: ModelChoice) -> list[str]:
        """Where the subagents still run on another model, the command that changes them."""
        config = self.models.config
        behind = sorted(
            {
                choice.for_subagent(name) or reference_for(config, f"subagent.{name}")
                for name in self.subagents
            }
            - {choice.model}
        )
        if not behind:
            return []
        names = ", ".join(self.subagents)
        return [
            f"  {DIM}subagents ({names}) still use {', '.join(behind)} — "
            f"/model subagents all, or name one{RESET}"
        ]


__all__ = ["USAGE", "ModelCommand"]
