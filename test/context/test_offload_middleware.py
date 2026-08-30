"""Unit tests for ToolResultOffloadPolicy — the load-bearing offload path.

Verifies the contract from the master plan: a tool returning 150k chars
leaves a <3k-char digest in graph state, the full body is retrievable via
the artifact store, and excluded/small/non-ToolMessage results pass through
byte-identical.

Phase 4 renamed the subject: the offload logic is now a plain
``Policy`` behind ``LangChainPolicyAdapter`` instead of an ``AgentMiddleware``
subclass. These cases are unchanged and still assert the same contract — driving
the adapter is what keeps them exercising the production wiring rather than the
policy in isolation (``test/policy/test_offload_parity.py`` does that).

Run:  uv run python -m unittest test.context.test_offload_middleware -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage

from yuyutsava.context.artifacts_unified import (
    ArtifactSchema, UnifiedArtifactStore, sqlite_artifact_store,
)
from yuyutsava.storage.dialect import SqliteDialect
from yuyutsava.context.config import ContextSettings
from yuyutsava.context.offload_policy import ToolResultOffloadPolicy
from yuyutsava.policy.adapter import LangChainPolicyAdapter


def _request(tool_name: str) -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": tool_name, "args": {}, "id": "tc-1"},
        tool=None,
        state={},
        runtime=None,
    )


def _handler_returning(message):
    async def handler(_request):
        return message

    return handler


class OffloadMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = sqlite_artifact_store(Path(self._tmp.name) / "state.db")
        self.settings = ContextSettings(offload_threshold_chars=20_000)
        self.mw = LangChainPolicyAdapter(
            [ToolResultOffloadPolicy(self.store, self.settings)])

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_big_result_offloaded_and_retrievable(self) -> None:
        content = "HEAD-" + ("z" * 150_000) + "-TAIL"
        original = ToolMessage(content=content, tool_call_id="tc-1", name="ws_search")

        result = await self.mw.awrap_tool_call(
            _request("ws_search"), _handler_returning(original)
        )

        self.assertIsInstance(result, ToolMessage)
        self.assertLess(len(result.content), 3_000)
        digest = json.loads(result.content)
        self.assertTrue(digest["offloaded"])
        self.assertEqual(digest["size_chars"], len(content))
        self.assertTrue(digest["head"].startswith("HEAD-"))
        self.assertTrue(digest["tail"].endswith("-TAIL"))
        self.assertIn("ctx_fetch_artifact", digest["hint"])
        # ToolMessage identity fields survive the swap.
        self.assertEqual(result.tool_call_id, "tc-1")
        self.assertEqual(result.name, "ws_search")

        sl = await self.store.get(digest["artifact_id"], offset=0, length=-1)
        self.assertEqual(sl.content, content)

    async def test_small_result_passthrough(self) -> None:
        """A small result from a size-gated tool is returned untouched.

        Uses a plain tool name, NOT ``ws_search``. ``ws_`` is in
        ``ContextSettings.always_offload_prefixes`` (default ``("ws_",)``), so a
        ws_* result is offloaded regardless of size — by design, to stop
        small-but-accumulating search results from piling up in context.
        This test asserted passthrough for ``ws_search`` and so had been failing
        ever since that feature landed, leaving the real size-gated passthrough
        path with no coverage at all.
        """
        original = ToolMessage(content="tiny", tool_call_id="tc-1", name="db_query")
        result = await self.mw.awrap_tool_call(
            _request("db_query"), _handler_returning(original)
        )
        self.assertIs(result, original)

    async def test_small_always_offload_prefix_is_offloaded(self) -> None:
        """The flip side: a *small* ws_* result IS offloaded, size notwithstanding.

        Pins the ``always_offload_prefixes`` behaviour that the stale assertion
        above was accidentally contradicting.
        """
        original = ToolMessage(content="tiny", tool_call_id="tc-1", name="ws_search")
        result = await self.mw.awrap_tool_call(
            _request("ws_search"), _handler_returning(original)
        )
        self.assertIsNot(result, original)
        digest = json.loads(result.content)
        self.assertTrue(digest["offloaded"])
        self.assertEqual(digest["size_chars"], len("tiny"))

    async def test_excluded_tool_passthrough(self) -> None:
        big = "y" * 50_000
        original = ToolMessage(content=big, tool_call_id="tc-1", name="ctx_fetch_artifact")
        result = await self.mw.awrap_tool_call(
            _request("ctx_fetch_artifact"), _handler_returning(original)
        )
        self.assertIs(result, original)

    async def test_non_tool_message_passthrough(self) -> None:
        sentinel = object()
        result = await self.mw.awrap_tool_call(
            _request("ws_search"), _handler_returning(sentinel)
        )
        self.assertIs(result, sentinel)

    async def test_store_failure_passes_original_through(self) -> None:
        # Subclass the store CLASS, not the factory function: since ADR-002
        # step 2.5b the store is constructed from a dialect, so the schema owner
        # and the store are separate objects.
        class _BrokenStore(UnifiedArtifactStore):
            async def put(self, *a, **k):  # noqa: ANN002, ANN003
                raise RuntimeError("db down")

        mw = LangChainPolicyAdapter([ToolResultOffloadPolicy(
            _BrokenStore(
                SqliteDialect(ArtifactSchema(Path(self._tmp.name) / "x.db"))
            ),
            self.settings,
        )])
        original = ToolMessage(content="q" * 50_000, tool_call_id="tc-1", name="ws_search")
        result = await mw.awrap_tool_call(
            _request("ws_search"), _handler_returning(original)
        )
        self.assertIs(result, original)


if __name__ == "__main__":
    unittest.main()
