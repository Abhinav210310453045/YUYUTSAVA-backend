"""Canonical filesystem paths for every persisted yuyutsava artifact.

Single place to look up "where does X live on disk?". Every store, sweeper,
and introspector resolves its target through one of these functions so a
test fixture can override the location with one env var.

Path-returning helpers are **pure** — they compute and return paths without
touching the filesystem. Directory materialization is the caller's job
(typically once at sync startup via :func:`ensure_state_dirs`). Async
stores additionally mkdir-on-open via ``asyncio.to_thread`` as defence in
depth; doing the sync mkdir inside ``async def`` would trip
``blockbuster`` when ``langgraph dev`` is in the process.

Two roots
---------
- **Global state** — ``state_dir()`` (``~/.yuyutsava``): per-user skills,
  agent memory, blobs, the SQLite files, ``mcp_config.json`` …
- **Per-workspace state** — :class:`WorkspaceLayout`
  (``<workspace>/.yuyutsava/``): the ONLY place yuyutsava writes inside a
  workspace — scratch sandbox, reusable scripts, deliverables, deepagents
  temp, the project knowledge files and the workspace-level MCP config.
  ``WorkspaceLayout.ensure()`` is sync like ``ensure_state_dirs``; call it
  from a sync entry point, or wrap it in ``asyncio.to_thread``.

Env overrides
-------------
- ``YUYUTSAVA_HOME``           override state dir (default: ``~/.yuyutsava``)
- ``YUYUTSAVA_SESSIONS_DB``    override sessions.db path
- ``YUYUTSAVA_STATE_DB``       override state.db path (events/proposals/rules/quotas/prefs)
- ``YUYUTSAVA_CHECKPOINTS_DB`` override checkpoints.db path (LangGraph saver)
- ``YUYUTSAVA_INTERRUPTS_DB``  override interrupts.db path (HITL audit)
- ``YUYUTSAVA_BLOBS_DIR``      override blobs/ root (webcam frames, audio clips)
- ``YUYUTSAVA_SANDBOX_DIR`` / ``YUYUTSAVA_OUTPUT_DIR`` — per-workspace sandbox /
  deliverables overrides, applied by the caller through ``WorkspaceLayout``
  (see ``core.config.LocalSettings``)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("yuyutsava.storage.paths")


def state_dir() -> Path:
    """Per-user state directory. Pure path; create via :func:`ensure_state_dirs`.

    Holds every SQLite file and the ``blobs/`` subtree. Override with
    ``YUYUTSAVA_HOME``.
    """
    raw = os.environ.get("YUYUTSAVA_HOME", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".yuyutsava"


def sessions_db_path() -> Path:
    """SQLite file backing the CLI session index."""
    raw = os.environ.get("YUYUTSAVA_SESSIONS_DB", "").strip()
    return Path(raw).expanduser() if raw else state_dir() / "sessions.db"


def state_db_path() -> Path:
    """SQLite file backing events, proposals, decisions, rules, quotas, prefs.

    Currently owned by ``yuyutsava.events.store.Store``; in Step 2 the stores
    split into ``storage/events/`` and ``storage/prefs.py`` but the DB file
    stays the same so existing data is preserved.
    """
    raw = os.environ.get("YUYUTSAVA_STATE_DB", "").strip()
    return Path(raw).expanduser() if raw else state_dir() / "state.db"


def checkpoints_db_path() -> Path:
    """SQLite file backing the LangGraph ``AsyncSqliteSaver`` checkpointer."""
    raw = os.environ.get("YUYUTSAVA_CHECKPOINTS_DB", "").strip()
    return Path(raw).expanduser() if raw else state_dir() / "checkpoints.db"


def interrupts_db_path() -> Path:
    """SQLite file backing the cross-front HITL interrupt audit log."""
    raw = os.environ.get("YUYUTSAVA_INTERRUPTS_DB", "").strip()
    return Path(raw).expanduser() if raw else state_dir() / "interrupts.db"


def blobs_dir() -> Path:
    """Root directory for source-produced blobs (webcam JPEGs, audio clips)."""
    raw = os.environ.get("YUYUTSAVA_BLOBS_DIR", "").strip()
    return Path(raw).expanduser() if raw else state_dir() / "blobs"


def channels_config_path() -> Path:
    """User-state path for ``channels_config.json`` (channel plugins).

    Under ``state_dir()`` (unlike ``events_config_path``) because which
    channels a user enabled — and their params — is per-user runtime
    state, not a project artifact. Override with ``YUYUTSAVA_CHANNELS_CONFIG``.
    """
    raw = os.environ.get("YUYUTSAVA_CHANNELS_CONFIG", "").strip()
    return Path(raw).expanduser() if raw else state_dir() / "channels_config.json"


def events_config_path() -> Path:
    """Repo-local path for ``events_config.json``.

    Not under ``state_dir()`` because the source registry config is a
    project artifact, not user runtime state. Sits next to the events
    package so a fresh clone has working defaults.
    """
    return Path(__file__).resolve().parent.parent / "events" / "events_config.json"


def ensure_state_dirs() -> None:
    """Create every state directory the app writes to. Sync, idempotent.

    Call once from the sync entry point (CLI ``main``, daemon ``main``)
    before ``asyncio.run`` — the path helpers above are pure, so something
    has to create the dirs, and doing it inside the event loop trips
    ``blockbuster`` when the LangGraph dev host is in-process.
    """
    state_dir().mkdir(parents=True, exist_ok=True)
    blobs_dir().mkdir(parents=True, exist_ok=True)
    for p in (
        sessions_db_path(),
        state_db_path(),
        checkpoints_db_path(),
        interrupts_db_path(),
    ):
        p.parent.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Per-workspace state — <workspace>/.yuyutsava/
# ---------------------------------------------------------------------------

WORKSPACE_STATE_DIRNAME = ".yuyutsava"

# Where the two workspace roots appear INSIDE the Docker sandbox container:
# the state dir is mounted read-write (and is the container workdir / the
# deepagents virtual root); the workspace itself is mounted read-only.
CONTAINER_STATE_MOUNT = "/yuyutsava"
CONTAINER_WORKSPACE_MOUNT = "/workspace"

# Written once into <ws>/.yuyutsava/.gitignore. Everything in the directory is
# machine-local — scratch, temp, deliverables, per-checkout knowledge — so it
# is ignored wholesale, the way virtualenv/uv ignore .venv. The user's own
# .gitignore is never touched.
WORKSPACE_GITIGNORE = "*\n"

_CHANGELOG_SEED = """\
# ChangeLog

