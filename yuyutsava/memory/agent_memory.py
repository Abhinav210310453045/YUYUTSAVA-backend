"""Per-agent user-behavior memory — a Claude-style file store.

Each MASTER agent (orchestrator, CLI deepagent, tinker) keeps a directory of
small markdown notes about durable behaviors of THIS user (phrasing habits,
standing constraints, recurring corrections), plus a ``MEMORY.md`` index:

    ~/.yuyutsava/agents/<agent>/memory/
        MEMORY.md          ← one line per note; the ONLY thing injected
        <slug>.md          ← one learned behavior each

Only the index rides in the system prompt (hard-capped), so the prompt cost is
fixed while the knowledge base grows; note bodies are read on demand via the
``um_read`` tool. The note files are the source of truth — the index is
rewritten from the file set on every mutation, so it can never drift or
corrupt (self-healing on the next write).

This deliberately stays separate from :mod:`yuyutsava.memory.store` (semantic
long-term memory, globally scoped): agent memory is per-agent, tiny, always
injected, and human-readable on disk.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger("yuyutsava.memory.agent_memory")

INDEX_NAME = "MEMORY.md"
MAX_NOTES = 30
MAX_BODY_CHARS = 4_000
MAX_SUMMARY_CHARS = 200
MAX_INDEX_BLOCK_CHARS = 2_000

_HEADER = (
    "## AGENT MEMORY — learned behaviors of this user "
    "(read one in full with um_read(name))\n"
)


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9-]", "-", name.lower().strip())
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug or "note"


class AgentMemoryStore:
    """Sync, stdlib-only file store. Callers on the event loop wrap the
    methods in ``asyncio.to_thread`` (the tool layer does)."""

    def __init__(self, agent_name: str, home: Path | None = None) -> None:
        self.agent_name = agent_name
        base = home or (Path.home() / ".yuyutsava")
        self._dir = base / "agents" / agent_name / "memory"

    # ── read side ─────────────────────────────────────────────────────

    def read_index_block(self) -> str:
        """The injectable prompt block: header + index lines, capped.

        Returns "" when the agent has learned nothing yet. Never raises —
        a broken memory dir must not take a session down."""
        try:
            lines = self._index_lines()
            if not lines:
                return ""
            block = _HEADER
            shown = 0
            for line in lines:
                if len(block) + len(line) + 1 > MAX_INDEX_BLOCK_CHARS:
                    block += f"… (+{len(lines) - shown} more — um_read by name)\n"
                    break
                block += line + "\n"
                shown += 1
            return block.rstrip()
        except Exception:
            logger.warning(
                "agent memory: failed reading index for %s", self.agent_name,
                exc_info=True,
            )
            return ""

    def read_note(self, name: str) -> str:
        path = self._dir / f"{_slugify(name)}.md"
        if not path.exists():
            names = ", ".join(sorted(self._note_slugs())) or "(none yet)"
            return f"no note named {name!r}; existing notes: {names}"
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            return f"error reading note {name!r}: {exc}"

    # ── write side ────────────────────────────────────────────────────

    def write_note(self, name: str, summary: str, body: str) -> str:
        """Create or update one behavior note, then rebuild the index.

        Returns a human-readable outcome string (the tool relays it)."""
        slug = _slugify(name)
        summary = " ".join(summary.split())[:MAX_SUMMARY_CHARS]
        body = body.strip()[:MAX_BODY_CHARS]
        if not summary:
            return "refused: summary must not be empty"
        existing = self._note_slugs()
        if slug not in existing and len(existing) >= MAX_NOTES:
            return (
                f"refused: {MAX_NOTES} notes already exist — consolidate "
                "(um_read a few, merge them into one updated note) before adding new ones"
            )
        self._dir.mkdir(parents=True, exist_ok=True)
        content = f"# {slug}\n\n> {summary}\n\n{body}\n" if body else f"# {slug}\n\n> {summary}\n"
        (self._dir / f"{slug}.md").write_text(content, encoding="utf-8")
        self._rebuild_index()
        verb = "updated" if slug in existing else "saved"
        return f"{verb} agent-memory note {slug!r}"

    def delete_note(self, name: str) -> str:
        slug = _slugify(name)
        path = self._dir / f"{slug}.md"
        if not path.exists():
            return f"no note named {slug!r}"
        path.unlink()
        self._rebuild_index()
        return f"deleted agent-memory note {slug!r}"

    # ── internals ─────────────────────────────────────────────────────

    def _note_slugs(self) -> set[str]:
        if not self._dir.exists():
            return set()
        return {
            p.stem for p in self._dir.glob("*.md") if p.name != INDEX_NAME
        }

    def _summary_of(self, slug: str) -> str:
        """First `> quoted` line of the note, else its first non-heading line."""
        try:
            text = (self._dir / f"{slug}.md").read_text(encoding="utf-8")
        except OSError:
            return ""
        for line in text.splitlines():
            s = line.strip()
            if s.startswith(">"):
                return s.lstrip("> ").strip()
        for line in text.splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                return s
        return ""

    def _index_lines(self) -> list[str]:
        return [
            f"- [{slug}] {self._summary_of(slug)}".rstrip()
            for slug in sorted(self._note_slugs())
        ]

    def _rebuild_index(self) -> None:
        """Derive MEMORY.md from the note files (atomic replace)."""
        lines = self._index_lines()
        tmp = self._dir / f".{INDEX_NAME}.tmp"
        tmp.write_text(
            "# Agent memory index — one line per learned behavior\n\n"
            + "\n".join(lines) + "\n",
            encoding="utf-8",
        )
        tmp.replace(self._dir / INDEX_NAME)
