"""``yuyutsava attach`` — observe a running daemon from the terminal.

Subscribes to the daemon's SSE stream, prints events/timeline lines to stderr,
and prompts for any Tier-2 ask (incl. async-subagent HITL prompts tagged
``#bg``). Ctrl-C cleanly detaches.

This is intentionally minimal — it's a tail-and-respond helper, not a chat
interface. Submitting tasks remains the Electron renderer's job; ``attach``
just lets users observe and answer prompts from a terminal.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from yuyutsava.cli.remote_attach import (
    CliAttachClient,
    prompt_user_for_ask,
    render_event_frame,
)

logger = logging.getLogger("yuyutsava.cli.commands.attach")


def _default_daemon_url() -> str:
    """Resolve the daemon URL: discovery file → env → built-in default.

    Reading the discovery file first means a running daemon advertises its
    actual URL (which may differ from the default if env-overridden), so
    ``yuyutsava attach`` "just works" without the user having to set
    ``YUYUTSAVA_DAEMON_URL`` manually.
    """
    env = os.environ.get("YUYUTSAVA_DAEMON_URL")
    if env:
        return env
    try:
        from yuyutsava.daemon.singleton import read_daemon_discovery
        disco = read_daemon_discovery()
        if disco and isinstance(disco.get("web_url"), str):
            url = str(disco["web_url"]).rstrip("/")
            return url
    except Exception:  # noqa: BLE001
        pass
    return "http://127.0.0.1:7654"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="yuyutsava attach",
        description="Attach to a running YUYUTSAVA daemon and surface its prompts in this terminal.",
    )
    p.add_argument(
        "--daemon-url",
        default=_default_daemon_url(),
        help=(
            "Daemon base URL. Resolution order: $YUYUTSAVA_DAEMON_URL → "
            "~/.yuyutsava/daemon.json discovery file → http://127.0.0.1:7654."
        ),
    )
    p.add_argument(
        "--session-id",
        default=None,
        help="Tag this attach as the origin for SESSION_ID so HITL routes back here.",
    )
    p.add_argument(
        "--label",
        default=os.environ.get("YUYUTSAVA_CLI_LABEL", "yuyutsava-cli"),
        help="Human-readable label logged by the daemon on attach.",
    )
    return p.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    client = CliAttachClient(
        base_url=args.daemon_url,
        session_id=args.session_id,
        label=args.label,
    )
    try:
        info = await client.attach()
        print(
            f"\033[32mattached\033[0m to {args.daemon_url}  "
            f"channel={info['channel_name']}  newly_attached={info['attached']}",
            file=sys.stderr,
        )
    except Exception as exc:
        print(f"attach failed: {exc!r}", file=sys.stderr)
        return 1
    print("(Ctrl-C to detach)", file=sys.stderr, flush=True)

    try:
        async for frame in client.stream():
            if frame.event == "hello":
                continue
            if frame.event == "event":
                render_event_frame(frame.data)
                continue
            if frame.event == "proposal":
                # Tier-1 proposals are out of scope for attach v1.
                p = frame.data.get("proposal", {})
                print(
                    f"\033[35m[proposal] {p.get('instruction', '')[:140]}\033[0m  "
                    "(respond via the Electron renderer for now)",
                    file=sys.stderr, flush=True,
                )
                continue
            if frame.event == "ask":
                ask_id = frame.data.get("ask_id") or ""
                if not ask_id:
                    continue
                reply = await prompt_user_for_ask(frame.data)
                ok = await client.respond_ask(ask_id, reply)
                if not ok:
                    print(
                        f"\033[33m(reply rejected — ask may have already been answered)\033[0m",
                        file=sys.stderr, flush=True,
                    )
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    except Exception:
        logger.exception("attach stream failed")
        return 2
    finally:
        await client.detach()
        await client.close()
        print("\ndetached", file=sys.stderr)
    return 0


def run_attach(argv: list[str]) -> int:
    args = _parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 0
