"""A reloaded chat never shows the repair's notes to the model as conversation.

`R-BUG-24`. The empty-turn repair closed a turn with "[interrupted] The user
interrupted this turn before I could finish…" — written into history as an
assistant message, so after a reload the app showed it as the agent's reply,
though the user had interrupted nothing. Repair closers and the retry's nudge
are marked internal, and `GET /sessions/{id}/messages` leaves them out.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from yuyutsava.conversation.repair import internal_kwargs
from yuyutsava.daemon.web.routers import sessions as router


def _row(seq: int, type_: str, content: str, *, internal: str | None = None) -> SimpleNamespace:
    data: dict = {"content": content}
    if internal is not None:
        data["additional_kwargs"] = internal_kwargs(internal)
    return SimpleNamespace(seq=seq, type=type_, created_ts=float(seq), content={"data": data})


class _Transcript:
    def __init__(self, rows):
        self._rows = rows

    async def list_messages(self, thread_id):
        return self._rows


def _history(rows) -> list[tuple[str, str]]:
    session = SimpleNamespace(thread_id="t1", origin="ui")
    store = SimpleNamespace(get=mock.AsyncMock(return_value=session))
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(voice_store=None, transcript_store=_Transcript(rows))
        )
    )
    out = asyncio.run(asyncio.wait_for(router.get_session_messages("s1", request, store=store), 10))
    return [(m["role"], m["text"]) for m in out["messages"]]


class InternalNotesStayOutOfTheHistory(unittest.TestCase):
    def test_a_repair_closer_and_a_retry_nudge_are_not_shown(self) -> None:
        shown = _history(
            [
                _row(1, "human", "plan my sunday"),
                _row(2, "human", "(Automatic retry …) Reply now.", internal="retry_nudge"),
                _row(3, "ai", "[no reply] I read the tool results above…", internal="empty_reply"),
                _row(4, "human", "continue"),
                _row(5, "ai", "Sunday: a 5k."),
            ]
        )
        self.assertEqual(
            shown,
            [("user", "plan my sunday"), ("user", "continue"), ("assistant", "Sunday: a 5k.")],
        )

    def test_ordinary_messages_are_untouched(self) -> None:
        self.assertEqual(
            _history([_row(1, "human", "hi"), _row(2, "ai", "hello")]),
            [("user", "hi"), ("assistant", "hello")],
        )


if __name__ == "__main__":
    unittest.main()
