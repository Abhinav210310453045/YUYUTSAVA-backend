"""File organizer subagent. Subclass of ``BaseSubAgent`` with ``fetch_event`` added."""

from __future__ import annotations

from langchain_core.tools import BaseTool

from yuyutsava.agents.base_sub_agent import BaseSubAgent
from yuyutsava.agents.file_organizer.prompts import FILE_ORGANIZER_PROMPT
from yuyutsava.agents.task_runner.agent import TaskRunnerAgent
from yuyutsava.events.store import Store
from yuyutsava.events.tools import make_fetch_event_tool
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
    ) -> None:
        super().__init__(task_runner, skill_registry=skill_registry)
        self._store = store

    @property
    def system_prompt(self) -> str:
        return FILE_ORGANIZER_PROMPT

    def extra_tools(self) -> list[BaseTool]:
        return [make_fetch_event_tool(self._store)]
