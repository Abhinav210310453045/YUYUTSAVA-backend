import asyncio
import sys
sys.path.insert(0, "$REPO")

from dotenv import load_dotenv
load_dotenv("$REPO/.env")

from yuyutsava.core import build_agent, astream_agent, llm_settings_from_env
from pathlib import Path

async def main():
    settings = llm_settings_from_env()
    workspace = Path("$REPO")
    bundle = build_agent(workspace, settings)

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
