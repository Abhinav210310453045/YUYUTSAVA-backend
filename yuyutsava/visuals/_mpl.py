"""matplotlib plumbing shared by the chart/table/math/timeline renderers.

Forces the headless ``Agg`` backend at import time so the library renders inside
the daemon (no display) and in background sub-agents.
"""

from __future__ import annotations

import io

import matplotlib

matplotlib.use("Agg")  # must precede pyplot import; headless PNG rendering
import matplotlib.pyplot as plt  # noqa: E402


def figure_to_png(fig, *, dpi: int = 144) -> tuple[bytes, int, int]:
    """Encode a matplotlib figure to PNG bytes and return (bytes, width, height)."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    data = buf.getvalue()
    # Pixel size = figure inches * dpi (bbox_inches='tight' may trim slightly).
    w, h = (int(v * dpi) for v in fig.get_size_inches())
    return data, w, h
