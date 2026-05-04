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
import uuid
from typing import Literal
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore[assignment, misc]

from yuyutsava.cli.scenarios import format_scenario_list, get_scenario
from yuyutsava.core.config import DockerSettings, LocalSettings, llm_settings_from_env
from yuyutsava.core.engine import (
    _cleanup_local_sandbox,
    astream_agent,
    build_agent,
    builtin_tools_reference_json,
    export_agent_state_graph_png,
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


def main(argv: list[str] | None = None) -> int:
    """Sync entry point required by setuptools console_scripts. Drives async logic."""
    raw = list(argv) if argv is not None else sys.argv[1:]
    if raw and raw[0] == "daemon":
        # Hand off to the always-on daemon. The rest of argv is the daemon's own.
        from yuyutsava.daemon.main import main as daemon_main
        return daemon_main(raw[1:])
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
    docker_cfg = _docker_settings_from_args(args)
    local_cfg = _local_settings_from_args(args)

    bundle = build_agent(
        workspace,
        settings,
        bash_timeout_sec=args.bash_timeout,
        execution_mode=execution,
        docker_settings=docker_cfg,
        local_settings=local_cfg,
        permission_check=not args.no_permission_check,
    )
    try:
        thread_id = str(uuid.uuid4())
        final = await astream_agent(
            bundle.agent,
            task,
            thread_id=thread_id,
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
