"""TaskRunner tools: reading, writing and deleting files.

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

_log = logging.getLogger("yuyutsava.agents.task_runner.tools.files")


def make_files_tools(ctx: ToolContext) -> list[BaseTool]:
    """Build the files tools bound to one workspace."""
    agent, workspace_root, agent_name = ctx.agent, ctx.workspace_root, ctx.agent_name

    @tool
    async def tr_read_file(
        path: str,
        reason: str,
        offset: int = 0,
        limit: int | None = None,
    ) -> str:
        """Read a file (zone-checked). Returns JSON {content, has_more, truncation_notice, total_lines}.

        Paginate large files via offset/limit; result.has_more + result.truncation_notice
        give the next offset. After tr_grep, feed the matched line number as offset.

        Args:
            path:   Absolute real path (convert virtual ls/glob paths first).
            reason: Specific purpose shown to the user in permission prompts.
            offset: 0-based line to start from (default 0).
            limit:  Max lines per call (None = read to EOF).
        """
        real_path = resolve_path(path, workspace_root)
        _log.debug("[tr_read_file] path=%s offset=%s limit=%s", real_path, offset, limit)
        request = OperationRequest(
            request_id=str(uuid.uuid4()),
            requesting_agent=agent_name,
            task_id=str(uuid.uuid4()),
            task_description=reason,
            operation=OperationType.READ,
            paths=[real_path],
            reason=reason,
            additional_context={"offset": offset, "limit": limit},
        )
        response = await agent.handle(request)
        _log.debug("[tr_read_file] status=%s", response.status)
        return response.model_dump_json()

    @tool
    async def tr_write_file(
        path: str,
        content: str,
        reason: str,
        append: bool = False,
    ) -> str:
        """Write/create a file (zone-checked, creates parent dirs). Returns JSON {status, result: {written_to}, error}.

        First write into the sandbox creates the sandbox dir.
        Deliverables → output_dir (from system prompt); scratch → sandbox.
        append=True appends to the file (creating it if missing) instead of
        overwriting — use it for ChangeLog.md / Assumptions.md /
        workspace_memory.md, which are append-only.

        Args:
            path: Absolute real path to write.
            content: Text content to write.
            reason: Specific purpose shown to user in permission prompts.
            append: Append instead of overwrite (default False).
        """
        real_path = resolve_path(path, workspace_root)
        _log.debug(
            "[tr_write_file] path=%s bytes=%d append=%s",
            real_path,
            len(content.encode()),
            append,
        )
        request = OperationRequest(
            request_id=str(uuid.uuid4()),
            requesting_agent=agent_name,
            task_id=str(uuid.uuid4()),
            task_description=reason,
            operation=OperationType.WRITE,
            paths=[real_path],
            reason=reason,
            additional_context={"content": content, "append": append},
        )
        response = await agent.handle(request)
        _log.debug("[tr_write_file] status=%s", response.status)
        return response.model_dump_json()

    @tool
    async def tr_delete_file(path: str, reason: str) -> str:
        """Delete a file or directory (zone-checked). Returns JSON {status, result: {deleted}, error}.

        Use to clean up temp scripts after tr_execute_in_sandbox.
        WORKSPACE zone prompts the user; SANDBOX is auto-allowed.

        Args:
            path: Absolute real path to delete.
            reason: Specific purpose shown to user in permission prompts.
        """
        real_path = resolve_path(path, workspace_root)
        _log.debug("[tr_delete_file] path=%s", real_path)
        request = OperationRequest(
            request_id=str(uuid.uuid4()),
            requesting_agent=agent_name,
            task_id=str(uuid.uuid4()),
            task_description=reason,
            operation=OperationType.DELETE,
            paths=[real_path],
            reason=reason,
        )
        response = await agent.handle(request)
        _log.debug("[tr_delete_file] status=%s", response.status)
        return response.model_dump_json()

    return [
        tr_read_file,
        tr_write_file,
        tr_delete_file,
    ]
