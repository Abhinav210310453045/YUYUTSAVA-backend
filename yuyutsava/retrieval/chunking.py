"""Generic text chunking for embedding/indexing.

Splits a body into windows small enough to embed while keeping each chunk's
exact ``char_offset`` into the original string — so a semantic hit can map back
to the source (e.g. ``ctx_fetch_artifact(offset=char_offset)``). Domain-neutral:
usable by the artifact index, memory, skills, or any future indexer. No DB or
embedder dependency.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_TARGET_CHARS = 1_200


@dataclass(frozen=True)
class TextChunk:
    """One window of a larger string."""

    seq: int
    char_offset: int  # start index into the original content
    text: str


def chunk_text(
    content: str,
    *,
    target_chars: int = DEFAULT_TARGET_CHARS,
    overlap: int = 0,
) -> list[TextChunk]:
    """Window *content* into ``target_chars``-sized chunks on whitespace breaks.

    Offsets are exact relative to *content*. Whitespace-only chunks are dropped.
    ``overlap`` (chars) repeats tail context into the next chunk for better
    recall across boundaries; 0 keeps chunks disjoint. Robust to pathological
    input (no whitespace, tiny target) — always makes forward progress.
    """
    n = len(content)
    if n == 0:
        return []
    target = max(1, target_chars)
    step_floor = max(1, target // 2)  # never break before this point in a window

    chunks: list[TextChunk] = []
    seq = 0
    pos = 0
    while pos < n:
        end = min(pos + target, n)
        if end < n:
            # Prefer a newline, then a space, no earlier than the window's
            # midpoint — so we break on a boundary without making tiny chunks.
            floor = pos + step_floor
            brk = max(content.rfind("\n", floor, end), content.rfind(" ", floor, end))
            if brk > pos:
                end = brk + 1
        text = content[pos:end]
        if text.strip():
            chunks.append(TextChunk(seq=seq, char_offset=pos, text=text))
            seq += 1
        if end <= pos:  # safety: guarantee progress
            end = min(pos + target, n) or n
        nxt = end - overlap if 0 < overlap < end - pos else end
        pos = nxt if nxt > pos else end
    return chunks
