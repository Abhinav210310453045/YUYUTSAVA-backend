"""Pydantic schemas for the server-info endpoint (Phase 6).

The mobile app calls ``GET /v1/server-info`` once after connecting and
degrades gracefully: capability flags gate UI affordances (model routing
chips, memory hints, system-metrics dashboard, background-task panel) and
``channels`` feeds the Settings screen.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CapabilitiesOut(BaseModel):
    model_routing: bool = Field(
        description="Complexity-based model routing is enabled "
                    "(YUYUTSAVA_MODEL_ROUTING=1)",
    )
    memory: bool = Field(
        description="Semantic memory store is wired (mem_* tools, "
                    "RELEVANT MEMORY injection)",
    )
    resource_governor: bool = Field(
        description="ResourceMonitor is running — /system/metrics and "
                    "system_metrics SSE events are live",
    )
    async_subagents: bool = Field(
        description="Background subagent host is enabled "
                    "(YUYUTSAVA_ASYNC_SUBAGENTS=1) — async_task_* SSE "
                    "events may appear",
    )


class ChannelInfoOut(BaseModel):
    name: str
    available: bool
    enabled: bool
    running: bool
    capabilities: list[str]


class ServerInfoOut(BaseModel):
    name: str = "yuyutsava"
    version: str = Field(description="Installed yuyutsava package version")
    api_version: str = Field("v1", description="Frozen API contract revision")
    capabilities: CapabilitiesOut
    channels: list[ChannelInfoOut] = Field(
        default_factory=list,
        description="ChannelPluginRegistry snapshot; empty when the "
                    "registry isn't wired",
    )
