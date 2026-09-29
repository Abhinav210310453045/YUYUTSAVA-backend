"""Pydantic schemas for the MCP status endpoint."""

from __future__ import annotations

from pydantic import BaseModel, Field


class McpServerOut(BaseModel):
    name: str
    state: str = Field(description='"connected", "failed" or "not running"')
    tools: int = Field(description="Tools the server exposes (0 unless connected)")
    error: str = Field(description="Why it failed to start; empty otherwise")
    source: str = Field(description="The config file that defines it")


class McpStatusOut(BaseModel):
    config: str = Field(description="The user's global MCP config file")
    servers: list[McpServerOut]
