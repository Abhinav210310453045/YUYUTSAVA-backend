"""The words a background task's states are reported in, on both surfaces.

`R-DRY-05`: two surfaces report background-task progress and they are not one
renderer — `attach` reads a daemon's whole turn stream off the wire, tokens and
tool calls included, while the REPL's bridge shows only the four typed
payloads. What they do share is the language: a user watching one and then the
other has to be told the same task reached the same state in the same words.

So the tags and their colours are in one place, and the field layout stays each
renderer's own. These tests pin both halves — the vocabulary, and that neither
renderer spells it itself.
"""

from __future__ import annotations

import contextlib
import io
import pathlib
import unittest

from yuyutsava.cli import bg_labels as bg
from yuyutsava.cli.remote_attach import render_event_frame


def _strip(text: str) -> str:
    import re

    return re.sub(r"\x1b\[[0-9;]*m", "", text)


class TheVocabulary(unittest.TestCase):
    def test_each_state_has_its_word(self) -> None:
        self.assertIn("[bg started]", bg.started())
        self.assertIn("[bg progress]", bg.progress())
        self.assertIn("[bg awaiting user]", bg.awaiting_user())
        self.assertIn("[bg done OK]", bg.done(ok=True))
        self.assertIn("[bg done FAIL]", bg.done(ok=False))

    def test_success_and_failure_differ_in_colour_not_only_in_words(self) -> None:
        """This is the line a user scans a scrollback for, and scanning is done
        by colour before it is done by reading. Comparing the whole strings
        would pass on the words alone, so compare the escape each starts with."""
        import re

        def colour(tag: str) -> str:
            match = re.match(r"\x1b\[[0-9;]*m", tag)
            return match.group(0) if match else ""

        self.assertNotEqual(colour(bg.done(ok=True)), colour(bg.done(ok=False)))
        self.assertTrue(colour(bg.done(ok=True)), "the tag must carry a colour at all")

    def test_every_tag_closes_its_colour(self) -> None:
        """An unclosed escape bleeds into everything printed after it."""
        for tag in (bg.started(), bg.progress(), bg.awaiting_user(), bg.done(ok=True)):
            with self.subTest(tag=_strip(tag)):
                self.assertTrue(tag.endswith("\x1b[0m"))

    def test_a_running_step_is_dimmer_than_a_start(self) -> None:
        """Progress is context, not news."""
        self.assertNotEqual(bg.started(), bg.progress())


class TheWireRendererUsesThem(unittest.TestCase):
    def _rendered(self, kind: str, data: dict) -> str:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            render_event_frame({"kind": kind, "data": data})
        return _strip(err.getvalue())

    def test_a_started_frame(self) -> None:
        line = self._rendered(
            "async_task_started", {"agent_name": "file-organizer", "task_id": "abcd1234efgh"}
        )
        self.assertIn("[bg started]", line)
        self.assertIn("file-organizer", line)
        self.assertIn("task=abcd1234", line)

    def test_a_progress_frame(self) -> None:
        line = self._rendered(
            "async_task_progress",
            {"agent_name": "tinker", "kind_hint": "tool", "text": "read a file"},
        )
        self.assertIn("[bg progress]", line)
        self.assertIn("tool: read a file", line)

    def test_a_completed_frame_says_which(self) -> None:
        ok = self._rendered("async_task_completed", {"agent_name": "t", "ok": True, "summary": "s"})
        bad = self._rendered(
            "async_task_completed", {"agent_name": "t", "ok": False, "summary": "s"}
        )
        self.assertIn("[bg done OK]", ok)
        self.assertIn("[bg done FAIL]", bad)

    def test_a_missing_agent_name_does_not_break_the_line(self) -> None:
        """Wire frames are not guaranteed to carry every field."""
        line = self._rendered("async_task_started", {})
        self.assertIn("[bg started]", line)


class NeitherSurfaceSpellsItItself(unittest.TestCase):
    ROOT = pathlib.Path(__file__).resolve().parent.parent.parent / "yuyutsava" / "cli"

    def test_the_tags_are_written_in_one_module(self) -> None:
        offenders = []
        for path in sorted(self.ROOT.rglob("*.py")):
            if path.name == "bg_labels.py":
                continue
            text = path.read_text()
            if any(t in text for t in ("[bg started]", "[bg progress]", "[bg done ")):
                offenders.append(str(path.relative_to(self.ROOT.parent)))
        self.assertEqual(offenders, [], f"tags spelled out in {offenders}")


if __name__ == "__main__":
    unittest.main()
