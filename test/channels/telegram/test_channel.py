"""TelegramChannelPlugin behavior against a fake Bot API client.

Run:  uv run python -m unittest test.channels.telegram.test_channel -v
"""

from __future__ import annotations

import asyncio
import time
import unittest

from yuyutsava.channels.plugin import InboundSink
from yuyutsava.channels.telegram.channel import (
    OFFSET_STATE_KEY,
    TelegramChannelPlugin,
)
from yuyutsava.daemon.channels import (
    AskPrompt,
    ChannelEvent,
    HttpLogPayload,
    LogPayload,
    TimelinePayload,
    TokenPayload,
)
from yuyutsava.daemon.web.services.decision_service import DecisionService
from yuyutsava.storage.events import Proposal

ALLOWED_CHAT = 111
STRANGER_CHAT = 999


class FakeTelegramClient:
    def __init__(self) -> None:
        self.sent: list[dict] = []        # sendMessage calls
        self.edited: list[dict] = []
        self.answered: list[tuple[str, str]] = []
        self.commands: list[dict] | None = None
        self.closed = False
        self._updates: asyncio.Queue[list[dict]] = asyncio.Queue()
        self._next_message_id = 1000

    def feed(self, *updates: dict) -> None:
        self._updates.put_nowait(list(updates))

    async def get_updates(self, offset=None, *, timeout=50):
        try:
            return self._updates.get_nowait()
        except asyncio.QueueEmpty:
            await asyncio.sleep(0.005)
            return []

    async def send_message(self, chat_id, text, *, reply_markup=None, parse_mode="HTML"):
        self._next_message_id += 1
        self.sent.append({
            "chat_id": chat_id, "text": text,
            "reply_markup": reply_markup, "parse_mode": parse_mode,
            "message_id": self._next_message_id,
        })
        return {"message_id": self._next_message_id}

    async def edit_message_text(self, chat_id, message_id, text, *, reply_markup=None, parse_mode="HTML"):
        self.edited.append({"chat_id": chat_id, "message_id": message_id, "text": text})
        return {}

    async def answer_callback_query(self, callback_query_id, *, text=""):
        self.answered.append((callback_query_id, text))
        return True

    async def set_my_commands(self, commands):
        self.commands = commands
        return True

    async def get_me(self):
        return {"username": "fake_bot"}

    async def aclose(self) -> None:
        self.closed = True


class _FakeSubmission:
    def __init__(self) -> None:
        self.submitted: list[tuple[str, str]] = []

    async def submit_direct(self, instruction, *, origin="api", session_hint=None):
        self.submitted.append((instruction, origin))
        return f"tsk_{len(self.submitted)}"


class _FakeTaskRegistry:
    def __init__(self, records=()) -> None:
        self._records = list(records)

    async def list(self, *, status=None, limit=50, cursor=None):
        return [r for r in self._records if r.status == status], None


class _FakeDecisionStore:
    def try_set_proposal_status(self, proposal_id, *, from_status, to_status):
        return True


class _FakePrefs:
    def __init__(self) -> None:
        self.data: dict = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    async def set(self, key, value) -> None:
        self.data[key] = value


def make_stack(records=()):
    client = FakeTelegramClient()
    decisions = DecisionService(_FakeDecisionStore())
    prefs = _FakePrefs()
    submission = _FakeSubmission()
    sink = InboundSink(
        task_submission=submission,
        decision_service=decisions,
        task_registry=_FakeTaskRegistry(records),
        prefs_store=prefs,
        status_provider=lambda: "daemon: ok",
    )
    decisions.add_waiters(
        proposals=sink.pending_proposals, asks=sink.pending_asks,
    )
    plugin = TelegramChannelPlugin(
        client, (ALLOWED_CHAT,), poll_timeout_sec=1, debounce_sec=0.02,
    )
    return plugin, client, sink, submission, prefs


def text_update(chat_id: int, text: str, *, update_id: int = 1, reply_to: int | None = None) -> dict:
    msg: dict = {"chat": {"id": chat_id}, "text": text, "message_id": update_id + 500}
    if reply_to is not None:
        msg["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": update_id, "message": msg}


def callback_update(chat_id: int, data: str, *, update_id: int = 1) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cq{update_id}",
            "data": data,
            "message": {"chat": {"id": chat_id}, "message_id": 42},
        },
    }


