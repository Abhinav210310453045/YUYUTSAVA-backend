"""Face-watcher subagent.

Consumes a single ``face.frame`` event, calls the DeepFace MCP server's
``identify`` tool, returns a one-line summary. Enrollment is explicitly
out of scope — that path runs through a user-approved proposal, never
on the subagent's own initiative.

DeepFace tools are not bundled here: they arrive via ``mcp_tools()`` if
``mcp_config.json`` lists ``"face-watcher": ["deepface"]`` under scopes.

TODO(future): authorized-user presence gating.
    Planned follow-up feature: the daemon proactively schedules face.frame
    captures (not just motion-triggered ones) and uses this subagent to
    confirm that the *authorized* user is in front of the screen. If the
    identified person is not in the enrolled-as-allowed set, the daemon:
      1. Displays an in-renderer notice ("You are not authorised to use
         this session").
      2. Optionally pings the authorised user out-of-band (push notification,
         Slack, SMS — provider TBD) to confirm whether the unknown user
         should be granted access.
      3. Locks the orchestrator/proposal stream until confirmation arrives
         or a timeout elapses (re-locks workstation, etc.).
    Implementation outline when the time comes:
      - New event source ``yuyutsava/events/sources/presence_check.py`` that
        fires ``face.frame`` on a schedule (e.g. every 60s), independent of
        the motion-triggered ``webcam`` source.
      - New consent-rule category in ``core/policy.py``:
        ``presence.authorised_identities = [...]``.
      - New "lockout" channel state in ``ChannelRouter`` that drops
        non-confirmation asks while gated.
      - Re-use existing deepface ``identify`` tool — no model changes.
    Out of scope right now: implement only when the user explicitly asks.
"""

from __future__ import annotations

from langchain_core.tools import BaseTool

from yuyutsava.agents.base_sub_agent import BaseSubAgent
from yuyutsava.agents.face_watcher.prompts import FACE_WATCHER_PROMPT
from yuyutsava.agents.task_runner.agent import TaskRunnerAgent
from yuyutsava.core.config import SearchConfig
from yuyutsava.storage.events import Store
from yuyutsava.events.tools import make_fetch_event_tool
from yuyutsava.mcp.loader import MCPClientManager
from yuyutsava.skills.registry import SkillRegistry


class FaceWatcherAgent(BaseSubAgent):
    name = "face-watcher"
    description = (
        "Identify the person in a face.frame event using the deepface MCP tools. "
        "Use for face.frame events when the user wants presence-aware behaviour. "
        "Does not enroll new identities."
    )

    def __init__(
        self,
        task_runner: TaskRunnerAgent,
        store: Store,
        skill_registry: SkillRegistry | None = None,
        can_write_skills: bool = False,
        mcp_manager: MCPClientManager | None = None,
        search_config: SearchConfig | None = None,
        cap_enforcer: object | None = None,
        memory_store: object | None = None,
        skill_store: object | None = None,
    ) -> None:
        super().__init__(
            task_runner,
            skill_registry=skill_registry,
            can_write_skills=can_write_skills,
            mcp_manager=mcp_manager,
            search_config=search_config,
            cap_enforcer=cap_enforcer,
            memory_store=memory_store,
            skill_store=skill_store,
        )
        self._store = store

    @property
    def system_prompt(self) -> str:
        return FACE_WATCHER_PROMPT

    def extra_tools(self) -> list[BaseTool]:
        return [make_fetch_event_tool(self._store)]
