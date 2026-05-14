"""MCP (Model Context Protocol) integration for YUYUTSAVA.

Exposes :class:`MCPConfig` (loaded from ``~/.yuyutsava/mcp_config.json``) and
:class:`MCPClientManager` (lifecycle of all configured MCP servers). Tools
discovered from each server are adapted to ``langchain_core.tools.BaseTool``
and scoped per agent via the config's ``scopes`` map.

See ``PHASE_2_PLAN.md`` §1 for the design.
"""

from yuyutsava.mcp.config import MCPConfig, MCPServerSpec
from yuyutsava.mcp.loader import MCPClientManager

__all__ = ["MCPConfig", "MCPServerSpec", "MCPClientManager"]
