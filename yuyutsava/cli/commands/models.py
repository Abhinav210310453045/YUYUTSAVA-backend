"""``yuyutsava models`` — see, check and change which models YUYUTSAVA uses.

Everything here reads or writes ``~/.yuyutsava/models.toml`` (and its
``secrets.toml``) through the one store every surface uses
(:mod:`yuyutsava.llm.config.store`), so a change made here is the change the app
and a running daemon see — the daemon picks it up without a restart.

Only ``discover``, ``verify`` and ``setup`` talk to a provider, and only
``verify --test-message`` sends a message: the one billable call here, and only
when asked for.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from typing import Any

from yuyutsava.ansi import CHROME, DIM, ERR, KEY, OK, RESET, VALUE, WARN
from yuyutsava.cli.render.ansi_wrap import strip_ansi
from yuyutsava.cli.setup_wizard.prompter import TerminalPrompter
from yuyutsava.llm.config.edits import (
    ConfigEditError,
    Edit,
    add_model,
    follow_default,
    remove_model,
    remove_provider,
    use,
)
from yuyutsava.llm.config.leftovers import Leftover, leftovers_on_disk, remove_leftovers
from yuyutsava.llm.config.loader import ModelsConfigError
from yuyutsava.llm.config.schema_export import models_config_json_schema
from yuyutsava.llm.config.secrets import read_secrets_file
from yuyutsava.llm.config.store import ConfigStore, StaleWrite
from yuyutsava.llm.config.watch import ConfigSource, ConfigState
from yuyutsava.llm.discovery import Availability, Discovery
from yuyutsava.llm.selection import TaskModels, reference_for
from yuyutsava.llm.target import access_for
from yuyutsava.llm.tasks import TASKS
from yuyutsava.storage.paths import models_config_path, secrets_path

#: How each availability is marked — one cell wide, like every mark the CLI draws.
AVAILABILITY_MARK: dict[Availability, str] = {
    Availability.AVAILABLE: f"{OK}✓{RESET}",
    Availability.NEEDS_QUOTA: f"{WARN}⊘{RESET}",
    Availability.NEEDS_ACCESS: f"{WARN}⊘{RESET}",
    Availability.NOT_IN_REGION: f"{WARN}⌖{RESET}",
    Availability.UNKNOWN: f"{DIM}?{RESET}",
}

#: Models a provider lists that ``discover`` shows before ``--all`` is needed.
_SHOWN = 40

_SETUP_HINT = "run `yuyutsava models setup` to create it"


def run_models(argv: list[str]) -> int:
    """``yuyutsava models <command>``; returns the process exit code."""
    parser = _parser()
    args = parser.parse_args(argv)
    if not getattr(args, "run", None):
        parser.print_help()
        return 2
    return args.run(args)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yuyutsava models",
        description="See, check and change which models YUYUTSAVA uses (models.toml).",
    )
    sub = parser.add_subparsers(metavar="<command>")

    def command(name: str, help_text: str, run: Callable[[argparse.Namespace], int]):
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.set_defaults(run=run)
        return p

    command("setup", "connect a provider and choose models, step by step", _setup)
    command("add", "add another provider, step by step", _add)
    command("list", "providers, models, the default and each task's model", _list)
    command("doctor", "check models.toml and its secrets without calling anyone", _doctor)
    p = command("discover", "list the models each provider offers this account", _discover)
    p.add_argument("providers", nargs="*", help="provider names (default: all)")
    p.add_argument("--all", action="store_true", help=f"show every model, not the first {_SHOWN}")
    p.add_argument("--grep", default="", help="only models whose id contains this")
    p = command("verify", "check each provider's login (free)", _verify)
    p.add_argument("providers", nargs="*", help="provider names (default: all)")
    p.add_argument(
        "--test-message",
        nargs="?",
        const="",
        metavar="MODEL",
        help="also send one short message (billable) to MODEL, or to the default model",
    )
    p = command("use", "make a model the default, or give it to some tasks", _use)
    p.add_argument("reference", help="an alias or <provider>/<model id>")
    p.add_argument("--task", action="append", default=[], help="a task (repeatable)")
    p = command("alias", "name a model, with where it runs or how big it is", _alias)
    p.add_argument("name", help="the short name to use, e.g. flash-lite")
    p.add_argument("reference", help="<provider>/<model id>")
    p.add_argument("--location", help="Vertex: a location for this model (e.g. global)")
    p.add_argument("--region", help="Bedrock: a region for this model")
    p.add_argument("--context-window", type=int, help="the input window, in tokens")
    p = command("follow", "put tasks back on the default model", _follow)
    p.add_argument("tasks", nargs="+")
    p = command("remove", "remove a provider or a model alias", _remove)
    p.add_argument("name")
    command("edit", "open models.toml in $VISUAL / $EDITOR, then check it", _edit)
    p = command(
        "clean-env", "remove the model variables models.toml made unnecessary from .env", _clean_env
    )
    p.add_argument("--yes", action="store_true", help="remove them without asking")
    command("path", "where models.toml and secrets.toml are", _path)
    command("schema", "the JSON Schema of models.toml", _schema)
    p = command("secrets", "set, remove or list secrets (secret:<name>)", _secrets)
    p.add_argument("action", choices=("set", "rm", "list"))
    p.add_argument("name", nargs="?")
    p.add_argument("--from-env", metavar="VARIABLE", help="take the value from this variable")
    return parser


# ─── reading ─────────────────────────────────────────────────────────────


def _store() -> ConfigStore:
    return ConfigStore(models_config_path(), secrets_path())


def _source() -> tuple[ConfigSource, ConfigState]:
    """``models.toml`` and its secrets, loaded once."""
    source = ConfigSource(models_config_path(), secrets_path())
    return source, source.reload()


def _state() -> ConfigState:
    return _source()[1]


def _loaded() -> ConfigState | None:
    """The config as it is now, or ``None`` after saying why there is none to use."""
    if not models_config_path().exists():
        _say_error(f"there is no {models_config_path()} yet — {_SETUP_HINT}")
        return None
    state = _state()
    if state.snapshot is None:
        _say_error(str(state.problem))
        return None
    return state


def _list(args: argparse.Namespace) -> int:
    path = models_config_path()
    if not path.exists():
        _say(f"{DIM}no models.toml yet ({path}) — models come from the environment;{RESET}")
        _say(f"{DIM}{_SETUP_HINT}{RESET}")
        return 0
    state = _loaded()
    if state is None:
        return 1
    config = state.snapshot.config
    _say(f"{KEY}models.toml{RESET}  {DIM}{path}{RESET}\n")
    _say(f"{KEY}default{RESET}      {VALUE}{config.defaults.model}{RESET}")
    assigned = [(t.name, reference_for(config, t.name)) for t in TASKS if t.name in config.tasks]
    assigned += [(t, r) for t, r in config.tasks.items() if t.startswith("subagent.")]
    if assigned:
        for i, (task, reference) in enumerate(assigned):
            label = f"{KEY}tasks{RESET}        " if i == 0 else " " * 13
            _say(f"{label}{task} {CHROME}→{RESET} {VALUE}{reference}{RESET}")
        _say(f"{' ' * 13}{DIM}every other task follows the default{RESET}")
    else:
        _say(f"{KEY}tasks{RESET}        {DIM}every task follows the default{RESET}")
    _say(f"\n{KEY}providers{RESET}")
    for name, connection in config.providers.items():
        _say(f"  {VALUE}{name:<14}{RESET} {_describe_connection(connection)}")
    if config.models:
        _say(f"\n{KEY}models{RESET}")
        for alias, entry in config.models.items():
            extras = [
                f"{k} {v}"
                for k, v in entry.model_dump(exclude_none=True).items()
                if k not in ("provider", "id")
            ]
            tail = f" {DIM}· {' · '.join(extras)}{RESET}" if extras else ""
            _say(f"  {VALUE}{alias:<14}{RESET} {entry.provider} · {entry.id}{tail}")
    for reference, issue in state.snapshot.unavailable.items():
        _say(f"\n{WARN}!{RESET} {reference}: {issue.problem} {DIM}— {issue.fix}{RESET}")
    return 0


def _describe_connection(connection: Any) -> str:
    data = connection.model_dump(exclude_none=True)
    auth = data.pop("auth", {})
    kind = data.pop("type", "")
    where = " · ".join(
        str(v)
        for k, v in data.items()
        if k in ("project", "location", "region", "base_url", "endpoint")
    )
    login = auth.get("method", "")
    return " · ".join(p for p in (kind, where, f"{DIM}{login}{RESET}") if p)


def _doctor(args: argparse.Namespace) -> int:
    path = models_config_path()
    if not path.exists():
        _say(
            f"{DIM}no models.toml ({path}) — models come from the environment; {_SETUP_HINT}{RESET}"
        )
        return 0
    source, state = _source()
    if state.snapshot is None:
        _say_error(str(state.problem))
        return 1
    problems = 0
    for reference, issue in state.snapshot.unavailable.items():
        problems += 1
        _say_error(f"{reference}: {issue.problem} — {issue.fix}")
    models = TaskModels(source)
    for task in TASKS:
        try:
            target = models.target(task.name)
        except (KeyError, ValueError, ModelsConfigError) as exc:
            problems += 1
            _say_error(f"task {task.name}: {exc}")
            continue
        _say(f"  {OK}✓{RESET} {task.name:<14} {CHROME}→{RESET} {target.reference}")
    leftovers = leftovers_on_disk(path, secrets_path())
    if leftovers:
        _say(f"\n{WARN}!{RESET} set, but not read now that models.toml decides:")
        _say_leftovers(leftovers)
        _say(f"  {DIM}`yuyutsava models clean-env` removes them{RESET}")
    if problems:
        _say(f"\n{ERR}{problems} problem{'s' if problems != 1 else ''}{RESET}")
        return 1
    _say(f"\n{OK}models.toml is fine{RESET} {DIM}({path}){RESET}")
    return 0


# ─── providers ───────────────────────────────────────────────────────────


def _accesses(names: Sequence[str]) -> list[Any] | None:
    state = _loaded()
    if state is None:
        return None
    snapshot = state.snapshot
    unknown = [n for n in names if n not in snapshot.config.providers]
    if unknown:
        _say_error(f"no provider named {', '.join(unknown)} — see `yuyutsava models list`")
        return None
    return [
        access_for(
            snapshot.config, name, secrets=snapshot.secrets, unavailable=snapshot.unavailable
        )
        for name in (names or list(snapshot.config.providers))
    ]


def _discovered(access: Any) -> Discovery:
    from yuyutsava.llm.providers.registry import discover  # noqa: PLC0415 — loads the provider SDKs

    _say(f"{DIM}asking {access.provider} …{RESET}", err=True)
    return discover(access)


def _discover(args: argparse.Namespace) -> int:
    accesses = _accesses(args.providers)
    if accesses is None:
        return 1
    failed = 0
    for access in accesses:
        found = _discovered(access)
        _say(f"\n{KEY}{access.provider}{RESET} {DIM}({access.type}){RESET}")
        if not found.ok:
            failed += 1
            _say_error(_problem(found))
            continue
        models = [m for m in found.models if args.grep.lower() in m.id.lower()]
        for model in models if args.all else models[:_SHOWN]:
            _say(f"  {format_model(model)}")
        if not args.all and len(models) > _SHOWN:
            _say(f"  {DIM}… {len(models) - _SHOWN} more (--all, or --grep to narrow){RESET}")
        if found.note:
            _say(f"  {DIM}{found.note}{RESET}")
    _say(f"\n{DIM}{_legend()}{RESET}")
    return 1 if failed else 0


def _problem(found: Discovery) -> str:
    return f"{found.problem} — {found.fix}" if found.fix else found.problem


def format_model(model: Any) -> str:
    """One discovered model on one line: its mark, id, window and any note."""
    mark = AVAILABILITY_MARK.get(model.availability, "?")
    window = f" {DIM}{model.context_window // 1000}k{RESET}" if model.context_window else ""
    note = f" {DIM}— {model.note}{RESET}" if model.note else ""
    return f"{mark} {model.id}{window}{note}"


def _legend() -> str:
    return "✓ ready · ⊘ needs a quota increase or access · ⌖ not served in this region · ? unknown"


def _verify(args: argparse.Namespace) -> int:
    accesses = _accesses(args.providers)
    if accesses is None:
        return 1
    failed = 0
    for access in accesses:
        found = _discovered(access)
        if found.ok:
            _say(f"  {OK}✓{RESET} {access.provider}: logged in, {len(found.models)} models listed")
        else:
            failed += 1
            _say_error(f"{access.provider}: {_problem(found)}")
    if args.test_message is not None and not failed:
        failed += _test_message(args.test_message)
    return 1 if failed else 0


def _test_message(reference: str) -> int:
    """Send one short message — billable — and say what came back and how fast."""
    from yuyutsava.llm.factory import chat_model  # noqa: PLC0415 — builds a model only here
    from yuyutsava.llm.failures import describe_model_failure  # noqa: PLC0415 — as above

    models = TaskModels(_source()[0])
    target = models.resolve(reference) if reference else models.target("chat")
    _say(f"{DIM}sending one message to {target.reference} (billable) …{RESET}", err=True)
    started = time.monotonic()
    try:
        reply = chat_model(target, temperature=0.0).invoke("Reply with just: OK")
    except Exception as exc:  # noqa: BLE001 — the point is to report how the call failed
        _say_error(describe_model_failure(exc) or f"{type(exc).__name__}: {exc}")
        return 1
    text = reply.content if isinstance(reply.content, str) else str(reply.content)
    _say(
        f"  {OK}✓{RESET} {target.reference} answered {text.strip()[:60]!r} in "
        f"{time.monotonic() - started:.1f}s"
    )
    return 0


# ─── changing ────────────────────────────────────────────────────────────


def _write(edit: Edit, done: str) -> int:
    store = _store()
    try:
        store.apply(edit, expected=store.read().digest)
    except (ConfigEditError, ModelsConfigError, StaleWrite) as exc:
        _say_error(str(exc))
        return 1
    _say(f"  {OK}✓{RESET} {done}")
    return 0


def _use(args: argparse.Namespace) -> int:
    if not models_config_path().exists():
        _say_error(f"there is no models.toml yet — {_SETUP_HINT}")
        return 1
    try:
        edit = use(args.reference, tasks=args.task)
    except ConfigEditError as exc:
        _say_error(str(exc))
        return 2
    done = (
        f"{', '.join(args.task)} now {'use' if len(args.task) > 1 else 'uses'} {args.reference}"
        if args.task
        else f"the default model is now {args.reference}"
    )
    return _write(edit, done)


def _alias(args: argparse.Namespace) -> int:
    provider, slash, model_id = args.reference.partition("/")
    if not slash or not model_id:
        _say_error("the model is <provider>/<model id>, e.g. vertex/gemini-3.5-flash-lite")
        return 2
    edit = add_model(
        args.name,
        provider=provider,
        model_id=model_id,
        location=args.location,
        region=args.region,
        context_window=args.context_window,
    )
    return _write(edit, f"{args.name} is {args.reference} — use it as {args.name}")


def _follow(args: argparse.Namespace) -> int:
    return _write(follow_default(args.tasks), f"{', '.join(args.tasks)} follow the default again")


def _remove(args: argparse.Namespace) -> int:
    state = _loaded()
    if state is None:
        return 1
    config = state.snapshot.config
    if args.name in config.providers:
        return _write(remove_provider(args.name), f"provider {args.name} removed")
    if args.name in config.models:
        return _write(remove_model(args.name), f"model {args.name} removed")
    _say_error(f"no provider or model alias named {args.name!r} — see `yuyutsava models list`")
    return 1


def _edit(args: argparse.Namespace) -> int:
    path = models_config_path()
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        _say(f"set $EDITOR to edit from here, or open {path} in any editor")
        return 2
    if not path.exists():
        _say_error(f"there is no models.toml yet — {_SETUP_HINT}")
        return 1
    subprocess.run([*shlex.split(editor), str(path)], check=False)  # noqa: S603 — the user's editor
    return _doctor(args)


def _clean_env(args: argparse.Namespace) -> int:
    if _loaded() is None:
        return 1
    leftovers = leftovers_on_disk(models_config_path(), secrets_path())
    if not leftovers:
        _say(f"{DIM}nothing to remove — the environment sets no model variable{RESET}")
        return 0
    _say("Set, but not read now that models.toml decides:")
    _say_leftovers(leftovers)
    removable = [leftover for leftover in leftovers if leftover.files]
    if not removable:
        return 0
    if not args.yes:
        if not sys.stdin.isatty():
            _say_error("not at a terminal to ask — run with --yes to remove them")
            return 2
        try:
            remove = TerminalPrompter().confirm("Remove them from those files?", default=False)
        except (KeyboardInterrupt, EOFError):
            remove = False
        if not remove:
            _say(f"{DIM}left as they are{RESET}")
            return 0
    for path in remove_leftovers(removable):
        _say(f"  {OK}✓{RESET} removed from {path}")
    return 0


def _say_leftovers(leftovers: Sequence[Leftover]) -> None:
    for leftover in leftovers:
        where = (
            ", ".join(str(path) for path in leftover.files)
            if leftover.files
            else "set outside any .env (your shell?) — unset it there"
        )
        _say(f"  {VALUE}{leftover.name:<26}{RESET} {leftover.why} {DIM}· {where}{RESET}")


def _path(args: argparse.Namespace) -> int:
    _say(models_config_path())
    _say(secrets_path())
    return 0


def _schema(args: argparse.Namespace) -> int:
    _say(json.dumps(models_config_json_schema(), indent=2))
    return 0


def _secrets(args: argparse.Namespace) -> int:
    store = _store()
    if args.action == "list":
        names = sorted(read_secrets_file(secrets_path()))
        _say("\n".join(names) if names else f"{DIM}no secrets stored ({secrets_path()}){RESET}")
        return 0
    if not args.name:
        _say_error(f"name the secret: yuyutsava models secrets {args.action} <name>")
        return 2
    if args.action == "rm":
        if store.forget_secret(args.name):
            _say(f"  {OK}✓{RESET} secret {args.name} removed")
            return 0
        _say_error(f"no secret named {args.name!r}")
        return 1
    value = (
        os.environ.get(args.from_env, "")
        if args.from_env
        else getpass.getpass(f"value for secret:{args.name} (not shown): ")
    )
    try:
        reference = store.store_secret(args.name, value)
    except ValueError as exc:
        _say_error(str(exc))
        return 1
    _say(f"  {OK}✓{RESET} stored — refer to it as {VALUE}{reference}{RESET} in models.toml")
    return 0


def _setup(args: argparse.Namespace) -> int:
    from yuyutsava.cli.setup_wizard import run_setup  # noqa: PLC0415 — loads provider SDKs

    return run_setup()


def _add(args: argparse.Namespace) -> int:
    from yuyutsava.cli.setup_wizard import run_add  # noqa: PLC0415 — as above

    return run_add()


def _say(text: str = "", *, err: bool = False) -> None:
    """Print *text* — coloured on a terminal, plain when piped or redirected."""
    stream = sys.stderr if err else sys.stdout
    print(text if stream.isatty() else strip_ansi(text), file=stream)


def _say_error(text: str) -> None:
    _say(f"{ERR}✗{RESET} {text}", err=True)


__all__ = ["AVAILABILITY_MARK", "format_model", "run_models"]
