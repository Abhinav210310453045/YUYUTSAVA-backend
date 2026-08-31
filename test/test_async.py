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

if _os.environ.get("YUYUTSAVA_ALLOW_BILLABLE") != "1":
    raise SystemExit(
        "test_async.py makes REAL billable LLM calls and was skipped.\n"
        "Re-run with YUYUTSAVA_ALLOW_BILLABLE=1 if that is intended."
    )

import asyncio
import sys
sys.path.insert(0, "$REPO")

from dotenv import load_dotenv
load_dotenv("$REPO/.env")

from yuyutsava.core import build_cli_deepagent, astream_agent, llm_settings_from_env
from pathlib import Path

async def main():
    settings = llm_settings_from_env()
    workspace = Path("$REPO")
    bundle = build_cli_deepagent(workspace, settings)

    # Run two tasks concurrently — proves astream_agent is truly async
    results = await asyncio.gather(
        astream_agent(bundle.agent, "Explain me the Value of Good Educatoin in real life in 200 words."),
        astream_agent(bundle.agent, "exmplain me IC engine in 200 words."),
    )
    print("\n=== Results ===")
    for i, r in enumerate(results):
        print(f"Task {i+1}: {r}")

    bundle.close()

asyncio.run(main())
