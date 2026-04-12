"""
YUYUTSAVA — command-line AI agent that executes natural language tasks.

Loads ``.env``, builds a Deep Agent with Groq or OpenRouter (see ``LLM_PROVIDER``)
and ``LocalShellBackend``, then invokes the graph.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Literal, cast

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore[assignment, misc]

from yuyutsava.cli.scenarios import format_scenario_list, get_scenario
from yuyutsava.core.config import llm_settings_from_env
from yuyutsava.core.engine import (
    build_agent,
    builtin_tools_reference_json,
    export_agent_state_graph_png,
    invoke_agent,
    setup_logging,
)
from yuyutsava.core.docker_sandbox_backend import pull_virtual_paths_to_host


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
        "--docker-pull-paths",
        default="",
        metavar="PATHS",
        help=(
            "After the run (docker only): comma-separated virtual paths to copy out via "
            "docker cp into <export-dir>/_pulled or <workspace>/_docker_pull."
        ),
    )
    return p


def _resolved_execution_mode(args: argparse.Namespace) -> str:
    if args.execution is not None:
        return args.execution
    env = os.environ.get("YUYUTSAVA_EXECUTION", "local").strip().lower()
    return env if env in ("local", "docker") else "local"


def _docker_image_arg(args: argparse.Namespace) -> str:
    if args.docker_image:
        return args.docker_image
    return os.environ.get("YUYUTSAVA_DOCKER_IMAGE", "deepagent-sandbox:local").strip() or "deepagent-sandbox:local"


def _docker_export_dir_arg(args: argparse.Namespace) -> Path | None:
    if args.docker_export_dir is not None:
        return args.docker_export_dir
    raw = os.environ.get("YUYUTSAVA_DOCKER_EXPORT_DIR", "").strip()
    return Path(raw) if raw else None


def _docker_network_arg(args: argparse.Namespace) -> str:
    if args.docker_network is not None:
        return args.docker_network
    env = os.environ.get("YUYUTSAVA_DOCKER_NETWORK", "bridge").strip().lower()
    return env if env in ("bridge", "none") else "bridge"


def _parse_pull_paths(s: str) -> list[str]:
    parts = [p.strip() for p in s.split(",") if p.strip()]
    return parts


def main(argv: list[str] | None = None) -> int:
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
                "Error: provide a task, or use --scenario, or --list-scenarios / --print-tools.",
                file=sys.stderr,
            )
            return 2

    settings = llm_settings_from_env()
    workspace = args.workspace.resolve()
    execution = _resolved_execution_mode(args)
    docker_export = _docker_export_dir_arg(args) if execution == "docker" else None

    bundle = build_agent(
        workspace,
        settings,
        bash_timeout_sec=args.bash_timeout,
        execution_mode=execution,
        docker_image=_docker_image_arg(args),
        docker_export_dir=docker_export,
        docker_network=cast(Literal["bridge", "none"], _docker_network_arg(args)),
    )
    try:
        final = invoke_agent(
            bundle.agent,
            task,
            verbose=args.verbose,
            recursion_limit=args.recursion_limit,
        )
        pulls = _parse_pull_paths(args.docker_pull_paths)
        if pulls:
            if bundle.docker_backend is None:
                print(
                    "Warning: --docker-pull-paths only applies with --execution docker; ignored.",
                    file=sys.stderr,
                )
            else:
                dest = (
                    (docker_export / "_pulled").resolve()
                    if docker_export is not None
                    else (workspace / "_docker_pull").resolve()
                )
                written = pull_virtual_paths_to_host(bundle.docker_backend, pulls, dest)
                if args.verbose and written:
                    print(f"Docker pull wrote: {written}", file=sys.stderr)
        if final.strip() and not args.verbose:
            print(final.strip())
    finally:
        bundle.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
