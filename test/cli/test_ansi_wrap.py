"""ANSI-aware wrapping: the invariant that stops the pane bleeding.

One long line — a `warnings.warn`, a log record, a base64 blob — painted
straight across the chat dashboard's context column, because the transcript
was stored as written and drawn without wrapping. These pin the properties the
pane depends on:

* a wrapped row is never wider than the pane, measured in **cells** not
  codepoints (escape sequences are free, CJK costs two, combining marks zero);
* colour survives the break, and is closed at it so nothing leaks into the
  column beside it;
* a line that already fits is returned untouched — no gratuitous rewriting of
  Rich's own careful output.

Run:  .venv/bin/python test/cli/test_ansi_wrap.py
"""

from __future__ import annotations

import unittest

from yuyutsava.cli.render.ansi_wrap import (
    char_width,
    strip_ansi,
    visible_width,
    wrap_ansi,
    wrap_lines,
)

CYAN = "\x1b[36m"
BOLD = "\x1b[1m"
RESET = "\x1b[0m"


class Measuring(unittest.TestCase):
    def test_escape_sequences_are_free(self):
        self.assertEqual(visible_width(f"{CYAN}abc{RESET}"), 3)

    def test_wide_characters_cost_two_cells(self):
        self.assertEqual(visible_width("日本"), 4)
        self.assertEqual(char_width("日"), 2)

    def test_combining_marks_cost_nothing(self):
        self.assertEqual(visible_width("é"), 1)

    def test_strip_removes_only_escapes(self):
        self.assertEqual(strip_ansi(f"{CYAN}a{RESET}b"), "ab")

    def test_osc_sequences_are_stripped(self):
        # Terminal title / hyperlink sequences also have no width.
        self.assertEqual(visible_width("\x1b]0;title\x07abc"), 3)


class Wrapping(unittest.TestCase):
    def test_no_row_exceeds_the_width(self):
        for width in (1, 2, 7, 34, 80):
            for line in ("x" * 500, f"{CYAN}{'y' * 500}{RESET}", "日" * 200,
                         "a b c " * 90):
                for row in wrap_ansi(line, width):
                    self.assertLessEqual(
                        visible_width(row), width, f"width={width} line={line[:20]}")

    def test_a_line_that_fits_is_untouched(self):
        line = f"{CYAN}already wrapped by rich{RESET}"
        self.assertEqual(wrap_ansi(line, 80), [line])

    def test_colour_is_reopened_on_the_continuation(self):
        rows = wrap_ansi(f"{CYAN}abcdefgh{RESET}", 4)
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[1].startswith(CYAN), rows[1])

    def test_every_wrapped_row_is_reset_so_colour_cannot_leak(self):
        # A row that ends mid-colour would tint the panel beside it.
        for row in wrap_ansi(f"{CYAN}{'x' * 40}{RESET}", 10):
            self.assertTrue(row.endswith(RESET), row)

    def test_nested_attributes_both_survive(self):
        rows = wrap_ansi(f"{CYAN}{BOLD}abcdefgh{RESET}", 4)
        self.assertIn(CYAN, rows[1])
        self.assertIn(BOLD, rows[1])

    def test_no_text_is_lost(self):
        line = f"{CYAN}{'abcdefghij' * 20}{RESET}"
        joined = "".join(strip_ansi(r) for r in wrap_ansi(line, 13))
        self.assertEqual(joined, strip_ansi(line))

    def test_an_empty_line_stays_one_blank_row(self):
        self.assertEqual(wrap_ansi("", 10), [""])

    def test_a_nonpositive_width_returns_the_line_rather_than_looping(self):
        self.assertEqual(wrap_ansi("abc", 0), ["abc"])
        self.assertEqual(wrap_ansi("abc", -5), ["abc"])

    def test_a_wide_character_is_not_split_across_rows(self):
        # Half a glyph in each row would corrupt the terminal.
        for row in wrap_ansi("日" * 10, 5):
            self.assertEqual(visible_width(row) % 2, 0, row)

    def test_a_glyph_wider_than_the_pane_becomes_an_ellipsis(self):
        # The no-overflow guarantee has to hold at every width, including one
        # too narrow for the character at all.
        rows = wrap_ansi("日本", 1)
        for row in rows:
            self.assertLessEqual(visible_width(row), 1, row)
        self.assertEqual("".join(strip_ansi(r) for r in rows), "……")

    def test_wrap_lines_flattens(self):
        self.assertEqual(wrap_lines(["ab", "cdef"], 2), ["ab", "cd", "ef"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
