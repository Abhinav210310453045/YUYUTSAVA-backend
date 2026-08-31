"""Minimal async Telegram Bot API client (httpx, no python-telegram-bot).

Only the handful of methods the channel plugin needs. Long-polling via
``getUpdates(timeout=50)`` works behind NAT — the daemon dials out, nothing
dials in.

Network errors are retried with exponential backoff (1s → 60s cap, bounded
attempts) so a flaky link doesn't kill the poll loop; Bot API-level errors
(``ok: false``) raise :class:`TelegramApiError` immediately — retrying a
4xx is pointless. The bot token never appears in logs (it is part of every
request URL, so failures log the method name only).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

logger = logging.getLogger("yuyutsava.channels.telegram.client")

_BACKOFF_INITIAL_SEC = 1.0
_BACKOFF_CAP_SEC = 60.0
_MAX_ATTEMPTS = 6


class TelegramApiError(Exception):
    """Bot API answered ``ok: false`` (or non-JSON garbage)."""

    def __init__(self, method: str, description: str, *, retry_after: float = 0.0) -> None:
        super().__init__(f"telegram {method}: {description}")
        self.method = method
        self.description = description
        self.retry_after = retry_after


class TelegramClient:
    """Thin wrapper over ``https://api.telegram.org/bot<token>/<method>``."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://api.telegram.org",
        # Long-poll requests hold the connection up to ``getUpdates``'s
        # timeout param; the read timeout must comfortably exceed it.
        read_timeout_sec: float = 75.0,
    ) -> None:
        if not token:
            raise ValueError("telegram bot token must not be empty")
        self._base = f"{base_url.rstrip('/')}/bot{token}"
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, read=read_timeout_sec),
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ #
    # Bot API methods                                                     #
    # ------------------------------------------------------------------ #

    async def get_me(self) -> dict[str, Any]:
        return await self._call("getMe")

    async def get_updates(
        self, offset: int | None = None, *, timeout: int = 50,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        result = await self._call("getUpdates", **params)
        return result if isinstance(result, list) else []

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = "HTML",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"chat_id": chat_id, "text": text[:4096]}
        if parse_mode:
            params["parse_mode"] = parse_mode
        if reply_markup is not None:
            params["reply_markup"] = json.dumps(reply_markup)
        return await self._call("sendMessage", **params)

    async def edit_message_text(
        self,
        chat_id: int | str,
        message_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = "HTML",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "chat_id": chat_id, "message_id": message_id, "text": text[:4096],
        }
        if parse_mode:
            params["parse_mode"] = parse_mode
        if reply_markup is not None:
            params["reply_markup"] = json.dumps(reply_markup)
        return await self._call("editMessageText", **params)

    async def answer_callback_query(
        self, callback_query_id: str, *, text: str = "",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            params["text"] = text[:200]
        return await self._call("answerCallbackQuery", **params)

    async def set_my_commands(self, commands: list[dict[str, str]]) -> dict[str, Any]:
        return await self._call("setMyCommands", commands=json.dumps(commands))

    # ------------------------------------------------------------------ #
    # Transport                                                           #
    # ------------------------------------------------------------------ #

    async def _call(self, method: str, **params: Any) -> Any:
        delay = _BACKOFF_INITIAL_SEC
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                resp = await self._http.post(f"{self._base}/{method}", data=params)
            except httpx.HTTPError:
                if attempt >= _MAX_ATTEMPTS:
                    raise
                logger.warning(
                    "telegram %s: network error (attempt %d/%d), retrying in %.0fs",
                    method, attempt, _MAX_ATTEMPTS, delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, _BACKOFF_CAP_SEC)
                continue

            try:
                body = resp.json()
            except ValueError as exc:
                raise TelegramApiError(method, f"non-JSON response ({resp.status_code})") from exc
            if not body.get("ok", False):
                retry_after = float(
                    (body.get("parameters") or {}).get("retry_after", 0) or 0
                )
                if retry_after and attempt < _MAX_ATTEMPTS:
                    # 429 flood control: honor Telegram's own pacing hint.
                    logger.warning(
                        "telegram %s: rate limited, retrying in %.0fs",
                        method, retry_after,
                    )
                    await asyncio.sleep(retry_after)
                    continue
                raise TelegramApiError(
                    method, str(body.get("description", "unknown error")),
                    retry_after=retry_after,
                )
            return body.get("result")
        raise TelegramApiError(method, "retries exhausted")  # pragma: no cover
