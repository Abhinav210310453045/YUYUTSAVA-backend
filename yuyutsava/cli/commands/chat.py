"""``yuyutsava <task>`` — the main chat flow.

This is the meat of the CLI: build agent stack, run one task to completion
(or resume one), optionally pull files out of docker, and clean up the local
sandbox. Pulled out of cli.py so the entry-point dispatch stays trivial.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal

from yuyutsava.cli.agent_stack import build_cli_agent_stack
from yuyutsava.core.config import DockerSettings, LlmSettings, LocalSettings, SearchConfig
from yuyutsava.core.docker_sandbox_backend import pull_virtual_paths_to_host
from yuyutsava.core.engine import cleanup_local_sandbox
from yuyutsava.sessions import ResumeFailed, run_session
from yuyutsava.storage.sessions import (
    SessionsSettings,
    build_checkpointer,
    get_default_session_store,
)


def _parse_pull_paths(s: str) -> list[str]:
    return [p.strip() for p in s.split(",") if p.strip()]


async def run_chat(
    *,
    task: str,
    workspace: Path,
    settings: LlmSettings,
    execution_mode: Literal["local", "docker"],
    docker_settings: DockerSettings,
    local_settings: LocalSettings,
    search_config: SearchConfig,
    bash_timeout_sec: int,
    recursion_limit: int,
    permission_check: bool,
    resume_id: str | None,
    continue_latest: bool,
    docker_pull_paths: str,
    verbose: bool,
) -> int:
    """Drive one CLI task end-to-end. Returns process exit code."""
    store = get_default_session_store()
    sessions_settings = SessionsSettings.from_env()

    async with build_checkpointer(sessions_settings) as checkpointer:
        bundle = await build_cli_agent_stack(
            workspace,
            settings,
            bash_timeout_sec=bash_timeout_sec,
            execution_mode=execution_mode,
            docker_settings=docker_settings,
            local_settings=local_settings,
            permission_check=permission_check,
            search_config=search_config,
            checkpointer=checkpointer,
        )

        # CLI Mode 1 async — if the bundle has a host URL (owned or attached),
        # stand up the bridge + watcher inside this asyncio context.
        cli_bridge = None
        cli_watcher = None
        if bundle.async_host_url is not None and bundle.async_task_mirror is not None:
            from yuyutsava.async_subagents.watcher import AsyncTaskHealthWatcher
            from yuyutsava.cli.async_hitl import CliHitlBridge
            cli_bridge = CliHitlBridge()
            cli_watcher = AsyncTaskHealthWatcher(
                mirror=bundle.async_task_mirror,
                host_url=bundle.async_host_url,
                ask_handler=cli_bridge.post_ask,
                event_sink=cli_bridge.post_event,
                agent_path_root="cli",
            )
            await cli_watcher.start()
        try:
            try:
                final = await run_session(
                    store=store,
                    agent=bundle.agent,
                    task=task,
                    workspace=workspace,
                    resume_id=resume_id,
                    continue_latest=continue_latest,
                    recursion_limit=recursion_limit,
                    agent_path="cli",
                )
            except ResumeFailed as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 2

            pulls = _parse_pull_paths(docker_pull_paths)
            if pulls:
                if bundle.docker_backend is None:
                    print(
                        "Warning: --docker-pull-paths only applies with --execution docker; ignored.",
                        file=sys.stderr,
                    )
                else:
                    dest = (
                        (docker_settings.export_dir / "_pulled").resolve()
                        if docker_settings.export_dir is not None
                        else (workspace / "_docker_pull").resolve()
                    )
                    written = pull_virtual_paths_to_host(bundle.docker_backend, pulls, dest)
                    if verbose and written:
                        print(f"Docker pull wrote: {written}", file=sys.stderr)

            if execution_mode == "local" and bundle.sandbox_root is not None:
                cleanup_local_sandbox(workspace, bundle.sandbox_root)
            if final.strip() and not verbose:
                print(final.strip())

            # Flush any background-task events emitted during the run.
            if cli_bridge is not None:
                await cli_bridge.render_between_turns()
        finally:
            # Shut down the watcher first (cancels in-flight runs), then the
            # host (best-effort daemon-thread teardown).
            if cli_watcher is not None:
                try:
                    await cli_watcher.shutdown()
                except Exception:
                    pass
            bundle.close()
    return 0
