"""TaskRunner tools: finding files and searching inside them.

Split out of the 609-line ``bind_tools`` (`R-RDB-02`); the factory in
``__init__`` assembles the families.
"""

from __future__ import annotations

import logging
import uuid

from langchain_core.tools import BaseTool, tool

from yuyutsava.agents.task_runner.tools.support import (
    ToolContext,
    resolve_path,
)
from yuyutsava.models.operations import OperationRequest, OperationType

_log = logging.getLogger("yuyutsava.agents.task_runner.tools.search")


def make_search_tools(ctx: ToolContext) -> list[BaseTool]:
    """Build the discovery tools bound to one workspace."""
    agent, workspace_root, agent_name = ctx.agent, ctx.workspace_root, ctx.agent_name

    @tool
    async def tr_ls(path: str, reason: str, max_entries: int = 500) -> str:
        """List directory entries at a real absolute path (zone-checked).

        Returns JSON {entries: [{name, path, type, size}], total, has_more}.
        Use this for ANY directory listing, including inside the workspace —
        the built-in ls is not available. WORKSPACE/SANDBOX zones auto-allow;
        EXTERNAL paths prompt the user once.

        Args:
            path:        Real absolute path of the directory to list.
            reason:      Specific purpose shown to the user in permission prompts.
            max_entries: Cap on returned entries (default 500).
        """
        real_path = resolve_path(path, workspace_root)
        _log.debug("[tr_ls] path=%s max=%d", real_path, max_entries)
        request = OperationRequest(
            request_id=str(uuid.uuid4()),
            requesting_agent=agent_name,
            task_id=str(uuid.uuid4()),
            task_description=reason,
            operation=OperationType.LIST,
            paths=[real_path],
            reason=reason,
            additional_context={"max_entries": max_entries},
        )
        response = await agent.handle(request)
        _log.debug("[tr_ls] status=%s", response.status)
        return response.model_dump_json()

    @tool
    async def tr_glob(
        pattern: str,
        path: str,
        reason: str,
        max_entries: int = 500,
    ) -> str:
        """Glob files matching a pattern under a real absolute path (zone-checked).

        Returns JSON {entries: [{name, path, type, size}], total, has_more, pattern, root}.
        Patterns use pathlib semantics: '*.pdf' shallow, '**/*.pdf' recursive.
        Use this for ANY pattern match, including inside the workspace — the
        built-in glob is not available. EXTERNAL paths prompt the user once.

        Args:
            pattern:     Glob pattern (e.g. '*.pdf', '**/*.py', 'README*').
            path:        Real absolute path of the root directory to search.
            reason:      Specific purpose shown to the user in permission prompts.
            max_entries: Cap on returned entries (default 500).
        """
        real_path = resolve_path(path, workspace_root)
        _log.debug("[tr_glob] pattern=%r path=%s max=%d", pattern, real_path, max_entries)
        request = OperationRequest(
            request_id=str(uuid.uuid4()),
            requesting_agent=agent_name,
            task_id=str(uuid.uuid4()),
            task_description=reason,
            operation=OperationType.GLOB,
            paths=[real_path],
            reason=reason,
            additional_context={"pattern": pattern, "max_entries": max_entries},
        )
        response = await agent.handle(request)
        _log.debug("[tr_glob] status=%s", response.status)
        return response.model_dump_json()

    @tool
    async def tr_grep(
        pattern: str,
        path: str,
        reason: str,
        context_lines: int = 3,
        case_insensitive: bool = False,
        max_matches: int = 100,
    ) -> str:
        """Search a regex pattern in a file or directory. Returns JSON with stdout (matches + line numbers).

        Use this, NOT the built-in grep (which only works on virtual paths).
        Pass real absolute paths; returned line numbers feed tr_read_file offset.
        A directory search skips .yuyutsava/ unless that dir IS the search root
        (pass the state dir explicitly to recall ChangeLog/assumptions/memory).

        Args:
            pattern:          Regex or fixed string to search for.
            path:             Real absolute path to a file or directory.
            reason:           Specific purpose shown to the user in permission prompts.
            context_lines:    Lines of context before/after each match (default 3).
            case_insensitive: Case-insensitive matching (default False).
            max_matches:      Stop after this many matches (default 100).
        """
        real_path = resolve_path(path, workspace_root)
        _log.debug("[tr_grep] pattern=%r path=%s", pattern, real_path)
        sandbox_path = str(agent.sandbox_root)
        # Pure-Python search (executor.execute_grep) — routed through EXECUTE so
        # the sandbox zone check is unchanged; no `grep` binary, no shell.
        request = OperationRequest(
            request_id=str(uuid.uuid4()),
            requesting_agent=agent_name,
            task_id=str(uuid.uuid4()),
            task_description=reason,
            operation=OperationType.EXECUTE,
            paths=[sandbox_path],
            reason=reason,
            additional_context={
                "search": {
                    "pattern": pattern,
                    "path": real_path,
                    "context_lines": context_lines,
                    "case_insensitive": case_insensitive,
                    "max_matches": max_matches,
                },
                "cwd": sandbox_path,
            },
        )
        response = await agent.handle(request)
        return response.model_dump_json()

    return [
        tr_ls,
        tr_glob,
        tr_grep,
    ]
