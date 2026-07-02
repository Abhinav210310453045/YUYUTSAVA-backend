"""YUYUTSAVA visuals — a delivery-agnostic rendering library.

Give the agent "visual wings": charts, diagrams, styled tables, code images,
math and timelines. The core (:func:`render`) has no dependency on LangChain,
FastAPI, or the agent runtime, so the same renderers are reused by:

  - LLM tools   → ``yuyutsava.visuals.tools.make_visual_tools``
  - REST / SSE  → ``yuyutsava.daemon.web.routers.visuals``

Persist results with :class:`~yuyutsava.visuals.store.VisualStore`.
"""

from __future__ import annotations

from .render import render
from .types import KINDS, RenderResult, VisualError

__all__ = ["render", "RenderResult", "VisualError", "KINDS"]
