"""Thin Kroki HTTP client for diagram rendering.

Kroki exposes one endpoint per language: ``POST {base}/{language}/{format}`` with
the raw diagram source as the request body, returning the encoded image. A single
service therefore renders Mermaid, Graphviz, PlantUML, D2 and ~20 more.

Backend URL comes from ``YUYUTSAVA_KROKI_URL`` (default ``http://localhost:8000``)
so it can point at a self-hosted container, the public ``https://kroki.io``, or be
swapped without touching the renderers.
"""

from __future__ import annotations

import os

import requests

from .types import VisualError

DEFAULT_KROKI_URL = "http://localhost:8000"
_TIMEOUT = 20


def kroki_base_url() -> str:
    return os.environ.get("YUYUTSAVA_KROKI_URL", DEFAULT_KROKI_URL).rstrip("/")


def render_via_kroki(language: str, source: str, *, fmt: str = "png") -> bytes:
    """POST diagram *source* to Kroki and return the encoded image bytes.

    Raises :class:`VisualError` (never a bare requests error) so a down backend
    surfaces as a clean "diagram backend unavailable" message to the agent.
    """
    url = f"{kroki_base_url()}/{language}/{fmt}"
    try:
        resp = requests.post(
            url,
            data=source.encode("utf-8"),
            headers={"Content-Type": "text/plain"},
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise VisualError(
            f"diagram backend unavailable at {kroki_base_url()} ({exc}). "
            "Start Kroki (`docker run -p8000:8000 yuzutech/kroki`) or set "
            "YUYUTSAVA_KROKI_URL."
        ) from exc

    if resp.status_code != 200:
        detail = resp.text[:300].strip()
        raise VisualError(
            f"diagram backend rejected the {language} source (HTTP {resp.status_code}): {detail}"
        )
    return resp.content
