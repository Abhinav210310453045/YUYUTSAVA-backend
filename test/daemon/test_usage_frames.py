"""Context-meter snapshots reach the app, and cannot cost it anything.

The websocket handler subscribes to the meter for the duration of a turn and
forwards each snapshot as a ``usage`` frame. Three properties matter:

* frames are **scoped to one conversation** — a second chat's window must never
  land on this one's panel;
* the subscription **ends with the turn**, or a closed socket's channel keeps
  receiving snapshots forever;
* ``usage`` is **ephemeral**, so a long turn's snapshots cannot evict the reply
  from the 500-frame replay ring a reattaching client reads.

Run:  .venv/bin/python test/daemon/test_usage_frames.py
"""

from __future__ import annotations

import unittest

import yuyutsava.core.config  # noqa: F401 — import first (core/__init__ cycle)
from yuyutsava.context.meter import ContextSnapshot, bus
from yuyutsava.daemon.turn_registry import EPHEMERAL_TYPES, TURN_RING_SIZE


class UsageFramesAreEphemeral(unittest.TestCase):
    def test_a_usage_frame_never_displaces_prose_in_the_replay_ring(self):
        # A 30-call turn publishes 60+ snapshots. At 500 frames, treating them
        # as durable would push out the tokens a reattaching client needs.
        self.assertIn("usage", EPHEMERAL_TYPES)

    def test_prose_is_still_durable(self):
        for kind in ("token", "tool_call", "tool_result", "final", "turn_end"):
            self.assertNotIn(kind, EPHEMERAL_TYPES, kind)

    def test_the_ring_is_small_enough_for_this_to_matter(self):
        # If the ring were unbounded the ephemeral marking would be cosmetic;
        # it is not, which is why this is a correctness property.
        self.assertLessEqual(TURN_RING_SIZE, 2_000)


class SnapshotWireShape(unittest.TestCase):
    """The frame the app receives is ``{"type": "usage", **snapshot}``."""

    def test_it_is_json_safe_and_carries_what_a_panel_needs(self):
        import json

        snap = ContextSnapshot(
            thread_id="t1", model="gemini-3.5-flash", max_input_tokens=1_000_000,
            system_tokens=4_000, messages_tokens=29_200, call_no=3,
            input_tokens=41_087, cache_read_tokens=38_000, calls=3,
        )
        frame = {"type": "usage", **snap.as_dict()}
        json.dumps(frame)  # must not raise: it goes out over a websocket
        self.assertEqual(frame["type"], "usage")
        self.assertEqual(frame["thread_id"], "t1")
        self.assertIn("segments", frame)
        self.assertIn("used_fraction", frame)
        self.assertEqual(frame["call"]["input_tokens"], 41_087)
        self.assertEqual(frame["session"]["calls"], 3)

    def test_segments_arrive_pre_ordered_so_both_fronts_agree(self):
        frame = ContextSnapshot().as_dict()
        self.assertEqual(
            [s["key"] for s in frame["segments"]],
            ["system", "tools", "memory", "skills", "messages"],
        )


class SubscriptionScope(unittest.TestCase):
    """What the handler's ``_meter_frames`` context manager guarantees."""

    def setUp(self):
        bus().clear()

    def tearDown(self):
        bus().clear()

    def test_a_subscriber_hears_only_its_own_conversation(self):
        frames: list[dict] = []
        token = bus().subscribe("conv-a", lambda s: frames.append(s.as_dict()))
        try:
            bus().publish(ContextSnapshot(thread_id="conv-a", call_no=1))
            bus().publish(ContextSnapshot(thread_id="conv-b", call_no=2))
        finally:
            bus().unsubscribe(token)
        self.assertEqual([f["call"]["n"] for f in frames], [1])

    def test_unsubscribing_stops_frames_for_a_finished_turn(self):
        frames: list[int] = []
        token = bus().subscribe("conv-a", lambda s: frames.append(s.call_no))
        bus().publish(ContextSnapshot(thread_id="conv-a", call_no=1))
        bus().unsubscribe(token)
        bus().publish(ContextSnapshot(thread_id="conv-a", call_no=2))
        self.assertEqual(frames, [1])

    def test_a_failing_emit_cannot_break_the_turn(self):
        # The callback runs inside the agent's model step. A closed channel
        # raising there would fail the user's turn over telemetry.
        def boom(_snap):
            raise RuntimeError("channel closed")

        token = bus().subscribe("conv-a", boom)
        try:
            bus().publish(ContextSnapshot(thread_id="conv-a"))  # must not raise
        finally:
            bus().unsubscribe(token)


class HandlerWiring(unittest.TestCase):
    """Pin the shape of the handler's own hookup without a live socket."""

    def test_the_handler_subscribes_per_turn_and_unsubscribes(self):
        import inspect

        from yuyutsava.daemon.web.routers import converse

        src = inspect.getsource(converse)
        self.assertIn("_meter_frames", src)
        self.assertIn("bus().unsubscribe(token)", src)
        # Both turn bodies, text and voice, are wrapped.
        self.assertEqual(src.count("async with _meter_frames(run):"), 2)

    def test_the_handshake_carries_the_last_known_reading(self):
        import inspect

        from yuyutsava.daemon.web.routers import converse

        src = inspect.getsource(converse)
        self.assertIn('"usage": _latest_usage_snapshot(convo.thread_id)', src)

    def test_a_broken_snapshot_read_cannot_fail_the_handshake(self):
        from yuyutsava.daemon.web.routers.converse import _latest_usage_snapshot

        self.assertIsNone(_latest_usage_snapshot("no-such-thread"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
