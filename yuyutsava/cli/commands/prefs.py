"""``yuyutsava prefs`` subcommand — read/write user-pref rows in state.db.

Procedural by design: argparse dispatches `prefs` to ``run_prefs(argv)`` which
returns a process exit code. No class, no global state.
"""

from __future__ import annotations

import asyncio
import json
import sys

from yuyutsava.aio import run as aio_run
from yuyutsava.storage.events import Store
from yuyutsava.storage.prefs import PrefsStore


def run_prefs(argv: list[str]) -> int:
    """``yuyutsava prefs {set|get|list|delete}`` subcommand."""
    if not argv:
        print(
            "Usage: yuyutsava prefs {set <key> <json> | get <key> | delete <key> | list}",
            file=sys.stderr,
        )
        return 2

    sub = argv[0]

    async def _run() -> int:
        store = Store()
        await store.start()
        prefs = PrefsStore(store)
        try:
            if sub == "list":
                all_prefs = await prefs.all()
                if not all_prefs:
                    print("(no preferences set)")
                else:
                    for key, val in sorted(all_prefs.items()):
                        print(f"{key} = {json.dumps(val)}")
                return 0

            if sub == "get":
                if len(argv) < 2:
                    print("Usage: yuyutsava prefs get <key>", file=sys.stderr)
                    return 2
                val = await prefs.get(argv[1])
                if val is None:
                    print(f"(not set: {argv[1]})")
                else:
                    print(json.dumps(val))
                return 0

            if sub == "set":
                if len(argv) < 3:
                    print("Usage: yuyutsava prefs set <key> <json_value>", file=sys.stderr)
                    return 2
                key = argv[1]
                try:
                    value = json.loads(argv[2])
                except json.JSONDecodeError as exc:
                    print(f"Error: invalid JSON value: {exc}", file=sys.stderr)
                    return 1
                await prefs.set(key, value)
                # Drain the write queue before closing.
                await asyncio.sleep(0.05)
                print(f"Set {key} = {json.dumps(value)}")
                return 0

            if sub == "delete":
                if len(argv) < 2:
                    print("Usage: yuyutsava prefs delete <key>", file=sys.stderr)
                    return 2
                await prefs.delete(argv[1])
                await asyncio.sleep(0.05)
                print(f"Deleted {argv[1]}")
                return 0

            print(f"Unknown prefs subcommand: {sub!r}", file=sys.stderr)
            return 2
        finally:
            await store.stop()

    return aio_run(_run())
