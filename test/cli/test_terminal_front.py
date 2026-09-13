"""What the terminal front got wrong, and must not get wrong again.

From the 12 Sep session, in the order the user hit them:

* the agent built an HTML artifact for a table (a saved memory says the user
  prefers HTML tables) and the renderer printed one grey line, so the user
  asked "where the fuck are they" — the terminal has no Artifacts tab;
* Ctrl+C during a turn printed a twenty-frame traceback and killed the REPL
  instead of returning the prompt;
* the banner said ``storage: sqlite`` while every store was on Postgres.

The artifact tests carry one load-bearing invariant: rendering must not put
the artifact body back into the conversation. The body is on disk, the agent
already wrote it, and re-sending it would duplicate the largest single
argument in the session on every later call.

Run:  .venv/bin/python test/cli/test_terminal_front.py
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["YUYUTSAVA_BLOBS_DIR"] = tempfile.mkdtemp(prefix="artifact-view-")

import yuyutsava.core.config  # noqa: F401,E402 — import first (core/__init__ cycle)
from yuyutsava.artifacts import store  # noqa: E402
from yuyutsava.cli.render import artifact_view as av  # noqa: E402
from yuyutsava.cli.render.console import make_console  # noqa: E402
from yuyutsava.cli.render.plain import ChatRenderer  # noqa: E402
from yuyutsava.cli.render.renderer import RichChatRenderer  # noqa: E402
from yuyutsava.core.prompts import (  # noqa: E402
    docker_system_prompt,
    front_block,
    local_system_prompt,
)
from yuyutsava.core.streaming import StreamEvent  # noqa: E402
from yuyutsava.storage.paths import WorkspaceLayout  # noqa: E402

TERMINAL_MARK = "THIS CONVERSATION IS A TERMINAL"


def event_for(rec) -> StreamEvent:
    """The exact payload ``_artifact_event_from_result`` builds."""
    return StreamEvent("artifact", {
        "artifact_id": rec.artifact_id,
        "attachment_id": rec.artifact_id,
        "url": rec.url,
        "kind": rec.kind,
        "mime": rec.mime,
        "title": rec.title,
    })


def render_rich(ev: StreamEvent, width: int = 76) -> str:
    console = make_console()
    console.width = width
    renderer = RichChatRenderer(verbose=False, workspace=None, console=console)
    with console.capture() as cap:
        asyncio.run(renderer.render(ev))
    return cap.get()


class ArtifactsAppearInTheTranscript(unittest.TestCase):
    def test_markdown_renders_inside_a_box(self):
        rec = store.create_from_content(
            "markdown", "# Top chats\n\n| name | unread |\n|---|---|\n| Club | 566 |\n",
            title="Top 10 WhatsApp Chats",
        )
        out = render_rich(event_for(rec))
        self.assertIn("Top 10 WhatsApp Chats", out)
        self.assertIn("Top chats", out)   # the body, not just the title
        self.assertIn("566", out)
        self.assertIn("╭", out)           # bounded, not loose text
        self.assertIn("╰", out)
        self.assertIn(rec.artifact_id, out)

    def test_json_is_pretty_printed(self):
        rec = store.create_from_content("json", '{"a":1,"b":[1,2]}', title="Data")
        out = render_rich(event_for(rec))
        self.assertIn('"a": 1', out)

    def test_html_cannot_be_drawn_so_it_offers_the_file(self):
        rec = store.create_from_content("html", "<h1>hi</h1>", title="Chat table")
        out = render_rich(event_for(rec))
        self.assertIn("cannot be displayed in a terminal", out)
        self.assertIn("open", out)

    def test_a_huge_document_is_clipped_with_a_pointer(self):
        rec = store.create_from_content("text", "x" * (av.INLINE_CHARS * 3), title="Big")
        out = render_rich(event_for(rec))
        self.assertIn("clipped", out)
        self.assertLess(len(out), av.INLINE_CHARS * 2)

    def test_unknown_id_falls_back_to_one_line(self):
        out = render_rich(StreamEvent("artifact", {"artifact_id": "gone", "title": "Ghost"}))
        self.assertIn("◨ artifact: Ghost", out)

    def test_a_render_failure_degrades_instead_of_raising(self):
        rec = store.create_from_content("markdown", "# hi", title="Boom")
        with mock.patch.object(av, "artifact_renderable", side_effect=RuntimeError("x")):
            out = render_rich(event_for(rec))
        self.assertIn("◨ artifact: Boom", out)

    def test_plain_renderer_no_longer_drops_them(self):
        rec = store.create_from_content("text", "hello from the artifact", title="Note")
        renderer = ChatRenderer(verbose=False)
        # The plain renderer writes to stderr; capture it.
        import io
        from contextlib import redirect_stderr

        buf = io.StringIO()
        with redirect_stderr(buf):
            asyncio.run(renderer.render(event_for(rec)))
        out = buf.getvalue()
        self.assertIn("Note", out)
        self.assertIn("hello from the artifact", out)


class RenderingNeverFeedsBackIntoContext(unittest.TestCase):
    """The invariant: display reads from disk, never widens the event."""

    def test_the_event_payload_is_untouched_by_rendering(self):
        rec = store.create_from_content("markdown", "# body " * 500, title="Doc")
        ev = event_for(rec)
        before = copy.deepcopy(ev.data)

        render_rich(ev)

        self.assertEqual(ev.data, before)
        self.assertEqual(json.dumps(ev.data, sort_keys=True),
                         json.dumps(before, sort_keys=True))

    def test_the_payload_never_carries_the_body(self):
        # Guards the upstream shape too: if _artifact_event_from_result ever
        # started including content, every later model call would re-send it.
        rec = store.create_from_content("markdown", "SENTINEL-BODY-TEXT", title="Doc")
        ev = event_for(rec)
        self.assertNotIn("SENTINEL-BODY-TEXT", json.dumps(ev.data))
        self.assertLess(len(json.dumps(ev.data)), 400)

    def test_the_tool_result_itself_stays_metadata_only(self):
        # artifact_create returns id/kind/mime/title/path/url — this is what
        # actually enters the conversation.
        from yuyutsava.artifacts.tools import _ok

        rec = store.create_from_content("markdown", "SENTINEL-BODY-TEXT", title="Doc")
        payload = _ok(rec)
        self.assertNotIn("SENTINEL-BODY-TEXT", payload)
        self.assertIn(rec.artifact_id, payload)


class FrontAwarePrompt(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.layout = WorkspaceLayout.for_workspace(Path(tempfile.mkdtemp()))

    def test_terminal_gets_the_block_and_the_app_does_not(self):
        self.assertIn(TERMINAL_MARK, local_system_prompt(self.layout, front="terminal"))
        self.assertNotIn(TERMINAL_MARK, local_system_prompt(self.layout))
        self.assertNotIn(TERMINAL_MARK, local_system_prompt(self.layout, front="app"))

    def test_docker_mode_too(self):
        self.assertIn(
            TERMINAL_MARK, docker_system_prompt(self.layout, None, front="terminal")
        )
        self.assertNotIn(TERMINAL_MARK, docker_system_prompt(self.layout, None))

    def test_the_block_overrides_the_remembered_html_preference(self):
        block = front_block("terminal")
        self.assertIn("Markdown", block)
        self.assertIn("HTML", block)
        self.assertIn("no card to click", block)

    def test_unknown_front_is_treated_as_the_app(self):
        self.assertEqual(front_block("something-else"), "")

    def test_the_repl_asks_for_the_terminal_front(self):
        import inspect

        from yuyutsava.cli.commands import chat, chat_repl

        self.assertIn('front="terminal"', inspect.getsource(chat_repl.run_chat_repl))
        self.assertIn('front="terminal"', inspect.getsource(chat.run_chat))

    def test_the_daemon_does_not(self):
        import inspect

        from yuyutsava.daemon import conversation_manager as cm

        self.assertNotIn("front=", inspect.getsource(cm.ConversationManager._build_master_bundle))


class InterruptKeepsTheSessionOpen(unittest.TestCase):
    """Ctrl+C mid-turn arrives as CancelledError, not KeyboardInterrupt."""

    def test_the_handler_catches_both(self):
        import inspect

        from yuyutsava.cli.commands import chat_repl

        src = inspect.getsource(chat_repl.run_chat_repl)
        self.assertIn("except (KeyboardInterrupt, asyncio.CancelledError):", src)
        self.assertIn("task.uncancel()", src)

    def test_uncancel_lets_a_cancelled_task_carry_on(self):
        # The mechanism the handler relies on: without uncancel() the next
        # await re-raises and asyncio.Runner converts the still-cancelled task
        # into a KeyboardInterrupt exit.
        async def turn_loop():
            done = []
            for i in range(3):
                try:
                    if i == 1:
                        asyncio.current_task().cancel()
                    await asyncio.sleep(0)
                except asyncio.CancelledError:
                    asyncio.current_task().uncancel()
                    done.append("cancelled")
                    continue
                done.append(f"turn{i}")
            return done

        self.assertEqual(asyncio.run(turn_loop()), ["turn0", "cancelled", "turn2"])

    def test_the_entry_point_exits_quietly(self):
        import inspect

        from yuyutsava.cli import cli

        src = inspect.getsource(cli.main)
        self.assertIn("except KeyboardInterrupt:", src)
        self.assertIn("return 130", src)


class BannerAndNoise(unittest.TestCase):
    def test_banner_reports_the_effective_backend(self):
        from yuyutsava.cli.commands.chat_repl import _startup_status_line
        from yuyutsava.storage.sessions import SessionsSettings

        settings = SessionsSettings.from_env()
        with mock.patch(
            "yuyutsava.storage.backend.StorageSettings.is_postgres", return_value=True
        ):
            self.assertIn("storage: postgres", _startup_status_line(settings))
        with mock.patch(
            "yuyutsava.storage.backend.StorageSettings.is_postgres", return_value=False
        ):
            # Falls back to the sessions knob, which is the checkpointer's.
            self.assertIn(f"storage: {settings.backend}", _startup_status_line(settings))

    def test_grpc_verbosity_is_pinned_by_the_entry_points(self):
        import inspect

        from yuyutsava.cli import cli
        from yuyutsava.daemon import main as daemon_main

        for mod in (cli, daemon_main):
            with self.subTest(module=mod.__name__):
                src = inspect.getsource(mod)
                self.assertIn('os.environ.setdefault("GRPC_VERBOSITY", "ERROR")', src)
        # Importing the CLI is enough to set it.
        self.assertEqual(os.environ.get("GRPC_VERBOSITY"), "ERROR")


if __name__ == "__main__":
    unittest.main(verbosity=2)
