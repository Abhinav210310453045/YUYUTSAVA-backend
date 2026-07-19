"""
YUYUTSAVA — command-line AI agent that executes natural language tasks.

This file is the entry-point: build the argparse parser, dispatch to the
handler in ``cli/commands/*``. All the actual work lives in those handlers
(chat, sessions, prefs, scenarios). Procedural by design — see
RESTRUCTURE_HANDOFF.md §5 / plan §11.1 for why we do not make this a class.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys
from pathlib import Path
from typing import Literal

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore[assignment, misc]

from yuyutsava.aio import run as aio_run
from yuyutsava.cli.commands.chat import run_chat
from yuyutsava.cli.commands.prefs import run_prefs
from yuyutsava.cli.commands.scenarios import format_scenario_list, get_scenario
from yuyutsava.cli.commands.sessions import delete_session, print_sessions_table
from yuyutsava.core.config import DockerSettings, LocalSettings, SearchConfig, llm_settings_from_env
from yuyutsava.core.engine import (
    build_agent,
    builtin_tools_reference_json,
    export_agent_state_graph_png,
    setup_logging,
)
from yuyutsava.storage.paths import ensure_state_dirs


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
        "--debug-plumbing",
        action="store_true",
        help=(
            "Show uvicorn / langgraph runtime / httpx logs (debugging only). "
            "Off by default — these are normally silenced regardless of --verbose. "
            "Also honoured via YUYUTSAVA_DEBUG_PLUMBING=1."
        ),
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
        "--delete-session",
        metavar="ID",
        default=None,
        help="Delete the session row AND its LangGraph checkpoint rows, then exit.",
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


def main(argv: list[str] | None = None) -> int:
    """Sync entry point required by setuptools console_scripts. Drives async logic."""
    ensure_state_dirs()
    raw = list(argv) if argv is not None else sys.argv[1:]
    if raw and raw[0] == "daemon":
        # Hand off to the always-on daemon. The rest of argv is the daemon's own.
        from yuyutsava.daemon.main import main as daemon_main
        return daemon_main(raw[1:])
    if raw and raw[0] == "prefs":
        return run_prefs(raw[1:])
    if raw and raw[0] == "attach":
        from yuyutsava.cli.commands.attach import run_attach
        return run_attach(raw[1:])
    if raw and raw[0] == "chat":
        return aio_run(_async_main(raw[1:], force_chat=True))
    return aio_run(_async_main(raw))


async def _async_main(argv: list[str] | None = None, *, force_chat: bool = False) -> int:
    if load_dotenv:
        load_dotenv()

    args = _build_parser().parse_args(argv)
    setup_logging(verbose=args.verbose, debug_plumbing=args.debug_plumbing)

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
        return await print_sessions_table(workspace_filter=ws_filter)

    if args.delete_session:
        # Short-circuit: no model/sandbox needed to remove a session.
        return await delete_session(args.delete_session)

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
        # No task + no scenario → drop into the interactive chat REPL.
        # Also when invoked explicitly via `yuyutsava chat`.
        if force_chat or not task:
            from yuyutsava.cli.commands.chat_repl import run_chat_repl

            return await run_chat_repl(
                workspace=args.workspace.resolve(),
                settings=llm_settings_from_env(),
                execution_mode=_resolved_execution_mode(args),
                docker_settings=_docker_settings_from_args(args),
                local_settings=_local_settings_from_args(args),
                search_config=SearchConfig.from_env(),
                bash_timeout_sec=args.bash_timeout,
                recursion_limit=args.recursion_limit,
                permission_check=not args.no_permission_check,
                resume_id=args.resume,
                continue_latest=args.continue_,
                verbose=args.verbose,
                debug_plumbing=args.debug_plumbing,
            )

    return await run_chat(
        task=task,
        workspace=args.workspace.resolve(),
        settings=llm_settings_from_env(),
        execution_mode=_resolved_execution_mode(args),
        docker_settings=_docker_settings_from_args(args),
        local_settings=_local_settings_from_args(args),
        search_config=SearchConfig.from_env(),
        bash_timeout_sec=args.bash_timeout,
        recursion_limit=args.recursion_limit,
        permission_check=not args.no_permission_check,
        resume_id=args.resume,
        continue_latest=args.continue_,
        docker_pull_paths=args.docker_pull_paths,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    raise SystemExit(main())
