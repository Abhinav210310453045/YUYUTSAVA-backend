"""The terminal palette is defined once.

`R-DRY-04`: nine escape codes were written out 157 times across ten modules,
and only one of them named any. A literal like `"\033[33m"` is unreviewable —
nothing tells a deliberate yellow from a typo'd one — and changing the scheme
meant finding every copy.

These pin the codes (so a rename cannot silently recolour the terminal) and the
rule that no other module writes one.
"""

from __future__ import annotations

import ast
import pathlib
import re
import unittest

from yuyutsava import ansi

_ROOT = pathlib.Path(__file__).resolve().parents[1] / "yuyutsava"
#: Any CSI sequence, not only the ones that end in "m". A pattern that
#: matched colour alone let `\\033[2J\\033[H` sit unnamed in the REPL.
_ESCAPE = re.compile(r"\\033\[[0-9;?]*[A-Za-z]")

#: The exact bytes each name must keep. Written out here rather than imported
#: so a change to the module is a change to two places on purpose.
CODES = {
    "RESET": "\033[0m",
    "DIM": "\033[2m",
    "BOLD": "\033[1m",
    "RED": "\033[31m",
    "GREEN": "\033[32m",
    "YELLOW": "\033[33m",
    "MAGENTA": "\033[35m",
    "CYAN": "\033[36m",
    "BOLD_RED": "\033[1;31m",
    # Movement, not colour: `/clear` erases the screen and homes the cursor.
    "CLEAR_SCREEN": "\033[2J\033[H",
}

#: The semantic layer: one hue per *kind* of thing in the chat transcript.
#: Their bytes depend on the terminal (24-bit or 256-colour) and on the ramp,
#: so what is pinned here is the vocabulary, not the codes.
ROLE_NAMES = {
    "YOU",
    "AGENT",
    "TOOL",
    "OK",
    "ERR",
    "WARN",
    "SUBAGENT",
    "CHROME",
    # The opening screen's own two, which the transcript never draws with.
    "KEY",
    "VALUE",
}

HELPERS = {"ROLES", "palette", "resolve_theme", "sgr", "truecolor"}


class ThePalette(unittest.TestCase):
    def test_every_name_keeps_its_code(self) -> None:
        for name, code in CODES.items():
            with self.subTest(name=name):
                self.assertEqual(getattr(ansi, name), code)

    def test_the_module_exports_exactly_these(self) -> None:
        self.assertEqual(set(ansi.__all__), set(CODES) | ROLE_NAMES | HELPERS)

    def test_every_role_is_a_colour_and_nothing_else(self) -> None:
        """A role that resolved to "" would silently draw in the last colour
        set, which is how a transcript ends up one undifferentiated hue."""
        for name in ROLE_NAMES:
            with self.subTest(role=name):
                code = getattr(ansi, name)
                self.assertTrue(code.startswith("\033[38;"), f"{name} is not a foreground colour")

    def test_the_roles_and_the_ramps_name_the_same_things(self) -> None:
        self.assertEqual({r.upper() for r in ansi.ROLES}, ROLE_NAMES)
        for theme in ("dark", "light"):
            with self.subTest(theme=theme):
                self.assertEqual(set(ansi.palette(theme)), set(ansi.ROLES))

    def test_reset_is_not_a_colour(self) -> None:
        """Used to close every sequence, so it must not carry one."""
        self.assertEqual(ansi.RESET, "\033[0m")


class NobodyElseWritesAnEscape(unittest.TestCase):
    def test_no_module_contains_a_raw_ansi_escape(self) -> None:
        offenders = []
        for path in sorted(_ROOT.rglob("*.py")):
            if path.name == "ansi.py":
                continue
            for i, line in enumerate(path.read_text().splitlines(), 1):
                if _ESCAPE.search(line):
                    offenders.append(f"{path.relative_to(_ROOT.parent)}:{i}")
        self.assertEqual(
            offenders,
            [],
            "raw escapes found — import the name from yuyutsava.ansi instead:\n  "
            + "\n  ".join(offenders),
        )

    def test_the_palette_is_a_leaf(self) -> None:
        """It is imported by both `cli` and `daemon`, so it may import neither.

        That is why it sits beside `yuyutsava.text` rather than under
        `cli/render`: a palette under `cli` would make `daemon` import `cli`.
        """
        tree = ast.parse((_ROOT / "ansi.py").read_text())
        imported = [
            node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        ] + [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        ]
        self.assertEqual(
            [m for m in imported if m.startswith("yuyutsava")],
            [],
            "the palette must not import from the package it colours",
        )


if __name__ == "__main__":
    unittest.main()
