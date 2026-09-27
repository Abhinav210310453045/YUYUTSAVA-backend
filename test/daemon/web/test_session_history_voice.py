"""A reloaded chat's voice: voice sessions, and clips reattached to text chats.

`GET /sessions/{id}/messages` answers a voice session from ``voice_messages``
(with an ``audio_url`` per stored clip), and a text session from the
transcript — then gives each assistant bubble whose text matches a stored
spoken reply its clip back, each clip once, in order, so a reopened thread can
still replay what the agent said. A voice surface that fails leaves the
history text-only rather than failing it.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from yuyutsava.daemon.web.routers import sessions as router


def _t(seq: int, type_: str, text: str) -> SimpleNamespace:
    return SimpleNamespace(
        seq=seq, type=type_, created_ts=float(seq), content={"data": {"content": text}}
    )


def _v(
    seq: int, role: str, text: str, audio: bool = True, modality: str = "audio"
) -> SimpleNamespace:
    return SimpleNamespace(
        seq=seq, role=role, text=text, has_audio=audio, modality=modality, created_ts=float(seq)
    )


class _Rows:
    def __init__(self, rows=None, fail: bool = False) -> None:
        self.rows, self.fail = rows or [], fail

    async def list_messages(self, thread_id):
        if self.fail:
            raise RuntimeError("voice db down")
        return self.rows


def _history(origin: str, *, transcript=None, voice=None) -> list[dict]:
    session = SimpleNamespace(thread_id="t1", origin=origin)
    store = SimpleNamespace(get=mock.AsyncMock(return_value=session))
    state = SimpleNamespace(voice_store=voice, transcript_store=transcript)
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    out = asyncio.run(asyncio.wait_for(router.get_session_messages("s1", request, store=store), 10))
    return out["messages"]


class VoiceSessions(unittest.TestCase):
    def test_come_from_voice_messages_with_audio_urls(self) -> None:
        msgs = _history(
            "voice",
            voice=_Rows(
                [_v(1, "user", "hi", audio=False, modality="voice"), _v(2, "assistant", "hello")]
            ),
            transcript=_Rows([_t(9, "human", "ignored")]),
        )
        self.assertEqual(
            [(m["role"], m["text"], m["audio_url"], m["modality"]) for m in msgs],
            [
                ("user", "hi", None, "voice"),
                ("assistant", "hello", "/sessions/s1/audio/2", "audio"),
            ],
        )


class ClipsReattach(unittest.TestCase):
    def test_each_clip_once_in_order_by_text(self) -> None:
        msgs = _history(
            "chat",
            transcript=_Rows(
                [
                    _t(1, "human", "q1"),
                    _t(2, "ai", "same"),
                    _t(3, "human", "q2"),
                    _t(4, "ai", "same"),
                ]
            ),
            voice=_Rows(
                [
                    _v(10, "assistant", ""),  # text-less rows never match
                    _v(11, "assistant", " same "),
                    _v(12, "user", "same"),  # user clips are not reattached
                    _v(13, "assistant", "same"),
                    _v(14, "assistant", "unmatched"),
                ]
            ),
        )
        self.assertEqual(
            [(m["role"], m["audio_url"], m["modality"], m["has_audio"]) for m in msgs],
            [
                ("user", None, "text", False),
                ("assistant", "/sessions/s1/audio/11", "audio", True),
                ("user", None, "text", False),
                ("assistant", "/sessions/s1/audio/13", "audio", True),
            ],
        )

    def test_a_failing_voice_store_leaves_text_only(self) -> None:
        msgs = _history(
            "chat",
            transcript=_Rows([_t(1, "human", "q"), _t(2, "ai", "a")]),
            voice=_Rows(fail=True),
        )
        self.assertEqual([(m["text"], m["has_audio"]) for m in msgs], [("q", False), ("a", False)])

    def test_system_and_prose_less_rows_are_not_bubbles(self) -> None:
        msgs = _history(
            "chat",
            transcript=_Rows([_t(1, "system", "sys"), _t(2, "human", "q"), _t(3, "ai", "   ")]),
        )
        self.assertEqual([m["text"] for m in msgs], ["q"])

    def test_no_stores_is_an_empty_history(self) -> None:
        self.assertEqual(_history("chat"), [])


if __name__ == "__main__":
    unittest.main()
