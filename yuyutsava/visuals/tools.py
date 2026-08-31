"""LLM tool adapters over the visuals library (``vis_*`` family).

Thin wrappers: validate → :func:`yuyutsava.visuals.render.render` → persist via
:class:`~yuyutsava.visuals.store.VisualStore` → return a JSON string carrying
*both* the on-disk ``path`` (so the CLI/user can open it) and the serving ``url``
(so the UI can render it). The core library never imports this module.

Factory ``make_visual_tools`` mirrors ``make_search_tools`` and is safe to call
unconditionally — it always returns the full family.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, tool

from .render import render
from .store import VisualStore, get_default_visual_store
from .types import VisualError

logger = logging.getLogger("yuyutsava.visuals.tools")

# The image-serving route (see daemon/web/routers/visuals.py). visual_id is a
# globally-unique ULID, so a flat path is enough and needs no session id.
def _url(visual_id: str) -> str:
    return f"/visuals/{visual_id}"


def make_visual_tools(
    *,
    store: VisualStore | None = None,
    output_dir: str | Path | None = None,
) -> list[BaseTool]:
    """Build the ``vis_*`` tools. ``store`` defaults to the backend-aware shared
    store (Postgres when the daemon injected it, else SQLite); ``output_dir`` is
    the agent's ``_output`` dir so files land in the workspace.
    """
    store = store or get_default_visual_store()

    async def _finish(kind: str, spec: dict[str, Any]) -> str:
        # Local import to avoid a hard dependency on the agent runtime at import.
        from yuyutsava.context.artifacts import thread_id_from_runtime

        try:
            result = render(kind, spec)
        except VisualError as exc:
            return json.dumps({"status": "error", "error": str(exc)})
        except Exception as exc:  # defensive: never crash a turn on a bad spec
            logger.exception("visual render failed for kind=%s", kind)
            return json.dumps({"status": "error", "error": f"render failed: {exc}"})

        rec = await store.save(result, thread_id_from_runtime(), out_dir=output_dir)
        return json.dumps({
            "status": "ok",
            "visual_id": rec.visual_id,
            "kind": rec.kind,
            "title": rec.title,
            "path": rec.path,
            "url": _url(rec.visual_id),
        })

    @tool
    async def vis_chart(spec: dict) -> str:
        """Render a data chart (matplotlib/seaborn) to a PNG the user can see.

        spec keys:
          chart_type: "bar" | "barh" | "line" | "pie" | "scatter" | "histogram" | "heatmap"
            (use "barh" — horizontal bars — when category names are long, e.g.
            process names, so labels never overlap)
          title, x_label, y_label: optional strings
          bar/barh/line/scatter: labels: [str,...]; series: [{name, data:[num,...]}, ...]
          pie: values: [num,...]; labels: [str,...]
          histogram: values: [num,...]; bins: int
          heatmap: matrix: [[num,...],...]; row_labels, col_labels: [str,...]
        Returns JSON with visual_id, on-disk path, and a url for the UI.
        """
        return await _finish("chart", spec)

    @tool
    async def vis_diagram(language: str, source: str, title: str | None = None) -> str:
        """Render a diagram-as-code source to PNG via the diagram backend (Kroki).

        language: "mermaid" | "graphviz" | "plantuml" | "d2" (and other Kroki ids).
        source: the diagram script (e.g. a mermaid `flowchart TD ...`). Graphviz
        also works offline via a local `dot`. Returns JSON with path + url, or an
        error if no diagram backend is reachable.
        """
        return await _finish("diagram", {"language": language, "source": source, "title": title})

    @tool
    async def vis_table(spec: dict) -> str:
        """Render tabular data as a styled table image.

        spec keys: columns: [str,...]; rows: [[cell,...],...] (same width as columns);
        title: optional; highlight: {row:int, col:int} optional (0-based) to emphasize
        one cell. Returns JSON with path + url.
        """
        return await _finish("table", spec)

    @tool
    async def vis_code(source: str, language: str | None = None, title: str | None = None) -> str:
        """Render a syntax-highlighted code snippet as a shareable image.

        source: the code; language: e.g. "python" (auto-detected if omitted).
        Returns JSON with path + url.
        """
        return await _finish("code", {"source": source, "language": language, "title": title})

    @tool
    async def vis_math(latex: str, title: str | None = None) -> str:
        """Render a LaTeX math expression to a PNG (no LaTeX install needed).

        latex: the expression WITHOUT surrounding $ (e.g. "E = mc^2" or
        "\\frac{a}{b}"). Returns JSON with path + url.
        """
        return await _finish("math", {"latex": latex, "title": title})

    @tool
    async def vis_timeline(spec: dict) -> str:
        """Render a timeline / Gantt chart of dated or numbered items.

        spec keys: title: optional; items: [{label, start, end, status?}, ...] where
        start/end are ISO dates ("2026-07-01") or numbers, and status is one of
        done|active|todo|blocked (colors the bar). Returns JSON with path + url.
        """
        return await _finish("timeline", spec)

    @tool
    async def vis_list_artifacts() -> str:
        """List the visuals already rendered in THIS conversation (no re-render).

        Use this to find a chart/diagram/table you (or an earlier turn) made
        before, e.g. to show it again. Returns JSON:
        {"status":"ok","artifacts":[{visual_id, kind, title, created_ts}, ...]}.
        """
        from yuyutsava.context.artifacts import thread_id_from_runtime

        records = await store.list_for_thread(thread_id_from_runtime())
        return json.dumps({
            "status": "ok",
            "artifacts": [
                {"visual_id": r.visual_id, "kind": r.kind, "title": r.title,
                 "created_ts": r.created_ts}
                for r in records
            ],
        })

    @tool
    async def vis_show_artifact(visual_id: str) -> str:
        """Re-show an EXISTING rendered visual inline — WITHOUT re-creating it.

        Use this when the user asks to see a chart/diagram again that was already
        made (find its id with vis_list_artifacts). It re-embeds the saved image
        in the chat instantly (no re-render, no recomputation). Returns the usual
        {"status":"ok", visual_id, kind, title, path, url}; unknown id → error.
        """
        rec = await store.get(visual_id)
        if rec is None:
            return json.dumps({
                "status": "error",
                "error": f"no visual with id {visual_id!r} — call vis_list_artifacts first",
            })
        return json.dumps({
            "status": "ok",
            "visual_id": rec.visual_id,
            "kind": rec.kind,
            "title": rec.title,
            "path": rec.path,
            "url": _url(rec.visual_id),
        })

    return [
        vis_chart, vis_diagram, vis_table, vis_code, vis_math, vis_timeline,
        vis_list_artifacts, vis_show_artifact,
    ]
