"""``ToolResultOffloadPolicy`` rewrites results exactly as the middleware did.

Phase 4 step 4.4, third migration — and the first to use ``after_tool``.

Offload is the root-cause fix for context rot: the digest it writes is what
enters graph state and the checkpoint, so a divergence here is not cosmetic. It
would either put a 150k-char blob back into every later model call, or replace a
result the model needed with a digest it cannot use.

The matrix is the same one ``test/context/test_offload_middleware.py`` covers,
run through **both** implementations against a real SQLite artifact store, with
the stored artifact compared as well as the returned message — a policy that
returned an identical-looking digest pointing at a different artifact would
otherwise pass.


## The golden record

The middleware was deleted at cutover, so it can no longer be run side by side.
Deleting it would have destroyed the evidence too, so its behaviour was
**captured first** into ``tool_policies_golden.json``. That file is the old
implementation's testimony; regenerating it is not a way to fix a failure, since
the code it came from no longer exists. A mismatch means the policy changed.

Run:  .venv/bin/python test/policy/test_offload_parity.py
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from pathlib import Path
from typing import Any

from langchain_core.messages import ToolMessage

from yuyutsava.context.artifacts_unified import sqlite_artifact_store
from yuyutsava.context.config import ContextSettings
from yuyutsava.context.offload_policy import ToolResultOffloadPolicy
from yuyutsava.policy.adapter import LangChainPolicyAdapter

THRESHOLD = 20_000

GOLDEN = json.loads(
    (pathlib.Path(__file__).resolve().parent / "tool_policies_golden.json")
    .read_text(encoding="utf-8"))["offload"]


class _Request:
    def __init__(self, tool_name: str) -> None:
        self.tool_call = {"name": tool_name, "args": {}, "id": "tc-1"}
        self.tool = None
        self.state: dict = {}
        self.runtime = None


def _handler_returning(message: Any):
    async def handler(_request: Any) -> Any:
        return message

    return handler


#: (label, tool name, content, expect_offloaded)
CASES: list[tuple[str, str, Any, bool]] = [
    ("oversized",            "ws_search", "HEAD-" + ("z" * 150_000) + "-TAIL", True),
    ("just under threshold", "tr_grep",   "x" * (THRESHOLD - 1),               False),
    ("exactly at threshold", "tr_grep",   "x" * THRESHOLD,                     False),
    ("one over threshold",   "tr_grep",   "x" * (THRESHOLD + 1),               True),
    ("small ws_ prefix",     "ws_exa",    "tiny",                              True),
    ("excluded tool, huge",  "ctx_recall", "y" * 150_000,                      False),
    ("excluded write_todos", "write_todos", "y" * 150_000,                     False),
    ("empty content",        "tr_grep",   "",                                  False),
]


def _shape(result: Any) -> dict[str, Any]:
    """Everything about the returned message that the graph will keep."""
    if not isinstance(result, ToolMessage):
        return {"kind": type(result).__name__, "value": repr(result)}
    content = result.content
    body: Any = content
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict) and parsed.get("offloaded"):
                # artifact_id is minted per put, so it cannot be compared
                # directly — its presence and the rest of the digest can.
                body = {**parsed, "artifact_id": "<minted>"}
        except (ValueError, TypeError):
            pass
    return {
        "kind": "ToolMessage",
        "content": body,
        "tool_call_id": result.tool_call_id,
        "name": result.name,
        "status": result.status,
    }


class Parity(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.settings = ContextSettings(offload_threshold_chars=THRESHOLD)

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _store(self, tag: str):
        return sqlite_artifact_store(Path(self._tmp.name) / f"{tag}.db")

    async def _new(self, tool: str, content: Any) -> Any:
        adapter = LangChainPolicyAdapter(
            [ToolResultOffloadPolicy(self._store("new"), self.settings)])
        message = ToolMessage(content=content, tool_call_id="tc-1", name=tool)
        return await adapter.awrap_tool_call(
            _Request(tool), _handler_returning(message))

    async def test_every_case_matches_the_middleware(self) -> None:
        for label, tool, content, _ in CASES:
            with self.subTest(case=label):
                self.assertEqual(
                    _shape(await self._new(tool, content)), GOLDEN[label],
                    f"{label}: the message entering graph state differs from "
                    f"what the middleware produced",
                )

    def test_every_case_has_a_recorded_outcome(self) -> None:
        """Negative control — a case the record does not cover asserts nothing."""
        self.assertEqual([l for l, _, _, _ in CASES if l not in GOLDEN], [])

    async def test_the_matrix_actually_offloads_and_passes_through(self) -> None:
        """Negative control — a matrix that only does one is vacuous."""
        expected = {flag for _, _, _, flag in CASES}
        self.assertEqual(expected, {True, False})
        for label, tool, content, should_offload in CASES:
            with self.subTest(case=label):
                shape = _shape(await self._new(tool, content))
                offloaded = (isinstance(shape.get("content"), dict)
                             and shape["content"].get("offloaded") is True)
                self.assertEqual(
                    offloaded, should_offload,
                    f"{label}: expected offloaded={should_offload}",
                )

    async def test_the_full_body_is_actually_stored(self) -> None:
        """A digest pointing at nothing would look identical in the message."""
        content = "HEAD-" + ("z" * 150_000) + "-TAIL"
        store = self._store("stored")
        adapter = LangChainPolicyAdapter(
            [ToolResultOffloadPolicy(store, self.settings)])
        message = ToolMessage(content=content, tool_call_id="tc-1", name="ws_search")
        result = await adapter.awrap_tool_call(
            _Request("ws_search"), _handler_returning(message))
        digest = json.loads(result.content)
        # length=-1: `get` slices by default, so the default read would compare
        # a truncated prefix and pass for the wrong reason.
        stored = await store.get(digest["artifact_id"], offset=0, length=-1)
        self.assertEqual(
            stored.content, content,
            "the artifact the digest points at does not hold the original body",
        )
        self.assertEqual(digest["size_chars"], len(content))

    async def test_a_non_tool_message_passes_through_untouched(self) -> None:
        sentinel = object()
        adapter = LangChainPolicyAdapter(
            [ToolResultOffloadPolicy(self._store("passthru"), self.settings)])
        result = await adapter.awrap_tool_call(
            _Request("ws_search"), _handler_returning(sentinel))
        self.assertIs(result, sentinel)

    async def test_storage_failure_never_fails_the_turn(self) -> None:
        """A broken artifact store must degrade, not raise."""
        class _Broken:
            supports_recall = False

            async def put(self, *_a: Any, **_k: Any) -> str:
                raise RuntimeError("disk on fire")

        content = "z" * 150_000
        adapter = LangChainPolicyAdapter(
            [ToolResultOffloadPolicy(_Broken(), self.settings)])  # type: ignore[arg-type]
        message = ToolMessage(content=content, tool_call_id="tc-1", name="ws_search")
        with self.assertLogs("yuyutsava.context.offload", level="ERROR"):
            result = await adapter.awrap_tool_call(
                _Request("ws_search"), _handler_returning(message))
        self.assertEqual(
            result.content, content,
            "the original result was lost when the store failed",
        )


class TheWholePoint(unittest.IsolatedAsyncioTestCase):
    """The size decision, with no store, no adapter and no framework result."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.policy = ToolResultOffloadPolicy(
            sqlite_artifact_store(Path(self._tmp.name) / "s.db"),
            ContextSettings(offload_threshold_chars=THRESHOLD),
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_over_the_threshold_offloads(self) -> None:
        self.assertTrue(self.policy._should_offload("tr_grep", "x" * (THRESHOLD + 1)))

    def test_exactly_at_the_threshold_does_not(self) -> None:
        """``>`` not ``>=`` — pinned because either reading looks plausible."""
        self.assertFalse(self.policy._should_offload("tr_grep", "x" * THRESHOLD))

    def test_an_always_offload_prefix_ignores_size(self) -> None:
        self.assertTrue(self.policy._should_offload("ws_tavily", "tiny"))

    def test_a_prefix_match_must_be_a_prefix(self) -> None:
        self.assertFalse(self.policy._should_offload("my_ws_thing", "tiny"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
