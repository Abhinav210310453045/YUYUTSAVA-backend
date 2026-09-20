"""The viewport arithmetic, without an application under it.

`R-SOL-01`: "which rows are on screen" was ten methods and a mutable offset
inside `ChatDashboard`, so every test of it had to build a full-screen
prompt_toolkit application and then reach past it. The arithmetic depends on
three things — the lines, the pane width, the pane height — and on nothing else,
which is what `ScrollView` takes.
"""

from __future__ import annotations

import unittest

from yuyutsava.cli.render.scroll_view import ScrollView


class _View:
    """A view over a fixed transcript at a fixed geometry."""

    def __init__(self, lines: list[str], *, width: int = 20, height: int = 5) -> None:
        self.lines = lines
        self.width = width
        self.height = height
        self.view = ScrollView(
            lines=lambda: self.lines,
            width=lambda: self.width,
            height=lambda: self.height,
        )


class FollowingTheTail(unittest.TestCase):
    def test_a_new_view_shows_the_end(self) -> None:
        v = _View([f"line {i}" for i in range(20)])
        self.assertEqual(v.view.offset, 0)
        self.assertEqual(v.view.visible()[-1], "line 19")

    def test_the_viewport_is_never_taller_than_the_pane(self) -> None:
        v = _View([f"line {i}" for i in range(100)], height=5)
        self.assertEqual(len(v.view.visible()), 5)

    def test_a_short_transcript_is_shown_whole(self) -> None:
        v = _View(["a", "b"], height=5)
        self.assertEqual(v.view.visible(), ["a", "b"])

    def test_an_empty_transcript_shows_nothing(self) -> None:
        self.assertEqual(_View([]).view.visible(), [])

    def test_follow_returns_to_the_tail_from_anywhere(self) -> None:
        v = _View([f"line {i}" for i in range(50)])
        v.view.scroll_by(20)
        self.assertGreater(v.view.offset, 0)
        v.view.follow()
        self.assertEqual(v.view.offset, 0)
        self.assertEqual(v.view.visible()[-1], "line 49")


class Scrolling(unittest.TestCase):
    def test_scrolling_up_shows_earlier_lines(self) -> None:
        v = _View([f"line {i}" for i in range(50)], height=5)
        v.view.scroll_by(10)
        # The tail showed 45-49; ten rows up is 35-39.
        self.assertEqual(v.view.visible(), [f"line {i}" for i in range(35, 40)])

    def test_it_cannot_scroll_past_the_beginning(self) -> None:
        v = _View([f"line {i}" for i in range(10)], height=5)
        for _ in range(50):
            v.view.page_up()
        self.assertEqual(v.view.offset, 5, "at most content minus one screen")
        self.assertIn("line 0", v.view.visible())

    def test_it_cannot_scroll_past_the_end(self) -> None:
        v = _View([f"line {i}" for i in range(10)])
        v.view.scroll_by(-100)
        self.assertEqual(v.view.offset, 0)

    def test_a_page_keeps_a_row_of_overlap(self) -> None:
        """Paging by the full height would step over a line each time."""
        v = _View([f"line {i}" for i in range(50)], height=10)
        self.assertEqual(v.view.page(), 9)

    def test_a_page_is_never_zero(self) -> None:
        """A one-row pane must still move, or PgUp does nothing at all."""
        v = _View([f"line {i}" for i in range(50)], height=1)
        self.assertEqual(v.view.page(), 1)

    def test_paging_down_comes_back_to_the_tail(self) -> None:
        v = _View([f"line {i}" for i in range(50)], height=5)
        for _ in range(5):
            v.view.page_up()
        for _ in range(20):
            v.view.page_down()
        self.assertEqual(v.view.offset, 0)


class Wrapping(unittest.TestCase):
    def test_a_long_line_becomes_several_rows(self) -> None:
        v = _View(["x" * 45], width=20)
        self.assertEqual(v.view.display_count(20), 3)

    def test_no_row_is_wider_than_the_pane(self) -> None:
        v = _View(["y" * 100, "short"], width=20, height=10)
        for row in v.view.visible():
            self.assertLessEqual(len(row), 20)

    def test_narrowing_re_wraps_what_is_already_there(self) -> None:
        """Wrapping on read rather than on write is why this works."""
        v = _View(["z" * 40], width=40, height=10)
        self.assertEqual(len(v.view.visible()), 1)
        v.width = 10
        self.assertEqual(len(v.view.visible()), 4)

    def test_the_limit_is_measured_in_display_rows(self) -> None:
        """A wrapped line occupies several rows, so the scroll limit has to
        count rows and not lines, or the top becomes unreachable."""
        v = _View(["w" * 100] * 3, width=10, height=5)
        for _ in range(50):
            v.view.page_up()
        self.assertEqual(v.view.offset, v.view.display_count(10) - 5)

    def test_only_what_is_needed_is_wrapped(self) -> None:
        """Walking backwards keeps a 5,000-line buffer cheap at several frames
        a second; asking for three rows must not wrap the whole transcript."""
        v = _View([f"line {i}" for i in range(5000)], height=3)
        rows = v.view.display_rows(3, 20)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[-1], "line 4999")


if __name__ == "__main__":
    unittest.main()
