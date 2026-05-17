"""
YUYUTSAVA — command-line AI agent that executes natural language tasks.

Loads ``.env``, builds a Deep Agent with Groq or OpenRouter (see ``LLM_PROVIDER``)
and ``LocalShellBackend``, then invokes the graph.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import os
import sys
from typing import Literal
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore[assignment, misc]

from yuyutsava.cli.scenarios import format_scenario_list, get_scenario
from yuyutsava.core.config import DockerSettings, LocalSettings, SearchConfig, llm_settings_from_env
from yuyutsava.core.engine import (
    _cleanup_local_sandbox,
    build_agent,
    builtin_tools_reference_json,
    export_agent_state_graph_png,
    setup_logging,
)
from yuyutsava.core.docker_sandbox_backend import pull_virtual_paths_to_host
from yuyutsava.sessions import (
    ResumeFailed,
    SessionsSettings,
    build_checkpointer,
    get_default_session_store,
    run_session,
)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="yuyutsava",
        description=(
            "YUYUTSAVA: AI agent for natural language tasks. "
            "Uses Groq or OpenRouter + local or Docker sandbox "
            "(read_file/write_file/execute). Set LLM_PROVIDER and API keys in .env."
        ),
    )
    p.add_argument(
        "task",
        nargs="*",
        help="Natural-language task (omit if using --scenario).",
    )
    p.add_argument(
        "--scenario",
        "-s",
        metavar="ID",
        help="Run a built-in scenario (see --list-scenarios).",
    )
    p.add_argument(
        "--list-scenarios",
        action="store_true",
        help="Print available scenarios and exit.",
    )
    p.add_argument(
        "--print-tools",
        action="store_true",
        help="Print built-in file/shell tool reference (JSON) and exit.",
    )
    p.add_argument(
        "--workspace",
        "-w",
        type=Path,
        default=Path.cwd(),
        help="Workspace root the agent may read/write/run commands in (default: cwd).",
    )
    p.add_argument(
        "--recursion-limit",
        type=int,
        default=200,
        help="LangGraph recursion limit for one invocation (default: 200).",
    )
    p.add_argument(
        "--bash-timeout",
        type=int,
        default=120,
        help="Seconds before execute() kills the subprocess (default: 120).",
    )
    p.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print tool calls, results, and assistant text to stderr.",
    )
    p.add_argument(
        "--list-sessions",
        action="store_true",
        help="List persisted sessions across all workspaces (id, workspace, timestamps, message count) and exit.",
    )
    p.add_argument(
        "--this-workspace",
        action="store_true",
        help="With --list-sessions: restrict to sessions whose workspace == --workspace (default cwd).",
    )
    p.add_argument(
        "--resume",
        metavar="ID",
        default=None,
        help="Resume the session with this id (see --list-sessions). The given task is appended as the next user message.",
    )
    p.add_argument(
        "--continue",
        dest="continue_",
        action="store_true",
        help="Resume the most recently-updated session for the current --workspace.",
    )
    p.add_argument(
        "--generate_agent_graph",
        action="store_true",
        help=(
            "Build the agent graph and save LangGraph+Mermaid PNG as "
            "State_Graph_v<n>.png (requires network for Mermaid.Ink). See --graph-dir."
        ),
    )
    p.add_argument(
        "--graph-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help="Directory for State_Graph_v*.png (default: current working directory).",
    )
    p.add_argument(
        "--execution",
        choices=("local", "docker"),
        default=None,
        help="Where tools run: host (local) or Docker sandbox. Default: YUYUTSAVA_EXECUTION or local.",
    )
    p.add_argument(
        "--docker-image",
        default=None,
        metavar="IMAGE",
        help="Image for --execution docker (default: YUYUTSAVA_DOCKER_IMAGE or deepagent-sandbox:local).",
    )
    p.add_argument(
        "--docker-export-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help=(
            "Host directory mounted at /output in the container for deliverables. "
            "Optional; also YUYUTSAVA_DOCKER_EXPORT_DIR."
        ),
    )
    p.add_argument(
        "--docker-network",
        choices=("bridge", "none"),
        default=None,
        help="Docker network mode (default: YUYUTSAVA_DOCKER_NETWORK or bridge).",
    )
    p.add_argument(
        "--docker-memory",
        default=None,
        metavar="MEM",
        help="Container memory limit (default: YUYUTSAVA_DOCKER_MEMORY or 512m). E.g. 1g, 256m.",
    )
    p.add_argument(
        "--docker-cpus",
        default=None,
        metavar="CPUS",
        help="Container CPU limit (default: YUYUTSAVA_DOCKER_CPUS or 1.0). E.g. 2.0.",
    )
    p.add_argument(
        "--docker-pids-limit",
        type=int,
        default=None,
        metavar="N",
        help="Container max process count (default: YUYUTSAVA_DOCKER_PIDS_LIMIT or 100).",
    )
    p.add_argument(
        "--no-permission-check",
        action="store_true",
        default=False,
        help=(
            "Disable the permission prompt for dangerous shell commands. "
            "Use in automated / non-interactive pipelines where stdin is unavailable."
        ),
    )
    p.add_argument(
        "--docker-pull-paths",
        default="",
        metavar="PATHS",
        help=(
            "After the run (docker only): comma-separated virtual paths to copy out via "
            "docker cp into <export-dir>/_pulled or <workspace>/_docker_pull."
        ),
    )
    p.add_argument(
        "--sandbox-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help=(
            "Local sandbox directory for temporary work (default: YUYUTSAVA_SANDBOX_DIR "
            "or <workspace>/_sandbox). Deleted after each run."
        ),
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help=(
            "Directory where the agent writes output files (default: YUYUTSAVA_OUTPUT_DIR "
            "or <workspace>/_output)."
        ),
    )
    return p


def _resolved_execution_mode(args: argparse.Namespace) -> Literal["local", "docker"]:
    if args.execution is not None:
        return args.execution
    env = os.environ.get("YUYUTSAVA_EXECUTION", "local").strip().lower()
    return "docker" if env == "docker" else "local"


def _docker_settings_from_args(args: argparse.Namespace) -> DockerSettings:
    """Load DockerSettings from .env, then apply any CLI flag overrides on top."""
    cfg = DockerSettings.from_env()
    if args.docker_image:
        cfg = dataclasses.replace(cfg, image=args.docker_image)
    if args.docker_network is not None:
        cfg = dataclasses.replace(cfg, network=args.docker_network)
    if args.docker_memory:
        cfg = dataclasses.replace(cfg, memory=args.docker_memory)
    if args.docker_cpus:
        cfg = dataclasses.replace(cfg, cpus=args.docker_cpus)
    if args.docker_pids_limit is not None:
        cfg = dataclasses.replace(cfg, pids_limit=args.docker_pids_limit)
    if args.docker_export_dir is not None:
        cfg = dataclasses.replace(cfg, export_dir=args.docker_export_dir)
    return cfg


def _local_settings_from_args(args: argparse.Namespace) -> LocalSettings:
    """Load LocalSettings from .env, then apply CLI flag overrides."""
    cfg = LocalSettings.from_env()
    if args.sandbox_dir is not None:
        cfg = dataclasses.replace(cfg, sandbox_dir=args.sandbox_dir)
    if args.output_dir is not None:
        cfg = dataclasses.replace(cfg, output_dir=args.output_dir)
    return cfg


def _parse_pull_paths(s: str) -> list[str]:
    parts = [p.strip() for p in s.split(",") if p.strip()]
    return parts


def _human_bytes(n: int) -> str:
    """Format ``n`` bytes as KB/MB/GB for the sessions table."""
    if n < 1024:
        return f"{n}B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024:
            return f"{n:.1f}{unit}"
    return f"{n:.1f}PB"


def _human_age(now: float, then: float) -> str:
    """Compact "3m ago" / "2h ago" / "5d ago" for the sessions table."""
    delta = max(0.0, now - then)
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def _ansi(code: str) -> str:
    """Return an ANSI escape only when stdout is a real TTY.

    Keeps the table copy-paste-friendly when piped through ``less``, ``grep``,
    or redirected to a file.
    """
    return code if sys.stdout.isatty() else ""


_STATUS_COLOR_CODES = {
    "running": "\033[32m",
    "done":    "\033[2m",
    "crashed": "\033[31m",
    "idle":    "\033[33m",
}


async def _print_sessions_table(workspace_filter: Path | None = None) -> int:
    """``--list-sessions`` handler. Prints to stdout, returns process exit code.

    Renders each row as a labelled card with a fully-formed copy-paste resume
    command beneath it. Long fields (workspace, task) are NOT truncated — the
    point of this view is to be the source of truth the user copies from.
    """
    import shlex
    import time as _time

    store = get_default_session_store()
    rows = await store.list(workspace=workspace_filter, limit=100)
    if not rows:
        print("(no sessions yet — start one with `uv run yuyutsava <task>`)")
        return 0

    reset  = _ansi("\033[0m")
    dim    = _ansi("\033[2m")
    bold   = _ansi("\033[1m")
    cyan   = _ansi("\033[36m")
    yellow = _ansi("\033[33m")

    now = _time.time()
    scope = "this workspace" if workspace_filter is not None else "all workspaces"
    sep = "─" * 78
    print(f"{bold}Sessions ({len(rows)}) — {scope}{reset}")
    print(dim + sep + reset)

    for s in rows:
        status_colour = _ansi(_STATUS_COLOR_CODES.get(s.status, ""))
        status_str = f"{status_colour}{s.status}{reset}"
        ws_quoted = shlex.quote(str(s.workspace))
        resume_cmd = (
            f"uv run yuyutsava --verbose --workspace {ws_quoted} "
            f"--resume {s.id} \"<your next message>\""
        )

        print(f"{bold}{cyan}{s.id}{reset}")
        print(f"  {dim}status   {reset}{status_str}"
              f"   {dim}updated  {reset}{_human_age(now, s.updated_at)}"
              f"   {dim}msgs  {reset}{s.message_count}"
              f"   {dim}mem  {reset}{s.memory_files_count}"
              f"   {dim}size  {reset}{_human_bytes(s.db_row_bytes)}")
        print(f"  {dim}workspace{reset}  {s.workspace}")
        print(f"  {dim}task     {reset} {s.task_preview}")
        print(f"  {dim}resume   {reset} {yellow}{resume_cmd}{reset}")
        print(dim + sep + reset)

    print(f"{dim}tip:{reset} replace {yellow}\"<your next message>\"{reset} "
          f"with what you want the agent to do next, then paste into a terminal.")
    return 0


def _prefs_main(argv: list[str]) -> int:
    """``yuyutsava prefs {set|get|list|delete}`` subcommand."""
    import json as _json
    from yuyutsava.core.config import yuyutsava_home
    from yuyutsava.events.store import Store
    from yuyutsava.prefs.store import UserPrefsStore

    if not argv:
        print("Usage: yuyutsava prefs {set <key> <json> | get <key> | delete <key> | list}",
              file=sys.stderr)
        return 2

    sub = argv[0]

    async def _run() -> int:
        store = Store()
        await store.start()
        prefs = UserPrefsStore(store)
        try:
            if sub == "list":
                all_prefs = prefs.all()
                if not all_prefs:
                    print("(no preferences set)")
                else:
                    for key, val in sorted(all_prefs.items()):
                        print(f"{key} = {_json.dumps(val)}")
                return 0

            if sub == "get":
                if len(argv) < 2:
                    print("Usage: yuyutsava prefs get <key>", file=sys.stderr)
                    return 2
                val = prefs.get(argv[1])
                if val is None:
                    print(f"(not set: {argv[1]})")
                else:
                    print(_json.dumps(val))
                return 0

            if sub == "set":
                if len(argv) < 3:
                    print("Usage: yuyutsava prefs set <key> <json_value>", file=sys.stderr)
                    return 2
                key = argv[1]
                try:
                    value = _json.loads(argv[2])
                except _json.JSONDecodeError as exc:
                    print(f"Error: invalid JSON value: {exc}", file=sys.stderr)
                    return 1
                await prefs.set(key, value)
                # Drain the write queue before closing.
                await asyncio.sleep(0.05)
                print(f"Set {key} = {_json.dumps(value)}")
                return 0

            if sub == "delete":
                if len(argv) < 2:
                    print("Usage: yuyutsava prefs delete <key>", file=sys.stderr)
                    return 2
                await prefs.delete(argv[1])
                await asyncio.sleep(0.05)
                print(f"Deleted {argv[1]}")
                return 0

            print(f"Unknown prefs subcommand: {sub!r}", file=sys.stderr)
            return 2
        finally:
            await store.stop()

    return asyncio.run(_run())


def main(argv: list[str] | None = None) -> int:
    """Sync entry point required by setuptools console_scripts. Drives async logic."""
    raw = list(argv) if argv is not None else sys.argv[1:]
    if raw and raw[0] == "daemon":
        # Hand off to the always-on daemon. The rest of argv is the daemon's own.
        from yuyutsava.daemon.main import main as daemon_main
        return daemon_main(raw[1:])
    if raw and raw[0] == "prefs":
        return _prefs_main(raw[1:])
    return asyncio.run(_async_main(raw))


async def _async_main(argv: list[str] | None = None) -> int:
    if load_dotenv:
        load_dotenv()

    args = _build_parser().parse_args(argv)
    setup_logging(verbose=args.verbose)

    if args.list_scenarios:
        sys.stdout.write(format_scenario_list())
        return 0

    if args.print_tools:
        sys.stdout.write(builtin_tools_reference_json() + "\n")
        return 0

    if args.list_sessions:
        # Short-circuit before build_agent — no model, no Docker, no LLM keys needed.
        # Default: show every session so the user can discover ids regardless of cwd.
        # --this-workspace narrows to the current --workspace.
        ws_filter = args.workspace.resolve() if args.this_workspace else None
        return await _print_sessions_table(workspace_filter=ws_filter)

    if args.generate_agent_graph:
        if args.scenario or args.task:
            print(
                "Error: --generate_agent_graph cannot be used with a task or --scenario.",
                file=sys.stderr,
            )
            return 2
        settings = llm_settings_from_env()
        workspace = args.workspace.resolve()
        graph_bundle = build_agent(
            workspace,
            settings,
            bash_timeout_sec=args.bash_timeout,
            execution_mode="local",
        )
        graph_dir = (args.graph_dir or Path.cwd()).resolve()
        try:
            try:
                path = export_agent_state_graph_png(graph_bundle.agent, graph_dir)
            except OSError as e:
                print(f"Error: could not write graph PNG: {e}", file=sys.stderr)
                return 1
            except Exception as e:
                print(
                    "Error: PNG export failed (network needed for default Mermaid.Ink). "
                    f"Details: {e}",
                    file=sys.stderr,
                )
                return 1
            sys.stdout.write(f"Wrote {path}\n")
            return 0
        finally:
            graph_bundle.close()

    if args.scenario:
        try:
            task = get_scenario(args.scenario).prompt
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        if args.task:
            print("Error: do not pass a task text together with --scenario.", file=sys.stderr)
            return 2
    else:
        task = " ".join(args.task).strip()
        if not task:
            print(
                "Error: provide a task, or use --scenario, --list-sessions, --list-scenarios / --print-tools.",
                file=sys.stderr,
            )
            return 2

    settings = llm_settings_from_env()
    workspace = args.workspace.resolve()
    execution = _resolved_execution_mode(args)
    docker_cfg = _docker_settings_from_args(args)
    local_cfg = _local_settings_from_args(args)
    search_cfg = SearchConfig.from_env()

    store = get_default_session_store()
    sessions_settings = SessionsSettings.from_env()

    async with build_checkpointer(sessions_settings) as checkpointer:
        bundle = build_agent(
            workspace,
            settings,
            bash_timeout_sec=args.bash_timeout,
            execution_mode=execution,
            docker_settings=docker_cfg,
            local_settings=local_cfg,
            permission_check=not args.no_permission_check,
            search_config=search_cfg,
            checkpointer=checkpointer,
        )
        try:
            try:
                final = await run_session(
                    store=store,
                    agent=bundle.agent,
                    task=task,
                    workspace=workspace,
                    resume_id=args.resume,
                    continue_latest=args.continue_,
                    recursion_limit=args.recursion_limit,
                )
            except ResumeFailed as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 2
            pulls = _parse_pull_paths(args.docker_pull_paths)
            if pulls:
                if bundle.docker_backend is None:
                    print(
                        "Warning: --docker-pull-paths only applies with --execution docker; ignored.",
                        file=sys.stderr,
                    )
                else:
                    dest = (
                        (docker_cfg.export_dir / "_pulled").resolve()
                        if docker_cfg.export_dir is not None
                        else (workspace / "_docker_pull").resolve()
                    )
                    written = pull_virtual_paths_to_host(bundle.docker_backend, pulls, dest)
                    if args.verbose and written:
                        print(f"Docker pull wrote: {written}", file=sys.stderr)
            if execution == "local" and bundle.sandbox_root is not None:
                _cleanup_local_sandbox(workspace, bundle.sandbox_root)
            if final.strip() and not args.verbose:
                print(final.strip())
        finally:
            bundle.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
