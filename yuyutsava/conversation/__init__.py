"""Shared, I/O-agnostic agent conversation engine.

:class:`~yuyutsava.conversation.service.ConversationService` is the multi-turn loop
behind the terminal chat, the Electron text chat and the voice agent. See
:mod:`yuyutsava.conversation.service` for the design rationale.

- :mod:`~yuyutsava.conversation.service` — the turn loop itself
- :mod:`~yuyutsava.conversation.sessions` — the session row a turn runs against:
  written before the first model call so a killed process stays resumable
- :mod:`~yuyutsava.conversation.repair` — putting a thread back in order after an
  interrupted turn
- :mod:`~yuyutsava.conversation.titles` — naming a session from what was said in it

``ConversationService`` is re-exported because other modules import it from here;
import anything else from the module that owns it.
"""

from yuyutsava.conversation.service import ConversationService

__all__ = ["ConversationService"]
