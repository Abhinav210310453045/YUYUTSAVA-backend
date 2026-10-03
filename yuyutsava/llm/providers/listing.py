"""Fetching a provider's model list over HTTP, with failures said in plain words.

Every listing endpoint fails the same few ways — the server cannot be reached,
the login is refused, it answers with an error — and each needs the same
explanation whichever provider it is. :func:`get_json` returns the parsed body,
or the problem and its fix for a :class:`~yuyutsava.llm.discovery.Discovery`.
Called off the event loop: it blocks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

#: How long a listing may take before the provider is called unreachable.
LISTING_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class Listing:
    """A listing endpoint's answer: its JSON, or why there is none."""

    body: Any = None
    problem: str = ""
    fix: str = ""


def get_json(
    url: str,
    *,
    label: str,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    unreachable_fix: str = "check the address and your network connection",
) -> Listing:
    """GET *url* and parse its JSON; *label* names the provider in a problem."""
    host = urlsplit(url).netloc or url
    try:
        response = httpx.get(url, headers=headers, params=params, timeout=LISTING_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        return Listing(
            problem=f"could not reach {label} at {host} ({type(exc).__name__})", fix=unreachable_fix
        )
    if response.status_code in (401, 403):
        return Listing(
            problem=f"{label} refused the login ({response.status_code}): {_message(response)}",
            fix="check the key or credentials this provider is set up with",
        )
    if response.status_code >= 400:
        return Listing(
            problem=f"{label} answered {response.status_code}: {_message(response)}",
            fix="try again later; if it persists, check the provider's status page",
        )
    try:
        return Listing(body=response.json())
    except ValueError:
        return Listing(
            problem=f"{label} answered with something that is not JSON", fix="check the address"
        )


def _message(response: httpx.Response) -> str:
    """The provider's own error message, or the start of the body."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)[:200]
    if isinstance(error, str):
        return error[:200]
    return str(body)[:200]


__all__ = ["LISTING_TIMEOUT_SECONDS", "Listing", "get_json"]
