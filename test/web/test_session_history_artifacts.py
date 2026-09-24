"""A reloaded chat carries the artifacts its live turns showed.

`GET /sessions/{id}/messages` replays the transcript. An artifact a tool made
attaches to the assistant prose that follows it — or to a text-less bubble of
its own when that turn had no prose — as the live `artifact` event did. Which
tools count is the artifacts package's `ARTIFACT_EVENT` rule, the same one the
graph driver applies, so the live stream and the replay cannot disagree.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from yuyutsava.daemon.web.routers import sessions as router


def _record(artifact_id: str) -> str:
    return json.dumps({"status": "ok", "artifact_id": artifact_id, "url": f"/a/{artifact_id}"})


def _row(seq: int, type_: str, content: str, name: str | None = None) -> SimpleNamespace:
    data = {"content": content}
    if name is not None:
        data["name"] = name
    return SimpleNamespace(seq=seq, type=type_, created_ts=float(seq), content={"data": data})


class _Transcript:
    def __init__(self, rows):
        self._rows = rows

    async def list_messages(self, thread_id):
        return self._rows


def _history(rows) -> list[dict]:
    session = SimpleNamespace(thread_id="t1", origin="chat")
    store = SimpleNamespace(get=mock.AsyncMock(return_value=session))
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(voice_store=None, transcript_store=_Transcript(rows))
        )
    )
    with mock.patch.object(router, "get_default_session_store", return_value=store):
        out = asyncio.run(asyncio.wait_for(router.get_session_messages("s1", request), 10))
    return out["messages"]


def _ids(message: dict) -> list[str]:
    return [a["artifact_id"] for a in message.get("artifacts", [])]


class ArtifactsFollowTheirTurn(unittest.TestCase):
    def test_both_artifact_tools_attach_to_the_next_prose(self) -> None:
        msgs = _history(
            [
                _row(1, "human", "make two"),
                _row(2, "tool", _record("art_a"), name="artifact_create"),
                _row(3, "tool", _record("art_b"), name="artifact_show"),
                _row(4, "ai", "here they are"),
            ]
        )
        self.assertEqual([m["text"] for m in msgs], ["make two", "here they are"])
        self.assertEqual(_ids(msgs[1]), ["art_a", "art_b"])

    def test_a_turn_without_prose_gets_its_own_bubble(self) -> None:
        msgs = _history(
            [
                _row(1, "human", "make one"),
                _row(2, "tool", _record("art_a"), name="artifact_create"),
                _row(3, "human", "thanks"),
            ]
        )
        self.assertEqual([(m["role"], m["text"]) for m in msgs][1], ("assistant", ""))
        self.assertEqual(_ids(msgs[1]), ["art_a"])

    def test_another_tool_with_an_artifact_shaped_result_is_not_an_artifact(self) -> None:
        msgs = _history(
            [
                _row(1, "human", "go"),
                _row(2, "tool", _record("art_x"), name="fs_read_file"),
                _row(3, "ai", "done"),
            ]
        )
        self.assertNotIn("artifacts", msgs[1])

    def test_a_failed_artifact_call_attaches_nothing(self) -> None:
        msgs = _history(
            [
                _row(1, "human", "go"),
                _row(2, "tool", '{"status": "error", "error": "nope"}', name="artifact_create"),
                _row(3, "ai", "that failed"),
            ]
        )
        self.assertNotIn("artifacts", msgs[1])


if __name__ == "__main__":
    unittest.main()
