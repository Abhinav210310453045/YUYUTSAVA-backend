"""Which renderer a visual kind reaches, and what a bad request is told.

W4 (OCP): the kinds were written twice — `KINDS` in `types.py`, and a six-branch
`if` chain in `render._load` that lazily imports each renderer. The two had to
agree, and nothing checked that they did: a kind in `KINDS` with no branch
returned `None`, and `render` then failed with "'NoneType' object is not
callable" rather than saying anything useful. Nothing tested this module at all.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from unittest import mock

from yuyutsava.visuals import render as r
from yuyutsava.visuals.render import KINDS
from yuyutsava.visuals.types import VisualError

EXPECTED = {
    "chart": "render_chart",
    "diagram": "render_diagram",
    "table": "render_table",
    "code": "render_code",
    "math": "render_math",
    "timeline": "render_timeline",
}


class EveryKindReachesARenderer(unittest.TestCase):
    def test_the_kinds_are_these(self) -> None:
        self.assertEqual(list(KINDS), list(EXPECTED))

    def test_each_kind_loads_a_callable(self) -> None:
        """The failure this guards: a kind that is advertised but has no
        renderer behind it."""
        for kind, fn_name in EXPECTED.items():
            with self.subTest(kind=kind):
                try:
                    fn = r._load(kind)
                except ImportError:
                    self.skipTest("the visuals extra is not installed")
                self.assertTrue(callable(fn))
                self.assertEqual(fn.__name__, fn_name)

    def test_render_hands_the_spec_to_that_renderer(self) -> None:
        for kind in EXPECTED:
            with self.subTest(kind=kind):
                seen = []
                with mock.patch.object(r, "_load", return_value=lambda spec: seen.append(spec)):
                    r.render(kind, {"k": 1})
                self.assertEqual(seen, [{"k": 1}])

    def test_kind_is_normalised(self) -> None:
        seen = []
        with mock.patch.object(r, "_load", return_value=lambda spec: seen.append(spec)) as load:
            r.render("  CHART ", {})
        load.assert_called_once_with("chart")


class RenderersLoadOnlyWhenUsed(unittest.TestCase):
    def test_building_the_tools_does_not_import_matplotlib(self) -> None:
        """The tools are built at agent start; matplotlib costs seconds and
        is an optional extra. Run in a fresh interpreter, since this one may
        already have it loaded."""
        probe = (
            "import sys, yuyutsava.visuals.tools, yuyutsava.visuals.render;"
            "print('matplotlib' in sys.modules)"
        )
        out = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, check=True, timeout=120
        )
        self.assertEqual(out.stdout.strip(), "False")


class BadRequestsAreToldWhy(unittest.TestCase):
    def test_an_unknown_kind(self) -> None:
        with self.assertRaises(VisualError) as caught:
            r.render("sculpture", {})
        self.assertEqual(
            str(caught.exception),
            "unknown visual kind 'sculpture'; expected one of "
            "['chart', 'diagram', 'table', 'code', 'math', 'timeline']",
        )

    def test_a_spec_that_is_not_an_object(self) -> None:
        with self.assertRaises(VisualError) as caught:
            r.render("chart", ["not", "a", "dict"])
        self.assertEqual(str(caught.exception), "spec must be a dict/object")

    def test_a_missing_extra_is_a_clean_error_not_a_crash(self) -> None:
        with mock.patch.object(r, "_load", side_effect=ImportError("No module named 'matplotlib'")):
            with self.assertRaises(VisualError) as caught:
                r.render("chart", {})
        self.assertIn("'visuals' extra", str(caught.exception))
        self.assertIn("matplotlib", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
