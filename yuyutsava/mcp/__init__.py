"""MCP (Model Context Protocol) client integration.

:class:`~yuyutsava.mcp.config.MCPConfig` loads and merges the global
``~/.yuyutsava/mcp_config.json`` with a trusted workspace's
``<ws>/.yuyutsava/mcp_config.json``; :class:`~yuyutsava.mcp.loader.MCPClientManager`
owns the lifecycle of every configured server. Tools discovered from each server
become LangChain tools named ``<server>__<tool>``, scoped per agent by the config's
``scopes`` map. The daemon starts one manager at boot and reloads it on SIGHUP; the
standalone CLI starts its own via
:func:`~yuyutsava.mcp.loader.start_manager_for_workspace`.

This package re-exports nothing, so reading MCP configuration does not load the MCP
SDK: import a name from the module that owns it.
"""
