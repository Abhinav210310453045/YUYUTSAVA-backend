"""Shared, I/O-agnostic agent conversation engine.

:class:`~yuyutsava.conversation.service.ConversationService` is the multi-turn loop
behind the terminal chat, the Electron text chat and the voice agent. See
:mod:`yuyutsava.conversation.service` for the design rationale.

``ConversationService`` is re-exported because other modules import it from here;
import anything else from the module that owns it.
"""

from yuyutsava.conversation.service import ConversationService

__all__ = ["ConversationService"]
