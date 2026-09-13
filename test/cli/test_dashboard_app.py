"""The dashboard application: it composes, it binds, it hands the terminal back.

test_dashboard.py covers the pure pieces (panel geometry, formatting, the
transcript sink). This covers the parts that only exist once a prompt_toolkit
application is built:

* the layout composes and **renders a real frame** without raising — a broken
  container or a bad style key is otherwise only discovered by a user;
* the keys mean what the help text says: Enter submits, Ctrl+C cancels the turn
  (there is no SIGINT in raw mode), Ctrl+D quits only on an empty line, Ctrl+G
  toggles the panel, scroll keys stay in bounds;
* ``sys.stdout``/``sys.stderr`` are captured for the app's lifetime and
  **restored afterwards** — the whole reason ~75 print sites in chat_repl did
  not have to change, and a leak would corrupt every later command;
* existing logging handlers are retargeted and put back.

Run:  .venv/bin/python test/cli/test_dashboard_app.py
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import unittest

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

import yuyutsava.core.config  # noqa: F401 — import first (core/__init__ cycle)
from yuyutsava.cli.render.dashboard import ChatDashboard
from yuyutsava.context.meter import ContextSnapshot, bus




class _DashboardCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        bus().clear()
        self._pipe_cm = create_pipe_input()
        self._pipe = self._pipe_cm.__enter__()
        self.dash = ChatDashboard(
            history_path=None, output=DummyOutput(), input=self._pipe,
        )

    async def asyncTearDown(self) -> None:
        with contextlib_suppress():
            await self.dash.stop()
        self._pipe_cm.__exit__(None, None, None)
        bus().clear()


def contextlib_suppress():
    return contextlib.suppress(Exception)


class LayoutComposes(_DashboardCase):
    async def test_a_real_frame_renders_without_raising(self):
        """Actually run the application for one frame, then exit it.

        A missing style class or a malformed container raises inside the
        renderer, which unit-testing the fragments cannot catch.
        """
        app = self.dash._app
        loop = asyncio.get_running_loop()
        loop.call_later(0.2, lambda: app.exit() if app.is_running else None)
        await asyncio.wait_for(app.run_async(), timeout=10)
        self.assertFalse(app.is_running)

    async def test_the_panel_and_transcript_split_the_width(self):
        self.assertEqual(
            self.dash.left_width + self.dash._panel_width + 1, self.dash._cols
        )

    async def test_hiding_the_panel_gives_the_width_to_the_transcript(self):
        before = self.dash.left_width
        self.dash.toggle_panel()
        self.assertGreater(self.dash.left_width, before)
        self.assertEqual(self.dash.console.width, self.dash.left_width)

    async def test_the_rich_console_is_sized_to_the_left_pane(self):
        self.assertEqual(self.dash.console.width, self.dash.left_width)


class BindingTable(_DashboardCase):
    async def test_every_documented_key_is_actually_bound(self):
        # The help text promises these; a typo in a key name would make it lie.
        from prompt_toolkit.keys import Keys as K

        bound = set()
        for binding in self.dash._app.key_bindings.bindings:
            bound.update(str(k) for k in binding.keys)
        for key in (K.ControlM, K.ControlC, K.ControlD, K.PageUp, K.PageDown,
                    K.End, K.ControlL, K.ControlG, K.ScrollUp, K.ScrollDown):
            self.assertIn(str(key), bound, key)


class Keys(_DashboardCase):
    async def test_enter_submits_the_line_and_clears_the_buffer(self):
        self.dash._buf.text = "hello there"
        self.dash.submit()
        self.assertEqual(await asyncio.wait_for(self.dash.read_input(), 2),
                         "hello there")
        self.assertEqual(self.dash._buf.text, "")

    async def test_enter_follows_the_output_again(self):
        # Submitting means "I am done reading back"; leaving the view parked
        # 200 lines up would hide the reply that is about to arrive.
        self.dash.buffer.write("x\n" * 100)
        self.dash._scroll = 50
        self.dash._buf.text = "go"
        self.dash.submit()
        self.assertEqual(self.dash._scroll, 0)

    async def test_ctrl_c_cancels_the_registered_turn(self):
        async def _long():
            await asyncio.sleep(30)

        task = asyncio.create_task(_long())
        self.dash.set_turn_task(task)
        self.dash.interrupt()
        await asyncio.sleep(0)
        self.assertTrue(task.cancelling() or task.cancelled() or task.done())
        task.cancel()
        # CancelledError is a BaseException; suppress(Exception) misses it.
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def test_ctrl_c_with_no_turn_clears_the_line(self):
        self.dash.set_turn_task(None)
        self.dash._buf.text = "half typed"
        self.dash.interrupt()
        self.assertEqual(self.dash._buf.text, "")

    async def test_ctrl_d_quits_only_on_an_empty_line(self):
        self.dash._buf.text = "mid-line"
        self.dash.request_quit()
        self.assertTrue(self.dash._queue.empty())
        self.dash._buf.text = ""
        self.dash.request_quit()
        self.assertIsNone(await asyncio.wait_for(self.dash.read_input(), 2))

    async def test_scrolling_stays_within_the_transcript(self):
        self.dash.buffer.write("line\n" * 10)
        self.dash.page_up()
        self.assertGreaterEqual(self.dash._scroll, 0)
        for _ in range(50):
            self.dash.page_up()
        self.assertLessEqual(self.dash._scroll, self.dash.buffer.line_count())
        for _ in range(200):
            self.dash.page_down()
        self.assertEqual(self.dash._scroll, 0)

    async def test_end_jumps_back_to_live(self):
        self.dash.buffer.write("line\n" * 200)
        self.dash._scroll = 90
        self.dash.follow()
        self.assertEqual(self.dash._scroll, 0)

    async def test_ctrl_l_clears_the_transcript(self):
        self.dash.buffer.write("gone\n")
        self.dash.clear_transcript()
        self.assertEqual(self.dash.buffer.lines(), [])


class VisibleSlice(_DashboardCase):
    async def test_only_the_visible_window_is_converted(self):
        # Converting 5,000 lines per frame is O(everything); this is why the
        # buffer stores pre-wrapped lines and the slice happens first.
        self.dash.buffer.write("".join(f"line {i}\n" for i in range(1_000)))
        frags = self.dash._visible_transcript()
        text = "".join(t for _s, t in frags)
        self.assertLessEqual(len(text.split("\n")), self.dash._height() + 1)
        self.assertIn("line 999", text)
        self.assertNotIn("line 0\n", text)

    async def test_scrolling_up_shows_earlier_lines(self):
        self.dash.buffer.write("".join(f"line {i}\n" for i in range(1_000)))
        self.dash._scroll = 500
        text = "".join(t for _s, t in self.dash._visible_transcript())
        self.assertNotIn("line 999", text)

    async def test_an_empty_transcript_renders_nothing(self):
        self.assertEqual(self.dash._visible_transcript(), [])

    async def test_a_stray_escape_byte_cannot_blank_the_pane(self):
        self.dash.buffer.write("\x1b[not-a-real-sequence\ntext\n")
        text = "".join(t for _s, t in self.dash._visible_transcript())
        self.assertIn("text", text)

    async def test_the_status_line_reports_being_scrolled_back(self):
        self.dash.buffer.write("line\n" * 200)
        self.dash._scroll = 30
        text = "".join(t for _s, t in self.dash._status_text())
        self.assertIn("End to follow", text)

    async def test_a_renderer_that_raises_does_not_blank_the_status(self):
        class Boom:
            def status_text(self):
                raise RuntimeError("nope")

            def tool_in_flight(self):
                raise RuntimeError("nope")

        self.dash.attach_renderer(Boom())
        self.assertIsInstance(self.dash._status_text(), list)
        self.assertIsInstance(self.dash._panel_text(), list)


class SnapshotsReachThePanel(_DashboardCase):
    async def test_a_published_snapshot_appears_in_the_panel(self):
        await self.dash.start()
        bus().publish(ContextSnapshot(
            thread_id="t1", model="gemini-3.5-flash", max_input_tokens=1_000_000,
            messages_tokens=29_200, call_no=3, input_tokens=41_087,
        ))
        text = "".join(t for _s, t in self.dash._panel_text())
        self.assertIn("gemini-3.5-flash", text)
        self.assertIn("41.1k", text)

    async def test_a_client_starting_mid_session_sees_the_last_snapshot(self):
        bus().publish(ContextSnapshot(thread_id="t1", model="already-running",
                                      max_input_tokens=1_000))
        await self.dash.start()
        text = "".join(t for _s, t in self.dash._panel_text())
        self.assertIn("already-running", text)

    async def test_the_subscription_is_dropped_on_stop(self):
        await self.dash.start()
        await self.dash.stop()
        before = self.dash._snapshot
        bus().publish(ContextSnapshot(thread_id="t1", model="after-stop",
                                      max_input_tokens=1))
        self.assertIs(self.dash._snapshot, before)


class StreamCapture(_DashboardCase):
    async def test_stdout_and_stderr_are_captured_and_restored(self):
        saved = (sys.stdout, sys.stderr)
        await self.dash.start()
        self.assertIs(sys.stdout, self.dash.buffer)
        self.assertIs(sys.stderr, self.dash.buffer)
        print("a print from anywhere")
        await self.dash.stop()
        self.assertEqual((sys.stdout, sys.stderr), saved)
        self.assertIn("a print from anywhere", "\n".join(self.dash.buffer.lines()))

    async def test_streams_are_restored_even_if_the_app_was_never_running(self):
        saved = (sys.stdout, sys.stderr)
        self.dash._install_streams()
        self.dash._restore_streams()
        self.assertEqual((sys.stdout, sys.stderr), saved)

    async def test_an_existing_log_handler_is_retargeted_and_put_back(self):
        # StreamHandler binds its stream at construction, so handlers that
        # already exist keep writing to the real terminal unless retargeted —
        # straight through a full-screen application.
        root = logging.getLogger()
        handler = logging.StreamHandler(sys.stderr)
        root.addHandler(handler)
        try:
            original = handler.stream
            self.dash._install_streams()
            self.assertIs(handler.stream, self.dash.buffer)
            logging.getLogger("test.dashboard").error("logged during the app")
            self.dash._restore_streams()
            self.assertIs(handler.stream, original)
        finally:
            root.removeHandler(handler)
        self.assertIn("logged during the app", "\n".join(self.dash.buffer.lines()))

    async def test_the_transcript_can_be_handed_back_on_exit(self):
        # The alternate screen takes the session's output with it; a user who
        # just spent an hour in here should not find an empty terminal.
        class Sink:
            def __init__(self):
                self.text = ""

            def write(self, s):
                self.text += s

            def flush(self):
                pass

        self.dash.buffer.write("first\nsecond\n")
        sink = Sink()
        self.dash.replay_to(sink)
        self.assertEqual(sink.text, "first\nsecond\n")




if __name__ == "__main__":
    unittest.main(verbosity=2)
