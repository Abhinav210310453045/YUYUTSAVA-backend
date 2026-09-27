"""Opening a ``/ws/converse`` socket: who owns its asks, and when it is refused.

The ask surface decides where an ask raised in the conversation may render —
a tinker card's panel, the voice overlay, or the chat. A socket is refused
with a reason when there is no conversation manager or the conversation
cannot be opened.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from typing import Any

from yuyutsava.daemon.web.routers import converse as router


class AskSurface(unittest.TestCase):
    def test_each_kind_of_conversation(self) -> None:
        cases = {
            ("tinker", "card-1", "ui"): ("tinker", "TinkerAgent"),
            ("tinker", None, "ui"): ("chat", "Chat"),  # a tinker without a card is a chat
            ("master", None, "voice"): ("voice", "Voice"),
            ("tinker", "card-1", "voice"): ("tinker", "TinkerAgent"),  # the card wins
            ("master", None, "cli"): ("chat", "Chat"),
        }
        for args, expected in cases.items():
            with self.subTest(args=args):
                self.assertEqual(router._ask_surface(*args), expected)


class _WS:
    def __init__(self, params: dict, manager: Any) -> None:
        self.query_params = params
        self.app = SimpleNamespace(state=SimpleNamespace(conversation_manager=manager))
        self.sent: list[dict] = []
        self.closed = False

    async def accept(self) -> None:
        pass

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))

    async def close(self) -> None:
        self.closed = True


class _FailingManager:
    async def open(self, **kw: Any) -> Any:
        raise ValueError("tinker conversations require a card id (?card=)")


class Refusals(unittest.TestCase):
    def _run(self, ws: _WS) -> _WS:
        asyncio.run(asyncio.wait_for(router.converse(ws), 10))
        return ws

    def test_no_manager(self) -> None:
        ws = self._run(_WS({}, None))
        self.assertEqual(
            ws.sent, [{"type": "error", "message": "conversation manager not initialized"}]
        )
        self.assertTrue(ws.closed)

    def test_a_conversation_that_cannot_open_says_why(self) -> None:
        ws = self._run(_WS({"agent": "tinker"}, _FailingManager()))
        self.assertEqual(ws.sent[0]["message"], "tinker conversations require a card id (?card=)")
        self.assertTrue(ws.closed)


if __name__ == "__main__":
    unittest.main()
