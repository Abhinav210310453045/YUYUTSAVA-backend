"""A real MCP server for tests: it waits ``--delay`` seconds before answering, then offers ``echo``.

Run by the manager over stdio, like any configured server:
``python test/mcp_layer/slow_server.py --delay 1``.
"""

from __future__ import annotations

import argparse
import time

from mcp.server.fastmcp import FastMCP

parser = argparse.ArgumentParser()
parser.add_argument("--delay", type=float, default=0.0)
args = parser.parse_args()
time.sleep(args.delay)  # a server that is slow to come up, like `npx -y`

server = FastMCP("slow")


@server.tool()
def echo(text: str) -> str:
    """Return *text* unchanged."""
    return text


server.run()
