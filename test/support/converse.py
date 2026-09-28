"""A conversation manager and conversation for tests of the converse sockets.

One copy of what ``test_converse_ws``, ``test_converse_frames`` and
``test_voice_ws`` each defined (R-TST-03). Copies drift: when the real
interface changed, some were updated and some were not.
``test/daemon/web/test_fake_conformance.py`` holds these to
:class:`~yuyutsava.daemon.conversation_manager.ConversationManager` and
:class:`~yuyutsava.conversation.service.ConversationService` by signature, so a
drift in either fails there rather than in whichever socket test notices first.
"""

from __future__ import annotations

import asyncio

from yuyutsava.core.streaming import StreamEvent
from yuyutsava.daemon.turn_registry import TurnRegistry


class FakeConversation:
    """One conversation whose turns stream *reply* then *final*.

    A turn whose text contains ``"slow"`` sleeps long enough to be interrupted.
    One containing ``"ask me"`` raises a question through the ask handler and
    streams the answer back as ``[answer]``, so a test can tell "the socket was
    told the ask resolved" apart from "the agent actually received the answer".
    """

    def __init__(self, origin: str, *, reply: str, final: str, seen: list[str]) -> None:
        self.session_id = "sess-123"
        self.thread_id = f"{origin}-123"
        self.finished: str | None = None
        self._reply = reply
        self._final = final
        self._seen = seen

    @property
    def bundle_ready(self) -> bool:
        return True

    async def discard_if_unused(self) -> bool:
        return False

    async def run_turn(self, text, *, on_event, ask_handler=None, retry=False, **_kw) -> str:
        self._seen.append("(retry)" if retry else text)
        await on_event(StreamEvent("token", {"text": self._reply}))
        if "slow" in text:
            await asyncio.sleep(5)  # long enough for the test to interrupt it
        if "ask me" in text and ask_handler is not None:
            answer = await ask_handler({"type": "user_question", "question": "proceed?"})
            await on_event(StreamEvent("token", {"text": f"[{answer}]"}))
        await on_event(StreamEvent("final", {"text": self._final}))
        return self._final

    async def finish(self, status: str = "done") -> None:
        self.finished = status


class FakeConversationManager:
    """Opens :class:`FakeConversation`\\ s and owns the turn registry, as the daemon's does.

    Records each ``open`` as ``(origin, resume_id, continue_latest)`` and the
    text of every turn, across all conversations it opened.
    """

    def __init__(self, *, reply: str = "hi ", final: str = "done") -> None:
        self.turns = TurnRegistry()
        self.opened: list[tuple] = []
        self.turn_texts: list[str] = []
        self._reply = reply
        self._final = final

    async def open(
        self, *, agent="master", card_id=None, origin="cli", resume_id=None, continue_latest=False
    ):
        self.opened.append((origin, resume_id, continue_latest))
        convo = FakeConversation(origin, reply=self._reply, final=self._final, seen=self.turn_texts)
        return convo, False

    def start_turn(self, thread_id: str, **kw):
        return self.turns.start(thread_id=thread_id, **kw)

    def record_async_launch(self, ev, *, thread_id, origin=None) -> None: ...
