"""The MCP connectors, as the daemon sees them (R-UI-12).

A server that failed to start used to be visible only in the daemon log. This
is the same per-server record the agent's prompt is built from, for a client
to show: which servers are connected with how many tools, which failed and why.
Read-only.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from yuyutsava.daemon.web.schemas.mcp import McpServerOut, McpStatusOut
from yuyutsava.mcp.config import global_mcp_config_path

router = APIRouter(tags=["mcp"])


@router.get(
    "/mcp/status",
    response_model=McpStatusOut,
    summary="Every configured MCP server: connected (with its tools) or why not",
)
async def mcp_status(request: Request) -> McpStatusOut:
    manager = getattr(request.app.state, "mcp_manager", None)
    statuses = manager.status() if manager is not None else []
    return McpStatusOut(
        config=str(global_mcp_config_path()),
        servers=[McpServerOut(**s.as_dict()) for s in statuses],
    )
