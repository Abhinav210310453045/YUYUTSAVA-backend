"""Accumulate streamed agent tokens into speakable sentences.

TTS sounds natural per *sentence*, not per *token*. As the agent streams token
chunks, :class:`SentenceChunker` buffers them and yields a chunk as soon as a
sentence boundary is complete, so synthesis can start on sentence 1 while the
model is still writing sentence 2 (low latency, natural prosody).

It is intentionally simple and dependency-free: split on ``.?!`` / newlines,
with a ``min_chars`` floor so abbreviations and decimals ("3.5", "e.g.") don't
trigger a premature, choppy cut.

Time-to-first-audio matters most for the *very first* chunk — that is the delay
the user hears as "it waited for the whole message". So when ``eager_first`` is
on, the first chunk may also break on a *clause* boundary (comma / colon /
semicolon / dash) once it has enough text, getting audio out the door sooner.
Every subsequent chunk uses full sentence boundaries for natural prosody.
"""

from __future__ import annotations

import re

# A boundary is sentence punctuation (optionally followed by quotes/brackets)
# then whitespace, OR a newline.
_BOUNDARY = re.compile(r"([.!?]+[\"')\]]?\s+|\n+)")
# Clause boundaries — only used to get the FIRST utterance out fast.
_CLAUSE = re.compile(r"([,;:][\"')\]]?\s+|[—–-]+\s+|\n+)")


class SentenceChunker:
    """Buffer streamed text; emit complete sentences for TTS."""

    def __init__(
        self,
        *,
        min_chars: int = 12,
        first_min_chars: int = 18,
        eager_first: bool = True,
    ) -> None:
        self._buf = ""
        self._min_chars = min_chars
        self._first_min_chars = first_min_chars
        self._eager_first = eager_first
        self._emitted_any = False

    def feed(self, text: str) -> list[str]:
        """Add ``text``; return any newly-complete chunks (possibly empty)."""
        if not text:
            return []
        self._buf += text
        out: list[str] = []
        while True:
            chunk = self._next_chunk()
            if chunk is None:
                break
            out.append(chunk)
        return out

    def _next_chunk(self) -> str | None:
        """Pop the next speakable chunk from the buffer, or None if not ready."""
        sentence_m = _BOUNDARY.search(self._buf)

        # Before the first emission, also accept a clause boundary so the agent
        # starts talking sooner — but only if the clause is long enough to be
        # worth speaking (otherwise wait for a real sentence).
        if self._eager_first and not self._emitted_any:
            clause_m = _CLAUSE.search(self._buf)
            if clause_m is not None and (
                sentence_m is None or clause_m.end() <= sentence_m.end()
            ):
                end = clause_m.end()
                candidate = self._buf[:end].strip()
                if len(candidate) >= self._first_min_chars:
                    self._buf = self._buf[end:]
                    self._emitted_any = True
                    return candidate or None
                # too short to speak alone — fall through to sentence handling

        if sentence_m is None:
            return None
        end = sentence_m.end()
        sentence = self._buf[:end].strip()
        # Hold back ultra-short fragments (likely an abbreviation/decimal):
        # keep accumulating until the next boundary.
        if len(sentence) < self._min_chars:
            nxt = _BOUNDARY.search(self._buf, end)
            if nxt is None:
                return None
            end = nxt.end()
            sentence = self._buf[:end].strip()
        self._buf = self._buf[end:]
        if sentence:
            self._emitted_any = True
            return sentence
        return None

    def flush(self) -> str:
        """Return whatever remains (end of turn), clearing the buffer."""
        rest = self._buf.strip()
        self._buf = ""
        if rest:
            self._emitted_any = True
        return rest
