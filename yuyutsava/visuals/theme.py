"""Shared dark palette so every visual matches the app's neon UI.

Colours mirror the renderer's ``globals.css`` custom properties (neon green /
amber / red / blue on a near-black card). Renderers call :func:`apply_matplotlib`
once per figure; the diagram/code backends read the hex values directly.
"""

from __future__ import annotations

# Core palette (hex) — kept in one place so charts, tables, code and math agree.
BG_DEEP = "#0a0e12"
BG_CARD = "#121820"
GRID = "#243040"
TEXT = "#e6edf3"
TEXT_MUTED = "#8b98a5"

# Neon accents, used in order for multi-series charts.
ACCENTS = [
    "#00ff88",  # green
    "#78a0ff",  # blue
    "#ffb000",  # amber
    "#ff3366",  # red
    "#b47bff",  # violet
    "#2ee6d6",  # teal
]

# Pygments style name for code-to-image (dark, ships with pygments).
CODE_STYLE = "monokai"
# Kroki theme hint for Mermaid diagrams.
MERMAID_THEME = "dark"


def apply_matplotlib(fig, ax_list) -> None:
    """Paint a matplotlib figure + axes with the dark theme in-place."""
    fig.patch.set_facecolor(BG_DEEP)
    for ax in ax_list:
        ax.set_facecolor(BG_CARD)
        ax.tick_params(colors=TEXT_MUTED, labelsize=9)
        for spine in ax.spines.values():
            spine.set_color(GRID)
        ax.title.set_color(TEXT)
        ax.xaxis.label.set_color(TEXT_MUTED)
        ax.yaxis.label.set_color(TEXT_MUTED)
        ax.grid(True, color=GRID, linewidth=0.5, alpha=0.6)
