"""A subagent that an event wakes, and that has to be able to read that event.

`face-watcher` and `file-organizer` are both triggered by something on the
timeline — a frame, a file appearing — and neither can do its job without
fetching the event that woke it. They each wrote the same constructor and the
same one-line `extra_tools`, differing only in a docstring and one default
(`R-DRY-05`).

The store is the whole difference from :class:`BaseSubAgent`, so it is the whole
of this class: take it, forward everything else, and hand the agent its
`fetch_event` tool.
"""

from __future__ import annotations

from langchain_core.tools import BaseTool

from yuyutsava.agents.base_sub_agent import BaseSubAgent
from yuyutsava.agents.task_runner.agent import TaskRunnerAgent
from yuyutsava.config.search import SearchConfig
from yuyutsava.events.tools import make_fetch_event_tool
from yuyutsava.mcp.loader import MCPClientManager
from yuyutsava.skills.registry import SkillRegistry
from yuyutsava.storage.events import Store


class EventDrivenSubAgent(BaseSubAgent):
    """Base for subagents woken by a timeline event."""

    #: Whether this agent may write skills when the caller does not say. A
    #: class attribute rather than a signature default, because the two agents
    #: sharing this constructor disagree about it and both answers have to
    #: survive: ``can_write_skills=None`` means "use mine".
    learns_by_default: bool = True

    def __init__(
        self,
        task_runner: TaskRunnerAgent,
        store: Store,
        skill_registry: SkillRegistry | None = None,
        can_write_skills: bool | None = None,
        mcp_manager: MCPClientManager | None = None,
        search_config: SearchConfig | None = None,
        cap_enforcer: object | None = None,
        memory_store: object | None = None,
        skill_store: object | None = None,
        visual_store: object | None = None,
    ) -> None:
        super().__init__(
            task_runner,
            skill_registry=skill_registry,
            can_write_skills=(
                self.learns_by_default if can_write_skills is None else can_write_skills
            ),
            mcp_manager=mcp_manager,
            search_config=search_config,
            cap_enforcer=cap_enforcer,
            memory_store=memory_store,
            skill_store=skill_store,
            visual_store=visual_store,
        )
        self._store = store

    def extra_tools(self) -> list[BaseTool]:
        """``fetch_event``, so the agent can read what woke it."""
        return [make_fetch_event_tool(self._store)]


__all__ = ["EventDrivenSubAgent"]
