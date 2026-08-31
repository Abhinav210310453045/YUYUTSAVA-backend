"""In-tree MCP servers shipped with YUYUTSAVA.

Each subpackage exposes a ``server`` module runnable as
``python -m yuyutsava.mcp_servers.<name>.server``. The daemon spawns them
through :class:`yuyutsava.mcp.loader.MCPClientManager` when listed in
``~/.yuyutsava/mcp_config.json``.
"""
