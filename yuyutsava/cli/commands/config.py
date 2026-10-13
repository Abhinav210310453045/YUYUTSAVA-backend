"""``yuyutsava config`` — see, change, check and move everything that is not a model.

Models have had ``yuyutsava models`` since ADR-005. Everything else — the web
search keys, the diagram renderer, the storage backend, tracing — could only be
changed by hand-editing a ``.env`` or through the desktop Settings form. This
is the terminal's half of that, over the same catalog
(:mod:`yuyutsava.config.catalog`) the form renders and the setup wizard asks
from, so the three can never drift.

    yuyutsava config list                  every setting, where it comes from
    yuyutsava config get <name>            one setting, and where it comes from
    yuyutsava config set <name> <value>    change one
    yuyutsava config unset <name>          remove one
    yuyutsava config check [name]          does it actually work?
    yuyutsava config where                 which file your settings are kept in
    yuyutsava config move --to settings    move them to settings.toml (or --to env)
    yuyutsava config version ...           keep, list and go back to whole setups
    yuyutsava config path                  where the files are
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable

from yuyutsava.ansi import DIM, ERR, KEY, OK, RESET, WARN
from yuyutsava.cli.render.ansi_wrap import strip_ansi
from yuyutsava.config.catalog import ConfigVar, config_groups, known_keys, variable
from yuyutsava.config.checks import COSTS_A_CREDIT
from yuyutsava.config.checks import run as run_check
from yuyutsava.config.settings_file import SettingsError, SettingsStore, StaleWrite, settings_path
from yuyutsava.config.source import (
    ENV,
    SETTINGS,
    active_source,
    env_source,
    move_settings,
    settings_source,
)

#: What to say when a name is not one of ours, every time.
UNKNOWN = "is not a setting YUYUTSAVA knows. `yuyutsava config list` shows every one."


def run_config(argv: list[str]) -> int:
    """``yuyutsava config {list|get|set|unset|check|where|move|version|path}``."""
    parser = argparse.ArgumentParser(prog="yuyutsava config", description=__doc__.splitlines()[0])
    subs = parser.add_subparsers(dest="action")

    def command(name: str, help_text: str, run: Callable[[argparse.Namespace], int]):
        sub = subs.add_parser(name, help=help_text, description=help_text)
        sub.set_defaults(run=run)
        return sub

    command("list", "every setting, its value and which file it came from", _list).add_argument(
        "--group", default="", help="only one group, e.g. search"
    )
    one = command("get", "one setting's value and where it came from", _get)
    one.add_argument("name")
    changed = command("set", "change one setting", _set)
    changed.add_argument("name")
    changed.add_argument("value")
    removed = command("unset", "remove one setting", _unset)
    removed.add_argument("name")
    checked = command("check", "whether a setting actually works", _check)
    checked.add_argument("name", nargs="?", default="")
    checked.add_argument(
        "--live",
        action="store_true",
        help="prove an API key by really calling it, which spends one search credit",
    )
    command("where", "which file your settings are kept in", _where)
    moved = command("move", "move every setting into the other file", _move)
    moved.add_argument("--to", choices=(SETTINGS, ENV), required=True)
    moved.add_argument(
        "--keep", action="store_true", help="leave a copy where they are now as well"
    )
    command("path", "where settings.toml, its versions and the .env are", _path)

    version = command("version", "keep, list and go back to whole setups", _version)
    version.add_argument("action", choices=("save", "list", "show", "use", "remove"))
    version.add_argument("name", nargs="?", default="")

    args = parser.parse_args(argv)
    if not getattr(args, "run", None):
        parser.print_help()
        return 2
    try:
        return args.run(args)
    except (SettingsError, StaleWrite, ValueError) as exc:
        _error(str(exc))
        return 1


# --- reading ---------------------------------------------------------------


def _say(text: str = "") -> None:
    """Print, without colour when it is going anywhere but a terminal."""
    print(text if sys.stdout.isatty() else strip_ansi(text))


def _error(text: str) -> None:
    line = f"{ERR}yuyutsava:{RESET} {text}"
    print(line if sys.stderr.isatty() else strip_ansi(line), file=sys.stderr)


def _shown(var: ConfigVar, value: str) -> str:
    """A value as it may be printed: a secret is never shown, only its shape."""
    if not value:
        return f"{DIM}(not set){RESET}"
    if var.secret:
        return f"{DIM}set, {len(value)} characters{RESET}"
    return value


def resolved(name: str) -> tuple[str, str]:
    """The value in force for *name*, and where it comes from.

    The files are read here rather than trusted to the environment, because
    ``yuyutsava config`` is not the chat path and nothing has applied
    ``settings.toml`` into this process. The order is the one start-up keeps:
    the shell, then ``settings.toml``, then the ``.env``, then the default. A
    shell value that merely matches a file's is credited to the file, since
    there is no way to tell those apart and the file is what a person edits.
    """
    settings, env = settings_source(), env_source()
    from_settings, from_env = settings.values(), env.values()
    shell = os.environ.get(name, "").strip()
    if shell and shell not in (from_settings.get(name), from_env.get(name)):
        return shell, "your shell"
    if name in from_settings:
        return from_settings[name], str(settings.path)
    if name in from_env:
        return from_env[name], str(env.path)
    if shell:
        return shell, "your shell"
    return "", ""


def _list(args: argparse.Namespace) -> int:
    wanted = args.group.strip().lower()
    shown = 0
    for group in config_groups():
        if wanted and group.slug != wanted:
            continue
        _say(f"{KEY}{group.name}{RESET}")
        for var in group.vars:
            value, where = resolved(var.key)
            if not value:
                value, where = var.default, "the default" if var.default else ""
            trail = f"  {DIM}from {where}{RESET}" if where else ""
            _say(f"  {var.key:<38} {_shown(var, value)}{trail}")
            shown += 1
        _say()
    if wanted and not shown:
        _error(f"there is no settings group called {args.group}")
        return 1
    return 0


def _get(args: argparse.Namespace) -> int:
    try:
        var = variable(args.name)
    except KeyError:
        _error(f"{args.name} {UNKNOWN}")
        return 1
    value, where = resolved(var.key)
    if not value:
        value, where = var.default, "the default" if var.default else ""
    _say(f"{var.key} = {_shown(var, value)}")
    if where:
        _say(f"{DIM}from {where}{RESET}")
    if var.help:
        _say(f"{DIM}{var.help}{RESET}")
    return 0


def _path(_args: argparse.Namespace) -> int:
    store = SettingsStore()
    _say(f"settings : {store.path}")
    _say(f"versions : {store.versions_path}")
    _say(f".env     : {env_source().path}")
    return 0


def _where(_args: argparse.Namespace) -> int:
    active = active_source()
    _say(f"Your settings are kept in {active.label}")
    if not active.exists:
        _say(f"{DIM}— nothing is there yet; `yuyutsava setup` writes it{RESET}")
        return 0
    settings, env = settings_source(), env_source()
    if settings.exists and env.exists:
        both = settings.overlaps(env)
        if both:
            _say(
                f"{WARN}!{RESET} {', '.join(both)} are set in both {settings.path} and "
                f"{env.path}. {settings.path} wins; the ones in {env.path} are ignored."
            )
    _say(f"{DIM}Move them with `yuyutsava config move --to settings` or `--to env`.{RESET}")
    return 0


# --- changing --------------------------------------------------------------


def _set(args: argparse.Namespace) -> int:
    if args.name not in known_keys():
        _error(f"{args.name} {UNKNOWN}")
        return 1
    target = active_source()
    target.write({args.name: args.value})
    _say(f"  {OK}✓{RESET} {args.name} set in {target.label}")
    var = variable(args.name)
    if var.reload_class != "hot":
        _say(f"{DIM}A running YUYUTSAVA picks this up when it next starts.{RESET}")
    return 0


def _unset(args: argparse.Namespace) -> int:
    if args.name not in known_keys():
        _error(f"{args.name} {UNKNOWN}")
        return 1
    target = active_source()
    if args.name not in target.values():
        _say(f"{DIM}{args.name} is not set in {target.label}{RESET}")
        return 0
    target.write({args.name: ""})
    _say(f"  {OK}✓{RESET} {args.name} removed from {target.label}")
    return 0


def _move(args: argparse.Namespace) -> int:
    target, moved = move_settings(args.to, keep=args.keep)
    if not moved:
        _say(f"{DIM}there was nothing to move{RESET}")
        return 0
    _say(f"  {OK}✓{RESET} moved {len(moved)} setting(s) to {target.label}")
    for name in moved:
        _say(f"      {name}")
    if args.keep:
        _say(f"{DIM}A copy is still where they were.{RESET}")
    return 0


# --- checking --------------------------------------------------------------


def _check(args: argparse.Namespace) -> int:
    if args.name:
        try:
            var = variable(args.name)
        except KeyError:
            _error(f"{args.name} {UNKNOWN}")
            return 1
        if not var.check:
            _say(f"{DIM}there is nothing to check about {var.key}{RESET}")
            return 0
        wanted = [var]
    else:
        wanted = [v for g in config_groups() for v in g.vars if v.check]
    problems = 0
    for var in wanted:
        if var.check in COSTS_A_CREDIT and not args.live:
            _say(f"{DIM}  {var.check}: not checked (--live spends one search credit){RESET}")
            continue
        live = {"live": True} if var.check in COSTS_A_CREDIT else {}
        found = run_check(var.check, **live)
        if found.ok:
            note = f" {DIM}— {found.note}{RESET}" if found.note else ""
            _say(f"  {OK}✓{RESET} {found.name}{note}")
            continue
        problems += 1
        _say(f"  {WARN}✗{RESET} {found.name}: {found.problem}")
        if found.fix:
            _say(f"      {DIM}{found.fix}{RESET}")
    if problems:
        _say()
        _say(f"{WARN}{problems} of these will not work until fixed{RESET}")
    return 1 if problems else 0


# --- versions --------------------------------------------------------------


def _version(args: argparse.Namespace) -> int:
    store = SettingsStore()
    action, name = args.action, args.name
    if action == "list":
        kept = store.versions()
        if not kept:
            _say(f"{DIM}no versions kept yet — `yuyutsava config version save <name>`{RESET}")
            return 0
        for one in kept:
            _say(f"  {one}")
        return 0
    if not name:
        _error(f"`yuyutsava config version {action}` needs a name")
        return 2
    if action == "save":
        _say(f"  {OK}✓{RESET} kept as {store.save_version(name)}")
        return 0
    if action == "show":
        try:
            _say(store.version_path(name).read_text(encoding="utf-8"))
        except FileNotFoundError:
            _error(f"there is no version called {name}")
            return 1
        return 0
    if action == "use":
        _say(f"  {OK}✓{RESET} {name} is now {store.use_version(name)}")
        _say(f"{DIM}What was there is kept as `previous`.{RESET}")
        return 0
    if store.remove_version(name):
        _say(f"  {OK}✓{RESET} {name} forgotten")
        return 0
    _error(f"there is no version called {name}")
    return 1


__all__ = ["run_config", "settings_path"]
