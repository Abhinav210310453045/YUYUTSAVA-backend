"""Tests import public names only (R-TST-04).

A test that imports ``_something`` pins an implementation detail, so a
behaviour-preserving refactor breaks it. When a helper is a genuine unit worth
testing on its own, the fix is to make it public in its module, not to reach
past the underscore. W6 did that for 21 names; this keeps it that way.

Run:  .venv/bin/python -m pytest test/architecture/test_tests_use_public_names.py
"""

from __future__ import annotations

import ast
import unittest

from test.support.paths import REPO

#: Private names a test may import, each with the reason it stays private.
ALLOWED = {
    # A subprocess entry module: the leading underscore marks "run me as a
    # child process, never import me in the daemon" — the same convention as
    # the face watcher's _webcam_proc, which is left exactly as it is. The
    # test drives its listen loop directly because the process boundary is
    # what it is testing around.
    "yuyutsava.events.sources._voice_proc",
}


def private_imports() -> dict[str, list[str]]:
    """``{"module.name": ["test/…py:line", …]}`` for every private yuyutsava import."""
    found: dict[str, list[str]] = {}
    for path in sorted((REPO / "test").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.ImportFrom) and node.module):
                continue
            if not node.module.startswith("yuyutsava"):
                continue
            for alias in node.names:
                name = alias.name
                if name.startswith("_") and not name.startswith("__"):
                    where = f"{path.relative_to(REPO).as_posix()}:{node.lineno}"
                    found.setdefault(f"{node.module}.{name}", []).append(where)
    return found


class TestsUsePublicNames(unittest.TestCase):
    def test_no_test_imports_a_private_name(self) -> None:
        offenders = {k: v for k, v in private_imports().items() if k not in ALLOWED}
        self.assertEqual(
            offenders,
            {},
            "tests import private names — test the public API, or make a genuine "
            "unit public in its module:\n"
            + "\n".join(f"  {k}  <- {', '.join(v)}" for k, v in sorted(offenders.items())),
        )

    def test_every_allowance_is_still_used(self) -> None:
        """An allowance whose import is gone is a hole waiting to be reused."""
        stale = ALLOWED - set(private_imports())
        self.assertEqual(stale, set(), f"remove these from ALLOWED: {sorted(stale)}")


if __name__ == "__main__":
    unittest.main()
