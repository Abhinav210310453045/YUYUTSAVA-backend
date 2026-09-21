"""File organizer subagent. Subclass of ``BaseSubAgent`` with ``fetch_event`` added."""

from __future__ import annotations

from yuyutsava.agents.event_driven import EventDrivenSubAgent
from yuyutsava.agents.file_organizer.prompts import make_file_organizer_prompt


class FileOrganizerAgent(EventDrivenSubAgent):
    name = "file-organizer"
    description = (
        "Move a single newly-arrived file into ~/Documents/Inbox/<year>/. "
        "Use for fs.changed events where the user wants a downloaded file tidied."
    )

    @property
    def system_prompt(self) -> str:
        return make_file_organizer_prompt()
