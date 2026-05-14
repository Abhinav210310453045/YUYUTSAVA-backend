"""File organizer subagent. Subclass of ``BaseSubAgent`` with ``fetch_event`` added."""

from __future__ import annotations

from langchain_core.tools import BaseTool

from yuyutsava.agents.base_sub_agent import BaseSubAgent
from yuyutsava.agents.file_organizer.prompts import make_file_organizer_prompt
from yuyutsava.agents.task_runner.agent import TaskRunnerAgent
from yuyutsava.core.config import SearchConfig
from yuyutsava.events.store import Store
from yuyutsava.events.tools import make_fetch_event_tool
from yuyutsava.mcp.loader import MCPClientManager
from yuyutsava.skills.registry import SkillRegistry


class FileOrganizerAgent(BaseSubAgent):
    name = "file-organizer"
    description = (
        "Move a single newly-arrived file into ~/Documents/Inbox/<year>/. "
        "Use for fs.changed events where the user wants a downloaded file tidied."
    )

    def __init__(
        self,
        task_runner: TaskRunnerAgent,
        store: Store,
        skill_registry: SkillRegistry | None = None,
        mcp_manager: MCPClientManager | None = None,
        search_config: SearchConfig | None = None,
        cap_enforcer: object | None = None,
    ) -> None:
        super().__init__(
            task_runner,
            skill_registry=skill_registry,
            mcp_manager=mcp_manager,
            search_config=search_config,
            cap_enforcer=cap_enforcer,
        )
        self._store = store

    @property
    def system_prompt(self) -> str:
        return make_file_organizer_prompt()

    def extra_tools(self) -> list[BaseTool]:
        return [make_fetch_event_tool(self._store)]