async def settle(condition, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        await asyncio.sleep(0.01)


class OutboundTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.plugin, self.client, self.sink, self.submission, self.prefs = make_stack()
        await self.plugin.start(self.sink)

    async def asyncTearDown(self) -> None:
        await self.plugin.stop()

    async def test_tokens_and_http_logs_suppressed(self) -> None:
        await self.plugin.post_event(ChannelEvent(payload=TokenPayload(text="tok")))
        await self.plugin.post_event(ChannelEvent(payload=HttpLogPayload(
            method="GET", path="/x", status=200, duration_ms=1, ts=0.0,
        )))
        await asyncio.sleep(0.1)
        self.assertEqual(self.client.sent, [])

    async def test_logs_debounced_into_batches(self) -> None:
        await self.plugin.post_event(ChannelEvent(payload=LogPayload(text="line one")))
        await self.plugin.post_event(ChannelEvent(payload=LogPayload(text="line two")))
        self.assertEqual(self.client.sent, [])  # not sent synchronously
        await settle(lambda: len(self.client.sent) >= 1)
        self.assertEqual(len(self.client.sent), 1)
        self.assertIn("line one", self.client.sent[0]["text"])
        self.assertIn("line two", self.client.sent[0]["text"])

    async def test_completion_flushes_immediately(self) -> None:
        await self.plugin.post_event(ChannelEvent(payload=TimelinePayload(
            line="orchestrator: all done", cls="event-action",
        )))
        await settle(lambda: len(self.client.sent) >= 1, timeout=0.5)
        self.assertIn("orchestrator: all done", self.client.sent[0]["text"])


class ProposalAskTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.plugin, self.client, self.sink, self.submission, self.prefs = make_stack()
        await self.plugin.start(self.sink)

    async def asyncTearDown(self) -> None:
        await self.plugin.stop()

    def _proposal(self, *, expiry_sec: int = 30) -> Proposal:
        return Proposal.new(
            event_id="ev1", topic="user.task.submitted", summary="do a thing",
            proposed="do a thing carefully", subagent="general-purpose",
            urgency=2, expiry_sec=expiry_sec,
        )

    async def test_proposal_approved_via_button(self) -> None:
        p = self._proposal()
        task = asyncio.create_task(self.plugin.post_proposal(p))
        await settle(lambda: any("Proposal" in m["text"] for m in self.client.sent))
        keyboard = self.client.sent[0]["reply_markup"]["inline_keyboard"][0]
        self.assertEqual(
            [b["callback_data"] for b in keyboard],
            [f"p:{p.proposal_id}:a", f"p:{p.proposal_id}:s", f"p:{p.proposal_id}:m"],
        )
        self.client.feed(callback_update(ALLOWED_CHAT, f"p:{p.proposal_id}:a"))
        decision = await asyncio.wait_for(task, timeout=2.0)
        self.assertEqual(decision.decision, "approve")
        # Outcome edited into the original message; callback answered.
        await settle(lambda: len(self.client.edited) >= 1)
        self.assertIn("approve", self.client.edited[0]["text"])
        self.assertTrue(self.client.answered)

    async def test_proposal_modify_flow(self) -> None:
        p = self._proposal()
        task = asyncio.create_task(self.plugin.post_proposal(p))
        await settle(lambda: len(self.client.sent) >= 1)
        self.client.feed(callback_update(ALLOWED_CHAT, f"p:{p.proposal_id}:m"))
        await settle(lambda: any("modified instruction" in m["text"] for m in self.client.sent))
        prompt_id = next(
            m["message_id"] for m in self.client.sent
            if "modified instruction" in m["text"]
        )
        self.client.feed(text_update(
            ALLOWED_CHAT, "actually do Z", update_id=2, reply_to=prompt_id,
        ))
        decision = await asyncio.wait_for(task, timeout=2.0)
        self.assertEqual(decision.decision, "modify")
        self.assertEqual(decision.edited_instruction, "actually do Z")

    async def test_proposal_expires(self) -> None:
        p = self._proposal(expiry_sec=-10)  # already past expires_ts → 1s floor
        decision = await asyncio.wait_for(self.plugin.post_proposal(p), timeout=5.0)
        self.assertEqual(decision.decision, "expired")

    async def test_ask_with_options_keyboard(self) -> None:
        ask = AskPrompt(
            ask_id="ask1", title="Permission: WRITE", body="write ~/x?",
            options=["approve", "reject"], interrupt_value={},
        )
        task = asyncio.create_task(self.plugin.post_ask(ask))
        await settle(lambda: len(self.client.sent) >= 1)
        self.client.feed(callback_update(ALLOWED_CHAT, "a:ask1:0"))
        self.assertEqual(await asyncio.wait_for(task, timeout=2.0), "approve")

    async def test_ask_free_text_via_force_reply(self) -> None:
        ask = AskPrompt(
            ask_id="ask2", title="Question", body="which dir?",
            options=[], interrupt_value={},
        )
        task = asyncio.create_task(self.plugin.post_ask(ask))
        await settle(lambda: len(self.client.sent) >= 1)
        sent = self.client.sent[0]
        self.assertEqual(sent["reply_markup"], {"force_reply": True})
        self.client.feed(text_update(
            ALLOWED_CHAT, "~/Documents", update_id=3, reply_to=sent["message_id"],
        ))
        self.assertEqual(await asyncio.wait_for(task, timeout=2.0), "~/Documents")


class InboundTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.plugin, self.client, self.sink, self.submission, self.prefs = make_stack()
        await self.plugin.start(self.sink)

    async def asyncTearDown(self) -> None:
        await self.plugin.stop()

    async def test_plain_text_submits_task(self) -> None:
        self.client.feed(text_update(ALLOWED_CHAT, "summarize ~/Downloads", update_id=7))
        await settle(lambda: self.submission.submitted)
        self.assertEqual(
            self.submission.submitted, [("summarize ~/Downloads", "telegram")],
        )
        await settle(lambda: any("task submitted" in m["text"] for m in self.client.sent))

    async def test_non_allowlisted_chat_dropped(self) -> None:
        self.client.feed(text_update(STRANGER_CHAT, "rm -rf /", update_id=8))
        await asyncio.sleep(0.1)
        self.assertEqual(self.submission.submitted, [])
        self.assertEqual(self.client.sent, [])

    async def test_status_command(self) -> None:
        self.client.feed(text_update(ALLOWED_CHAT, "/status", update_id=9))
        await settle(lambda: any("daemon: ok" in m["text"] for m in self.client.sent))
        self.assertEqual(self.submission.submitted, [])

    async def test_tasks_command_empty(self) -> None:
        self.client.feed(text_update(ALLOWED_CHAT, "/tasks", update_id=10))
        await settle(lambda: any("No queued or running tasks" in m["text"]
                                 for m in self.client.sent))

    async def test_offset_persisted_and_resumed(self) -> None:
        self.client.feed(text_update(ALLOWED_CHAT, "/status", update_id=41))
        await settle(lambda: self.prefs.data.get(OFFSET_STATE_KEY) == 42)
        await self.plugin.stop()

        # A fresh plugin instance resumes from the persisted offset.
        client2 = FakeTelegramClient()
        plugin2 = TelegramChannelPlugin(
            client2, (ALLOWED_CHAT,), poll_timeout_sec=1, debounce_sec=0.02,
        )
        await plugin2.start(self.sink)
        self.assertEqual(plugin2._offset, 42)
        await plugin2.stop()
        self.assertTrue(client2.closed)


class FromConfigTests(unittest.TestCase):
    def test_missing_token_raises(self) -> None:
        import os
        old_tok = os.environ.pop("YUYUTSAVA_TELEGRAM_BOT_TOKEN", None)
        old_ids = os.environ.pop("YUYUTSAVA_TELEGRAM_CHAT_IDS", None)
        try:
            with self.assertRaises(ValueError):
                TelegramChannelPlugin.from_config({})
            os.environ["YUYUTSAVA_TELEGRAM_BOT_TOKEN"] = "123:abc"
            with self.assertRaises(ValueError):
                TelegramChannelPlugin.from_config({})  # chat ids still missing
            os.environ["YUYUTSAVA_TELEGRAM_CHAT_IDS"] = "111, 222"
            plugin = TelegramChannelPlugin.from_config({"poll_timeout_sec": 10})
            self.assertEqual(plugin._chat_ids, (111, 222))
            self.assertEqual(plugin._poll_timeout, 10)
        finally:
            for key, val in (
                ("YUYUTSAVA_TELEGRAM_BOT_TOKEN", old_tok),
                ("YUYUTSAVA_TELEGRAM_CHAT_IDS", old_ids),
            ):
                if val is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = val


if __name__ == "__main__":
    unittest.main()
