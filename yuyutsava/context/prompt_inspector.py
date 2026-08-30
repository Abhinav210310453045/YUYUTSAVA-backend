"""Prompt inspector: see the EXACT context sent to the model, per step.

Pure observability — never mutates state, returns ``None`` from every hook, and
does nothing unless ``YUYUTSAVA_DEBUG_PROMPT`` is truthy. It runs as the last
``before_model`` hook so it reports the message list *after* offload and
compaction have done their work — i.e. what the LLM actually receives.

Phase 4 step 4.8, thirteenth migration: a plain
:class:`~yuyutsava.policy.base.Policy` rather than an ``AgentMiddleware``
subclass. Nothing about the report changed.

This is the answer to "is the content really reduced, or just copied into a
table too?". For each model call it logs one line per message with its byte
size and flags offloaded tool digests, plus a total. You will see that a
``ws_*`` tool message in-context is the ~1 KB digest (``offloaded=true``), not
the multi-KB raw body — the full body lives only in the ``artifacts`` table.

Enable::

    YUYUTSAVA_DEBUG_PROMPT=1            # one summary block per model call (INFO)
    YUYUTSAVA_DEBUG_PROMPT_DUMP=/path   # also dump full JSON of each call there

The CLI ``chat`` REPL logs to the same terminal you type in; the daemon logs to
its stdout — either way it is real-time. For the richest view (full prompt +
tool I/O) enable Langfuse tracing instead (LANGFUSE_* env, the docker-compose
``langfuse`` service).
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from yuyutsava.policy.base import Policy
from yuyutsava.policy.types import Directive, Turn

logger = logging.getLogger("yuyutsava.context.prompt_inspector")


def _enabled() -> bool:
    return os.environ.get("YUYUTSAVA_DEBUG_PROMPT", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _msg_size(content: Any) -> int:
    if isinstance(content, str):
        return len(content)
    try:
        return len(json.dumps(content))
    except Exception:
        return len(str(content))


def _is_offloaded_digest(content: Any) -> bool:
    return isinstance(content, str) and content.lstrip().startswith('{"offloaded": true')


class PromptInspectorPolicy(Policy):
    """Log the assembled per-step context. No-op unless YUYUTSAVA_DEBUG_PROMPT."""

    name = "PromptInspectorPolicy"

    def __init__(self, role: str = "agent") -> None:
        super().__init__()
        self._role = role
        self._call = 0

    async def before_model(self, turn: Turn) -> Directive | None:
        self._report(list(turn.messages))
        return None

    def _report(self, messages: list[Any]) -> None:
        if not _enabled():
            return
        self._call += 1
        total = 0
        offloaded = 0
        lines: list[str] = []
        for i, m in enumerate(messages):
            mtype = getattr(m, "type", type(m).__name__)
            name = getattr(m, "name", None)
            content = getattr(m, "content", "")
            size = _msg_size(content)
            total += size
            tag = ""
            if _is_offloaded_digest(content):
                offloaded += 1
                tag = " [OFFLOADED DIGEST]"
            label = f"{mtype}" + (f":{name}" if name else "")
            preview = content if isinstance(content, str) else str(content)
            preview = preview[:80].replace("\n", " ")
            lines.append(f"    [{i:>2}] {label:<22} {size:>7,}c{tag}  {preview!r}")

        approx_tokens = total // 4  # rough 4 chars/token
        header = (
            f"\n┌─ PROMPT INSPECT [{self._role}] call#{self._call} ─ "
            f"{len(messages)} msgs · {total:,} chars · ~{approx_tokens:,} tok · "
            f"{offloaded} offloaded digest(s) in-context"
        )
        logger.info("%s\n%s\n└─", header, "\n".join(lines))

        dump_path = os.environ.get("YUYUTSAVA_DEBUG_PROMPT_DUMP", "").strip()
        if dump_path:
            try:
                record = {
                    "ts": time.time(),
                    "role": self._role,
                    "call": self._call,
                    "total_chars": total,
                    "messages": [
                        {
                            "i": i,
                            "type": getattr(m, "type", type(m).__name__),
                            "name": getattr(m, "name", None),
                            "size": _msg_size(getattr(m, "content", "")),
                            "content": getattr(m, "content", ""),
                        }
                        for i, m in enumerate(messages)
                    ],
                }
                with open(dump_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record) + "\n")
            except Exception:
                logger.warning("prompt inspect: dump to %s failed", dump_path, exc_info=True)
