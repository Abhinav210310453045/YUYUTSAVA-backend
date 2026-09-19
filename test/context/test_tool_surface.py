"""Characterization: the `ctx_*` tools, per wiring.

`R-RDB-02`: `make_context_tools` is 242 lines and the set it returns depends on
what is wired behind it — four distinct surfaces, not one. A tool that stops
being offered is how a compacted conversation quietly becomes unrecoverable, so
the split has to preserve all four.

Order is pinned as well as membership: the agent fingerprint sorts tool names
before hashing, so a reordering would pass every other check in the suite.
"""

from __future__ import annotations

import unittest

from yuyutsava.context.tools import make_context_tools

#: Always present: reading back what compaction offloaded, and compacting.
BASE = ["ctx_fetch_artifact", "ctx_grep_artifact", "ctx_compact"]

#: Offered once a transcript store is wired — the read-back path for turns
#: compaction has evicted. Without these a compacted conversation cannot reach
#: its own history.
HISTORY = ["ctx_history", "ctx_history_message", "ctx_history_grep"]

#: Only meaningful when the store maintains a semantic index (Postgres plus an
#: embedder). On SQLite it is not offered at all, so the surface degrades
#: cleanly rather than exposing a tool that always fails.
RECALL = ["ctx_recall"]


class _Store:
    def __init__(self, *, recall: bool) -> None:
        self.supports_recall = recall


class TheContextToolSurface(unittest.TestCase):
    @staticmethod
    def _names(*, recall: bool, transcript: bool) -> list[str]:
        return [
            t.name
            for t in make_context_tools(_Store(recall=recall), object() if transcript else None)
        ]

    def test_the_bare_surface_can_still_read_and_compact(self) -> None:
        self.assertEqual(self._names(recall=False, transcript=False), BASE)

    def test_a_transcript_store_adds_the_history_tools(self) -> None:
        self.assertEqual(self._names(recall=False, transcript=True), [*BASE, *HISTORY])

    def test_semantic_recall_is_offered_only_when_supported(self) -> None:
        self.assertEqual(self._names(recall=True, transcript=False), [*BASE, *RECALL])

    def test_everything_wired_offers_everything(self) -> None:
        self.assertEqual(self._names(recall=True, transcript=True), [*BASE, *HISTORY, *RECALL])

    def test_the_base_surface_is_a_prefix_of_every_other(self) -> None:
        """The wirings add tools; none of them takes one away."""
        base = self._names(recall=False, transcript=False)
        for recall in (False, True):
            for transcript in (False, True):
                with self.subTest(recall=recall, transcript=transcript):
                    self.assertEqual(
                        self._names(recall=recall, transcript=transcript)[: len(base)],
                        base,
                    )

    def test_every_tool_describes_itself(self) -> None:
        for tool in make_context_tools(_Store(recall=True), object()):
            with self.subTest(tool=tool.name):
                self.assertTrue((tool.description or "").strip())


if __name__ == "__main__":
    unittest.main()
