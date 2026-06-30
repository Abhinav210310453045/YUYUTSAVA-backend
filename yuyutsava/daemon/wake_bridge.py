"""Wake-word → web bridge.

Subscribes to ``voice.wake`` on the event bus and relays each detection to the
web SSE hub as a :class:`StreamWakeItem`, so a connected UI (Electron/mobile)
can pop its voice overlay and start a converse session.

This is intentionally a thin, separate loop rather than logic inside the triage
loop: triage classifies events into proposals, whereas a wake word is a direct
UI actuation signal that should reach the front-end immediately and unfiltered.
The captured-utterance blob is *not* forwarded — the overlay opens a fresh
live-mic converse session; only the small wake metadata travels over SSE.
"""

from __future__ import annotations

import asyncio
import logging

from yuyutsava.daemon.web.services.stream_service import StreamWakeItem, WebHub
from yuyutsava.events.bus import EventBus

logger = logging.getLogger("yuyutsava.daemon.wake_bridge")


async def run_wake_bridge(
    bus: EventBus, hub: WebHub, stop_event: asyncio.Event
) -> None:
    """Relay ``voice.wake`` bus events onto the web hub until stopped."""
    logger.info("wake bridge: relaying voice.wake → web hub")
    stop_task = asyncio.create_task(stop_event.wait(), name="wake-bridge-stop")
    try:
        agen = bus.subscribe("voice.wake")
        async for ev in agen:
            if stop_event.is_set():
                break
            # ``stage`` distinguishes the instant overlay-pop ("open") from the
            # trailing same-breath command ("command"); the command text rides in
            # ``hints`` (the bus envelope omits the persisted payload) so the
            # overlay can seed it as the first turn without re-popping.
            stage = ev.hints.get("stage", "open")
            command = ev.hints.get("command", "") if stage == "command" else ""
            await hub.broadcast(StreamWakeItem(
                wake_word=ev.hints.get("wake_word", ""),
                transcript=command,
                stage=stage,
                command=command,
                ts=ev.ts,
            ))
            logger.debug(
                "wake bridge: relayed %s (%s)", stage, ev.hints.get("wake_word", ""),
            )
    except asyncio.CancelledError:
        raise
    except Exception:  # pragma: no cover - defensive; never crash the daemon
        logger.exception("wake bridge: unexpected error")
    finally:
        stop_task.cancel()
