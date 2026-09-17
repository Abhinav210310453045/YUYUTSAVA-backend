"""MANUAL SMOKE SCRIPT — MAKES REAL, BILLABLE LLM CALLS.

⚠️ This is NOT a unit test despite living in test/ and being named test_*.py.
It builds a real agent and runs two concurrent turns against whatever
LLM_PROVIDER is configured (currently Vertex), so every execution costs money.

It has no guard, so any bulk `for f in test/*.py; do python "$f"; done` sweep
runs it — which is exactly how it got executed unintentionally on 2026-08-08.

To run it deliberately:

    YUYUTSAVA_ALLOW_BILLABLE=1 .venv/bin/python test/test_async.py

Consider renaming to scripts/smoke_async.py so it stops looking like a test.
"""

import os as _os
import sys as _sys

_ALLOWED = _os.environ.get("YUYUTSAVA_ALLOW_BILLABLE") == "1"
_REFUSAL = (
    "test_async.py makes REAL billable LLM calls and was skipped.\n"
    "Re-run with YUYUTSAVA_ALLOW_BILLABLE=1 if that is intended."
)

if not _ALLOWED:
    # A module-level `raise SystemExit` here aborted pytest's *collection* of
    # the whole suite, not just this file: pytest imports every test module up
    # front, so one SystemExit stops all 1,587 tests. Skip when pytest is the
    # importer; keep exiting when run directly, which is the guard that matters
    # for a `for f in test/*.py` sweep — how this got billed on 2026-08-08.
    # The test is whether pytest is *importing* us, not whether it happens to be
    # installed: `import pytest` succeeds either way now, and calling skip()
    # outside a pytest run raises `Skipped` — a traceback with exit status 0,
    # which a bulk sweep would read as success.
    _pytest = _sys.modules.get("pytest")
    if _pytest is None:
        raise SystemExit(_REFUSAL)
    _pytest.skip(_REFUSAL, allow_module_level=True)

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv

load_dotenv(REPO_ROOT / ".env")

from yuyutsava.cli.render.terminal_stream import astream_agent
from yuyutsava.core.engine import build_cli_deepagent
from yuyutsava.llm.settings import llm_settings_from_env


async def main():
    settings = llm_settings_from_env()
    workspace = REPO_ROOT
    bundle = build_cli_deepagent(workspace, settings)

    # Run two tasks concurrently — proves astream_agent is truly async
    results = await asyncio.gather(
        astream_agent(
            bundle.agent, "Explain me the Value of Good Educatoin in real life in 200 words."
        ),
        astream_agent(bundle.agent, "exmplain me IC engine in 200 words."),
    )
    print("\n=== Results ===")
    for i, r in enumerate(results):
        print(f"Task {i + 1}: {r}")

    bundle.close()


asyncio.run(main())
