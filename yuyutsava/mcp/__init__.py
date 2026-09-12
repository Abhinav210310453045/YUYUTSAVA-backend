"""MCP (Model Context Protocol) integration for YUYUTSAVA.

:class:`MCPConfig` loads and merges the global ``~/.yuyutsava/mcp_config.json``
with a trusted workspace's ``<ws>/.yuyutsava/mcp_config.json``;
:class:`MCPClientManager` owns the lifecycle of every configured server. Tools
discovered from each server are adapted to ``langchain_core.tools.BaseTool``
(``<server>__<tool>``) and scoped per agent via the config's ``scopes`` map.
The daemon starts one manager at boot (and hot-reloads it on SIGHUP); the
standalone CLI starts its own via :func:`~yuyutsava.mcp.loader.start_manager_for_workspace`.
"""

from yuyutsava.mcp.config import MCPConfig, MCPServerSpec
from yuyutsava.mcp.loader import MCPClientManager

__all__ = ["MCPConfig", "MCPServerSpec", "MCPClientManager"]
