"""``yuyutsava <task>`` — the main chat flow.

This is the meat of the CLI: build agent stack, run one task to completion
(or resume one), optionally pull files out of docker, and clean up the local
sandbox. Pulled out of cli.py so the entry-point dispatch stays trivial.
"""

from __future__ import annotations

import contextlib
import sys

from yuyutsava.cli.commands.options import ChatOptions
from yuyutsava.cli.session_run import run_session
from yuyutsava.cli.tooling import cleanup_local_sandbox
from yuyutsava.conversation.sessions import ResumeFailed
from yuyutsava.core.agent_stack import build_agent_stack
from yuyutsava.core.app_context import open_events_store
from yuyutsava.core.deps import StackWiring
from yuyutsava.docker_sandbox.backend import pull_virtual_paths_to_host
from yuyutsava.llm.failures import describe_model_failure
from yuyutsava.storage.paths import WorkspaceLayout
from yuyutsava.storage.prefs import PrefsStore
from yuyutsava.storage.sessions import (
    SessionsSettings,
    build_checkpointer,
    session_store_for,
)


def _parse_pull_paths(s: str) -> list[str]:
    return [p.strip() for p in s.split(",") if p.strip()]


async def run_chat(
    opts: ChatOptions,
    *,
    task: str,
    docker_pull_paths: str,
) -> int:
    """Drive one CLI task end-to-end. Returns process exit code."""
    workspace = opts.workspace
    settings = opts.settings
    docker_settings = opts.docker_settings
    search_config = opts.search_config
    bash_timeout_sec = opts.bash_timeout_sec
    recursion_limit = opts.recursion_limit
    permission_check = opts.permission_check
    resume_id = opts.resume_id
    continue_latest = opts.continue_latest
    verbose = opts.verbose
    store = session_store_for()
    sessions_settings = SessionsSettings.from_env()

    async with (
        build_checkpointer(sessions_settings) as checkpointer,
        open_events_store() as events,
    ):
        bundle = await build_agent_stack(
            workspace,
            settings,
            # The per-turn USER PREFERENCES block, from the configured
            # backend's preferences — the ones the daemon and the app read.
            wiring=StackWiring(prefs_store=PrefsStore(events), models=opts.models, sessions=store),
            execution=opts.execution,
            bash_timeout_sec=bash_timeout_sec,
            permission_check=permission_check,
            search_config=search_config,
            checkpointer=checkpointer,
            front="terminal",
        )

        # Background approvals reach this run the same way they reach the REPL:
        # through the daemon when one is running, and only otherwise through a
        # local watcher. This path used to always start its own, which meant a
        # one-shot run that had *attached* to a daemon's host put a second
        # watcher on it — both asking the user, either able to resume.
        # Deferred: ~10 ms on every CLI start, needed only when background
        # subagents run.
        from yuyutsava.cli.background_approvals import BackgroundApprovals  # noqa: PLC0415

        approvals = await BackgroundApprovals.open(bundle)
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
            except Exception as exc:
                # A quota, a busy provider or an overlong conversation is said in
                # one line the reader can act on; anything else is a bug and
                # keeps its traceback.
                said = describe_model_failure(exc)
                if said is None:
                    raise
                print(f"Error: {said}", file=sys.stderr)
                return 1

            pulls = _parse_pull_paths(docker_pull_paths)
            if pulls:
                if bundle.docker_backend is None:
                    print(
                        "Warning: --docker-pull-paths only applies with --execution docker; ignored.",
                        file=sys.stderr,
                    )
                else:
                    layout = bundle.layout or WorkspaceLayout.for_workspace(workspace)
                    dest = (
                        (docker_settings.export_dir / "_pulled").resolve()
                        if docker_settings.export_dir is not None
                        else layout.outputs / "_pulled"
                    )
                    written = pull_virtual_paths_to_host(bundle.docker_backend, pulls, dest)
                    if verbose and written:
                        print(f"Docker pull wrote: {written}", file=sys.stderr)

            if final.strip() and not verbose:
                print(final.strip())

            # Flush any background-task events emitted during the run.
            await approvals.render_between_turns()
        finally:
            # Approvals first (this cancels in-flight background runs), then
            # the host — a best-effort daemon-thread teardown.
            await approvals.aclose()
            await bundle.aclose()
            # After teardown the docker container is gone, so nothing has the
            # sandbox as its cwd — safe to wipe scratch in either mode. Guarded
            # because a failure to tidy up must not become the exit status of a
            # turn that worked.
            if bundle.layout is not None:
                with contextlib.suppress(Exception):
                    cleanup_local_sandbox(bundle.layout)
    return 0
