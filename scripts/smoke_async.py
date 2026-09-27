"""MANUAL SMOKE SCRIPT — MAKES REAL, BILLABLE LLM CALLS.

Builds a real agent and runs two concurrent turns against whatever
LLM_PROVIDER is configured, so every execution costs money. It proves
``astream_agent`` is genuinely async: two turns interleave on one loop.

It refuses to run unless asked to:

    YUYUTSAVA_ALLOW_BILLABLE=1 .venv/bin/python scripts/smoke_async.py

It lived at ``test/test_async.py`` until W6, where a bulk
``for f in test/*.py`` sweep billed it by accident on 2026-08-08, and where its
module-level refusal later aborted pytest's collection of the whole suite. Out
of the test tree, no runner collects it; the guard stays for anyone who runs it.
Do not remove it.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from yuyutsava.cli.render.terminal_stream import astream_agent
from yuyutsava.core.engine import build_cli_deepagent
from yuyutsava.llm.settings import llm_settings_from_env

REPO_ROOT = Path(__file__).resolve().parents[1]
REFUSAL = (
    "smoke_async.py makes REAL billable LLM calls and did not run.\n"
    "Re-run with YUYUTSAVA_ALLOW_BILLABLE=1 if that is intended."
)


async def main() -> None:
    load_dotenv(REPO_ROOT / ".env")
    bundle = build_cli_deepagent(REPO_ROOT, llm_settings_from_env())

    # Two tasks at once — proves astream_agent is truly async.
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


if __name__ == "__main__":
    if os.environ.get("YUYUTSAVA_ALLOW_BILLABLE") != "1":
        sys.exit(REFUSAL)
    asyncio.run(main())
