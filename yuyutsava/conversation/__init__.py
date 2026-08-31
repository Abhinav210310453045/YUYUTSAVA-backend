"""Shared, I/O-agnostic agent conversation engine.

``ConversationService`` is the single multi-turn loop behind every human↔agent
interface (CLI terminal, Electron text chat, voice agent). See
:mod:`yuyutsava.conversation.service` for the design rationale.
"""

from yuyutsava.conversation.service import (
    AskHandler,
    ConversationService,
    EventSink,
)

__all__ = ["ConversationService", "EventSink", "AskHandler"]
