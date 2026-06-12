"""TelegramChannelPlugin — outbound notify + inbound invoke over Bot API.

Outbound (UserChannel):
    - ``post_event``: ``TokenPayload`` and ``HttpLogPayload`` are suppressed
      entirely; everything else is debounced into 2s batches (Telegram allows
      ~1 msg/s per chat). Completions / errors (``TimelinePayload`` with a
      terminal ``cls``, ``AsyncTaskCompletedPayload``) flush immediately.
    - ``post_proposal``: inline keyboard [Approve][Skip][Modify…]; blocks on
      an ``asyncio.Future`` parked in ``InboundSink.pending_proposals`` (the
      WebHub pattern) so the shared DecisionService can resolve it from any
      surface. Honors ``p.expires_ts``.
    - ``post_ask``: keyboard from options, or force-reply for free text.

Inbound (long-poll loop):
    - ``callback_query`` → ``sink.respond_proposal`` / ``respond_ask``.
    - Allowlisted plain text → ``sink.submit_task(text, origin="telegram")``.
    - Commands: ``/tasks`` (queued + running), ``/status`` (health line).
    - ``getUpdates`` offset persisted via sink state (``telegram.offset``)
      so a daemon restart resumes without replaying updates.

Config: ``YUYUTSAVA_TELEGRAM_BOT_TOKEN`` (env ONLY — never in the config
json), ``YUYUTSAVA_TELEGRAM_CHAT_IDS`` (comma allowlist; messages from any
other chat are dropped and logged at WARNING).
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import time
from typing import Any

from yuyutsava.channels.plugin import (
    CAP_ASK, CAP_INVOKE, CAP_NOTIFY, CAP_PROPOSAL,
    ChannelPlugin, DecisionConflictError, InboundSink,
)
from yuyutsava.channels.telegram.client import TelegramClient
from yuyutsava.daemon.channels import (
    AskPrompt,
    AsyncTaskCompletedPayload,
    ChannelEvent,
    HttpLogPayload,
    LogPayload,
    ProposalDecision,
    TimelinePayload,
    TokenPayload,
    ToolCallPayload,
    ToolResultPayload,
)
from yuyutsava.storage.events import Proposal

logger = logging.getLogger("yuyutsava.channels.telegram.channel")

OFFSET_STATE_KEY = "telegram.offset"

# TimelinePayload classes that mean "a run finished" — these flush the
# debounce buffer immediately so completions reach the phone right away.
_FLUSH_NOW_CLASSES = frozenset({"event-action", "event-error"})

_HELP_TEXT = (
    "Send any text to submit it as a task.\n"
    "/tasks — pending + running tasks\n"
    "/status — daemon health"
)


def _parse_chat_ids(raw: str) -> tuple[int, ...]:
    ids: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError as exc:
            raise ValueError(
                f"YUYUTSAVA_TELEGRAM_CHAT_IDS: {part!r} is not a chat id"
            ) from exc
    return tuple(ids)


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


class TelegramChannelPlugin(ChannelPlugin):
    name = "telegram"
    plugin_id = "telegram"
    capabilities = frozenset({CAP_NOTIFY, CAP_PROPOSAL, CAP_ASK, CAP_INVOKE})

    def __init__(
        self,
        client: TelegramClient,
        chat_ids: tuple[int, ...],
        *,
        poll_timeout_sec: int = 50,
        debounce_sec: float = 2.0,
    ) -> None:
        if not chat_ids:
            raise ValueError("telegram: at least one allowlisted chat id required")
        self._client = client
        self._chat_ids = chat_ids
        self._poll_timeout = poll_timeout_sec
        self._debounce_sec = debounce_sec
        self._sink: InboundSink | None = None
        self._offset: int | None = None
        self._stopped = asyncio.Event()
        self._poll_task: asyncio.Task[None] | None = None
        self._flush_task: asyncio.Task[None] | None = None
        # Debounced outbound lines awaiting the next flush.
        self._buffer: list[str] = []
        # message_id → ("ask", ask_id) | ("modify", proposal_id): force-reply
        # prompts whose *reply* carries the user's free-text answer.
        self._reply_waits: dict[int, tuple[str, str]] = {}
        # ask_id → options list (callback_data carries the option index).
        self._ask_options: dict[str, list[str]] = {}
        # Fire-and-forget sends (immediate flushes) kept alive until done.
        self._bg: set[asyncio.Task[None]] = set()

    @classmethod
    def from_config(cls, params: dict[str, Any]) -> "TelegramChannelPlugin":
        token = os.environ.get("YUYUTSAVA_TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise ValueError(
                "telegram: YUYUTSAVA_TELEGRAM_BOT_TOKEN is not set "
                "(the token is env-only, never channels_config.json)"
            )
        chat_ids = _parse_chat_ids(
            os.environ.get("YUYUTSAVA_TELEGRAM_CHAT_IDS", "")
        )
        if not chat_ids:
            raise ValueError(
                "telegram: YUYUTSAVA_TELEGRAM_CHAT_IDS is not set "
                "(comma-separated allowlist of chat ids)"
            )
        return cls(
            TelegramClient(token),
            chat_ids,
            poll_timeout_sec=int(params.get("poll_timeout_sec", 50)),
            debounce_sec=float(params.get("debounce_sec", 2.0)),
        )

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    async def start(self, inbound: InboundSink) -> None:
        self._sink = inbound
        self._stopped.clear()
        stored = inbound.get_state(OFFSET_STATE_KEY, None)
        self._offset = int(stored) if isinstance(stored, (int, float, str)) and str(stored).strip() else None
        self._poll_task = asyncio.create_task(self._poll_loop(), name="telegram-poll")
        self._flush_task = asyncio.create_task(self._flush_loop(), name="telegram-flush")
        logger.info("telegram: started (chats: %d, offset: %s)",
                    len(self._chat_ids), self._offset)

    async def stop(self) -> None:
        self._stopped.set()
        for task in (self._poll_task, self._flush_task, *self._bg):
            if task is not None and not task.done():
                task.cancel()
        for task in (self._poll_task, self._flush_task, *self._bg):
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._poll_task = None
        self._flush_task = None
        self._bg.clear()
        await self._client.aclose()
        logger.info("telegram: stopped")

    async def shutdown(self) -> None:
        # UserChannel hook — the registry owns stop(); router.shutdown()
        # must not double-close a plugin the registry already stopped.
        if not self._stopped.is_set():
            await self.stop()

    # ------------------------------------------------------------------ #
    # Outbound: events                                                    #
    # ------------------------------------------------------------------ #

    async def post_event(self, ev: ChannelEvent) -> None:
        p = ev.payload
        if isinstance(p, (TokenPayload, HttpLogPayload)):
            return
        line = self._format_payload(p)
        if not line:
            return
        self._buffer.append(line)
        flush_now = isinstance(p, AsyncTaskCompletedPayload) or (
            isinstance(p, TimelinePayload) and p.cls in _FLUSH_NOW_CLASSES
        )
        if flush_now:
            # Don't block the router's fan-out gather on Telegram's network;
            # the send runs in the background and logs its own failures.
            t = asyncio.create_task(self._flush())
            self._bg.add(t)
            t.add_done_callback(self._bg.discard)

    @staticmethod
    def _format_payload(p: Any) -> str:
        if isinstance(p, LogPayload):
            return p.text.strip()
        if isinstance(p, TimelinePayload):
            return p.line.strip()
        if isinstance(p, ToolCallPayload):
            return f"⚙ {p.name}"
        if isinstance(p, ToolResultPayload):
            preview = (p.preview or "").strip().replace("\n", " ")[:120]
            return f"⚙ {p.name} → {preview}" if preview else ""
        if isinstance(p, AsyncTaskCompletedPayload):
            mark = "✅" if p.ok else "❌"
            return f"{mark} bg {p.agent_name}: {p.summary[:200]}"
        # Other async-task chatter: keep it terse.
        text = getattr(p, "text", "") or getattr(p, "title", "")
        return str(text).strip()[:200]

    async def _flush_loop(self) -> None:
        while not self._stopped.is_set():
            await asyncio.sleep(self._debounce_sec)
            await self._flush()

    async def _flush(self) -> None:
        if not self._buffer:
            return
        lines, self._buffer = self._buffer, []
        text = "\n".join(dict.fromkeys(lines))  # de-dupe, keep order
        await self._send_to_all(_esc(text)[:4000])

    async def _send_to_all(
        self, text: str, *, reply_markup: dict[str, Any] | None = None,
    ) -> dict[int, int]:
        """Send to every allowlisted chat. Returns chat_id → message_id."""
        sent: dict[int, int] = {}
        for chat_id in self._chat_ids:
            try:
                msg = await self._client.send_message(
                    chat_id, text, reply_markup=reply_markup,
                )
                if isinstance(msg, dict) and msg.get("message_id"):
                    sent[chat_id] = int(msg["message_id"])
            except Exception:
                logger.exception("telegram: send to chat %s failed", chat_id)
        return sent

    # ------------------------------------------------------------------ #
    # Outbound: proposals + asks                                          #
    # ------------------------------------------------------------------ #

    async def post_proposal(self, p: Proposal) -> ProposalDecision:
        if self._sink is None:
            raise NotImplementedError("telegram plugin not started")
        text = (
            f"<b>Proposal</b> — {_esc(p.summary)}\n"
            f"{_esc(p.proposed)}\n"
            f"<i>subagent: {_esc(p.subagent)} · urgency {p.urgency}</i>"
        )
        keyboard = {"inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": f"p:{p.proposal_id}:a"},
            {"text": "⏭ Skip", "callback_data": f"p:{p.proposal_id}:s"},
            {"text": "✏️ Modify…", "callback_data": f"p:{p.proposal_id}:m"},
        ]]}
        loop = asyncio.get_running_loop()
        fut: "asyncio.Future[ProposalDecision]" = loop.create_future()
        self._sink.pending_proposals[p.proposal_id] = fut
        sent = await self._send_to_all(text, reply_markup=keyboard)
        if not sent:
            # Nothing reached the user — let the router try the next channel.
            self._sink.pending_proposals.pop(p.proposal_id, None)
            raise NotImplementedError("telegram: proposal could not be delivered")
        timeout = max(1.0, p.expires_ts - time.time())
        try:
            decision = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            decision = ProposalDecision(decision="expired")
        finally:
            self._sink.pending_proposals.pop(p.proposal_id, None)
        for chat_id, message_id in sent.items():
            try:
                await self._client.edit_message_text(
                    chat_id, message_id,
                    f"{text}\n→ <b>{_esc(decision.decision)}</b>",
                )
            except Exception:
                logger.debug("telegram: proposal message edit failed", exc_info=True)
        return decision

    async def post_ask(self, a: AskPrompt) -> str:
        if self._sink is None:
            raise NotImplementedError("telegram plugin not started")
        text = f"<b>{_esc(a.title)}</b>\n{_esc(a.body)}"
        loop = asyncio.get_running_loop()
        fut: "asyncio.Future[str]" = loop.create_future()
        self._sink.pending_asks[a.ask_id] = fut
        try:
            if a.options:
                self._ask_options[a.ask_id] = list(a.options)
                keyboard = {"inline_keyboard": [
                    [{"text": opt, "callback_data": f"a:{a.ask_id}:{i}"}]
                    for i, opt in enumerate(a.options)
                ]}
                sent = await self._send_to_all(text, reply_markup=keyboard)
            else:
                sent = await self._send_to_all(
                    text + "\n<i>Reply to this message with your answer.</i>",
                    reply_markup={"force_reply": True},
                )
                for message_id in sent.values():
                    self._reply_waits[message_id] = ("ask", a.ask_id)
            if not sent:
                raise NotImplementedError("telegram: ask could not be delivered")
            return await fut
        finally:
            self._sink.pending_asks.pop(a.ask_id, None)
            self._ask_options.pop(a.ask_id, None)

    # ------------------------------------------------------------------ #
    # Inbound: long-poll loop                                             #
    # ------------------------------------------------------------------ #

    async def _poll_loop(self) -> None:
        try:
            await self._client.set_my_commands([
                {"command": "tasks", "description": "Pending and running tasks"},
                {"command": "status", "description": "Daemon health"},
                {"command": "help", "description": "How to use this bot"},
            ])
        except Exception:
            logger.warning("telegram: setMyCommands failed (continuing)", exc_info=True)

        error_delay = 1.0
        while not self._stopped.is_set():
            try:
                updates = await self._client.get_updates(
                    offset=self._offset, timeout=self._poll_timeout,
                )
                error_delay = 1.0
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "telegram: getUpdates failed; retrying in %.0fs", error_delay,
                )
                await asyncio.sleep(error_delay)
                error_delay = min(error_delay * 2, 60.0)
                continue
            for update in updates:
                self._offset = int(update.get("update_id", 0)) + 1
                try:
                    await self._handle_update(update)
                except Exception:
                    # One bad update must not kill the poller.
                    logger.exception("telegram: update handling failed")
            if updates and self._sink is not None:
                await self._sink.put_state(OFFSET_STATE_KEY, self._offset)

    def _allowed(self, chat_id: Any) -> bool:
        try:
            return int(chat_id) in self._chat_ids
        except (TypeError, ValueError):
            return False

    async def _handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            await self._handle_callback(update["callback_query"])
            return
        message = update.get("message")
        if isinstance(message, dict):
            await self._handle_message(message)

    # --- callback queries (inline keyboard taps) -------------------------

    async def _handle_callback(self, cq: dict[str, Any]) -> None:
        cq_id = str(cq.get("id", ""))
        chat = ((cq.get("message") or {}).get("chat") or {})
        if not self._allowed(chat.get("id")):
            logger.warning("telegram: callback from non-allowlisted chat %s dropped",
                           chat.get("id"))
            return
        data = str(cq.get("data", ""))
        parts = data.split(":", 2)
        toast = "ok"
        try:
            if len(parts) == 3 and parts[0] == "p":
                _, proposal_id, action = parts
                if action == "m":
                    sent = await self._send_to_all(
                        "✏️ Reply to this message with the modified instruction.",
                        reply_markup={"force_reply": True},
                    )
                    for message_id in sent.values():
                        self._reply_waits[message_id] = ("modify", proposal_id)
                    toast = "send the new instruction"
                else:
                    decision = "approve" if action == "a" else "skip"
                    await self._sink.respond_proposal(proposal_id, decision)
                    toast = decision
            elif len(parts) == 3 and parts[0] == "a":
                _, ask_id, idx_raw = parts
                options = self._ask_options.get(ask_id, [])
                try:
                    response = options[int(idx_raw)]
                except (ValueError, IndexError):
                    response = idx_raw
                await self._sink.respond_ask(ask_id, response)
                toast = response[:60]
            else:
                logger.warning("telegram: unrecognized callback data %r", data)
                toast = "unrecognized button"
        except DecisionConflictError:
            toast = "already resolved or expired"
        try:
            await self._client.answer_callback_query(cq_id, text=toast)
        except Exception:
            logger.debug("telegram: answerCallbackQuery failed", exc_info=True)

    # --- plain messages ----------------------------------------------------

    async def _handle_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if not self._allowed(chat_id):
            logger.warning("telegram: message from non-allowlisted chat %s dropped",
                           chat_id)
            return
        text = (message.get("text") or "").strip()
        if not text:
            return

        # Replies to force-reply prompts: free-text ask answers / modify text.
        reply_to = (message.get("reply_to_message") or {}).get("message_id")
        if reply_to in self._reply_waits:
            kind, target_id = self._reply_waits.pop(reply_to)
            try:
                if kind == "ask":
                    await self._sink.respond_ask(target_id, text)
                else:
                    await self._sink.respond_proposal(
                        target_id, "modify", edited_instruction=text,
                    )
                await self._client.send_message(chat_id, "✓ got it", parse_mode=None)
            except DecisionConflictError:
                await self._client.send_message(
                    chat_id, "That prompt already expired.", parse_mode=None,
                )
            return

        command = text.split()[0].lower() if text.startswith("/") else ""
        if command in ("/start", "/help"):
            await self._client.send_message(chat_id, _HELP_TEXT, parse_mode=None)
        elif command == "/status":
            await self._client.send_message(
                chat_id, self._sink.daemon_status(), parse_mode=None,
            )
        elif command == "/tasks":
            await self._client.send_message(
                chat_id, await self._format_tasks(), parse_mode=None,
            )
        else:
            task_id = await self._sink.submit_task(text, origin="telegram")
            await self._client.send_message(
                chat_id, f"✓ task submitted: {task_id}", parse_mode=None,
            )

    async def _format_tasks(self) -> str:
        pending = await self._sink.list_pending()
        tasks = pending.get("tasks", [])
        if not tasks:
            return "No queued or running tasks."
        lines = []
        for t in tasks[:15]:
            instr = (t.get("instruction") or "").replace("\n", " ")[:80]
            lines.append(f"[{t.get('status')}] {t.get('task_id')} — {instr}")
        n_props = len(pending.get("pending_proposal_ids", []))
        n_asks = len(pending.get("pending_ask_ids", []))
        if n_props or n_asks:
            lines.append(f"(awaiting you: {n_props} proposal(s), {n_asks} ask(s))")
        return "\n".join(lines)