<!-- Appended by YUYUTSAVA after every task that changed files. One entry per task:
## <ISO-8601 UTC> · <agent role> · <task gist, ≤80 chars>
- <path relative to workspace> — <what changed> (<file|module|config|docs|test|deps>)
-->
"""

_ASSUMPTIONS_SEED = """\
# Assumptions

<!-- Appended by YUYUTSAVA when it settles something the user left ambiguous:
## <ISO-8601 UTC> · <task gist>
- ASSUMED: <what> — because <why>
-->
"""

_MEMORY_SEED = """\
# Workspace memory

<!-- Durable facts about THIS workspace, appended by YUYUTSAVA. One dated, tagged bullet each:
- <YYYY-MM-DD> [layout|conventions|gotchas|decisions] <fact>
-->
"""

_home_collision_warned = False


@dataclass(frozen=True)
class WorkspaceLayout:
    """Every path yuyutsava owns inside one workspace — the single source of truth.

    ::

        <ws>/.yuyutsava/
          .gitignore            "*" — the whole directory is machine-local
          ChangeLog.md          what changed, per task (appended by the agent)
          Assumptions.md        what the agent assumed when the user was ambiguous
          workspace_memory.md   durable facts about this workspace
          mcp_config.json       optional workspace-level MCP servers/scopes
          skills/<name>/SKILL.md  workspace-scope skills
          scripts/              reusable agent-written scripts (WORKSPACE zone)
          outputs/              deliverables (WORKSPACE zone)
          sandbox/              scratch (SANDBOX zone) — created on demand,
                                wiped after a CLI task
          tmp/                  deepagents scratch: large_tool_results/,
                                conversation_history/

    Build one with :meth:`for_workspace`; never join these names by hand.
    All fields are absolute. ``sandbox`` and ``outputs`` honour the explicit
    ``YUYUTSAVA_SANDBOX_DIR`` / ``YUYUTSAVA_OUTPUT_DIR`` overrides when the
    caller passes them through.

    The one special case: a workspace whose ``.yuyutsava`` *is* the global
    state dir (running from ``~``, or ``YUYUTSAVA_HOME`` pointing inside the
    workspace). Its per-workspace state is routed to
    ``state_dir()/workspaces/home`` so the two never share a directory.
    """

    root: Path
    state: Path
    sandbox: Path
    outputs: Path
    scripts: Path
    tmp: Path
    skills: Path
    changelog: Path
    assumptions: Path
    memory: Path
    mcp_config: Path
    gitignore: Path

    @classmethod
    def for_workspace(
        cls,
        workspace: Path | str,
        *,
        sandbox_override: Path | None = None,
        outputs_override: Path | None = None,
    ) -> WorkspaceLayout:
        """Pure: derive the layout for *workspace*. Touches no filesystem."""
        root = Path(workspace).expanduser().resolve()
        state = root / WORKSPACE_STATE_DIRNAME
        if state == state_dir().expanduser().resolve():
            state = state_dir() / "workspaces" / "home"
            _warn_home_collision_once(root, state)
        return cls(
            root=root,
            state=state,
            sandbox=(
                sandbox_override.expanduser().resolve()
                if sandbox_override is not None else state / "sandbox"
            ),
            outputs=(
                outputs_override.expanduser().resolve()
                if outputs_override is not None else state / "outputs"
            ),
            scripts=state / "scripts",
            tmp=state / "tmp",
            skills=state / "skills",
            changelog=state / "ChangeLog.md",
            assumptions=state / "Assumptions.md",
            memory=state / "workspace_memory.md",
            mcp_config=state / "mcp_config.json",
            gitignore=state / ".gitignore",
        )

    @property
    def large_tool_results(self) -> Path:
        """deepagents' eviction cache (offloaded large tool results)."""
        return self.tmp / "large_tool_results"

    @property
    def conversation_history(self) -> Path:
        """deepagents' summarization transcript dumps."""
        return self.tmp / "conversation_history"

    def ensure(self, *, seed_files: bool = True) -> None:
        """Materialize the layout. Sync, idempotent.

        Creates ``state``, ``scripts``, ``outputs`` and ``tmp``; writes the
        ``.gitignore`` and seeds the three knowledge files **only when they
        are absent** (never rewrites user/agent content). Deliberately does
        NOT create ``sandbox`` — the executor creates it on the first write
        or run, and the CLI wipes it after each task.
        """
        for d in (self.state, self.scripts, self.outputs, self.tmp):
            d.mkdir(parents=True, exist_ok=True)
        if not seed_files:
            return
        for path, body in (
            (self.gitignore, WORKSPACE_GITIGNORE),
            (self.changelog, _CHANGELOG_SEED),
            (self.assumptions, _ASSUMPTIONS_SEED),
            (self.memory, _MEMORY_SEED),
        ):
            if not path.exists():
                path.write_text(body, encoding="utf-8")


def _warn_home_collision_once(root: Path, state: Path) -> None:
    global _home_collision_warned
    if _home_collision_warned:
        return
    _home_collision_warned = True
    logger.warning(
        "workspace %s contains the global state dir; its per-workspace state "
        "lives at %s instead", root, state,
    )
