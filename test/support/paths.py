"""Where the repository is, for tests that read its files.

One definition instead of ``Path(__file__).parents[N]`` in each test: a depth
counted from the test's own location silently points somewhere else the moment
the test moves.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PACKAGE = REPO / "yuyutsava"
