"""TaskRunner tools: Describing the machine the agent is on.

Split out of the 609-line ``bind_tools`` (`R-RDB-02`); the factory in
``__init__`` assembles the families.
"""

from __future__ import annotations

import json
import logging

from langchain_core.tools import BaseTool, tool

from yuyutsava.agents.task_runner.tools.support import (
    ToolContext,
)
from yuyutsava.platform import host_profile

_log = logging.getLogger("yuyutsava.agents.task_runner.tools.host")


def make_host_tools(ctx: ToolContext) -> list[BaseTool]:
    """Build the host tools bound to one workspace."""
    # tr_sysinfo asks the host, not the workspace, so it needs nothing from ctx.

    @tool
    async def tr_sysinfo(reason: str) -> str:
        """Report the host OS passport — OS/version/arch, native shell, package
        managers, service manager, elevation mechanism, and key paths.

        Call this before writing native tr_execute commands so you use the right
        dialect and tools (PowerShell vs bash, winget vs brew, SCM vs launchd).

        Args:
            reason: Why you need the host info (shown in the activity log).
        """

        hp = host_profile()
        return json.dumps(
            {
                "status": "success",
                "result": {
                    "os_family": hp.os_family,
                    "os_version": hp.os_version,
                    "arch": hp.arch,
                    "shell": hp.shell_kind,
                    "package_managers": list(hp.package_managers),
                    "service_manager": hp.service_manager,
                    "elevation": hp.elevation_mechanism,
                    "home": hp.home_dir,
                    "temp": hp.temp_dir,
                },
            }
        )

    return [
        tr_sysinfo,
    ]
