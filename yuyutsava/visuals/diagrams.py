"""Diagram-as-code renderer.

Renders Mermaid / Graphviz / PlantUML / D2 (and any other Kroki language) to PNG.
Graphviz gets a pure-local fallback via the ``dot`` binary so the most common
diagram type works even when no Kroki service is running.

Spec shape::

    {"language": "mermaid" | "graphviz" | "plantuml" | "d2" | ...,
     "source": str, "title": str | None}
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any

from ._kroki import render_via_kroki
from .types import RenderResult, VisualError

# Common aliases → canonical Kroki language ids.
_ALIASES = {
    "dot": "graphviz",
    "gv": "graphviz",
    "puml": "plantuml",
    "uml": "plantuml",
    "mmd": "mermaid",
    "flowchart": "mermaid",
    "sequence": "mermaid",
}


def render_diagram(spec: dict[str, Any]) -> RenderResult:
    language = str(spec.get("language", "")).lower().strip()
    language = _ALIASES.get(language, language)
    source = spec.get("source")
    if not language:
        raise VisualError("diagram requires a 'language' (e.g. mermaid, graphviz, plantuml, d2)")
    if not source:
        raise VisualError("diagram requires 'source'")

    if language == "graphviz":
        data = _render_graphviz(source)
    else:
        data = render_via_kroki(language, source)

    return RenderResult(
        image_bytes=data,
        mime="image/png",
        kind="diagram",
        title=spec.get("title") or language,
        width=None,
        height=None,
        source=source,
    )


def _render_graphviz(source: str) -> bytes:
    """Prefer the local ``dot`` binary; fall back to Kroki if it is absent."""
    dot = shutil.which("dot")
    if not dot:
        return render_via_kroki("graphviz", source)
    try:
        proc = subprocess.run(
            [dot, "-Tpng"],
            input=source.encode("utf-8"),
            capture_output=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise VisualError(f"local graphviz render failed: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace")[:300].strip()
        raise VisualError(f"graphviz rejected the DOT source: {detail}")
    return proc.stdout
