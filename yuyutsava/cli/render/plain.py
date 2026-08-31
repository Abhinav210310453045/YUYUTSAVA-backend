"""Plain ANSI renderer for the chat REPL (the non-rich fallback).

``ChatRenderer`` + ``RingEntry`` moved verbatim from ``chat_repl.py`` so
the rich renderer (``render/renderer.py``) can subclass without an
import cycle. Behavior is unchanged: terse ANSI lines to stderr, prose
to stdout through ``TokenSmoother``. The ANSI palette lives here and is
re-imported by ``chat_repl.py``.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yuyutsava.cli.stream_smoother import TokenSmoother
from yuyutsava.core.streaming import StreamEvent

_CYAN = "\033[36m"
_DIM = "\033[2m"
_YELLOW = "\033[33m"
_GREEN = "\033[32m"
_RED = "\033[31m"
_RESET = "\033[0m"


@dataclass
class RingEntry:
    """One captured event with its full untruncated payload.

    Stored in ``ChatRenderer._ring`` so ``/last`` and ``/expand <n>`` can
    surface payloads the truncated preview hid. Per-entry body is capped
    by ``YUYUTSAVA_REPL_RING_ENTRY_KB`` (default 100 KB) to keep huge tool
    outputs from blowing up REPL memory.
    """
    index: int
    kind: str           # "tool_call" | "tool_result"
    name: str           # tool name
    full: str           # pretty-printed full body (already capped)
    truncated: bool     # True if the body was clipped to the per-entry cap


class ChatRenderer:
    """Print StreamEvents in a Claude-Code-style minimal layout.

    Two display modes share the same ring buffer and slash commands:

    * Non-verbose — one terse line per tool call (``· name: <summary>
      [zone] [#N]``). Successful tool results are suppressed; errors
      surface as a single red ``↳ name ✗ <msg>`` line.
    * Verbose — multi-line pretty-printed tool calls and results, JSON
      bodies indented two levels, truncation only at the per-entry cap.

    ``write_todos`` always renders as a checklist regardless of mode.
    Use ``/expand <n>`` to pull the full payload of any ``[#N]`` entry.
    """

    def __init__(self, *, verbose: bool, workspace: Path | None = None) -> None:
        self._verbose = verbose
        self._workspace = workspace
        self._in_ai_stream = False
        try:
            ring_size = max(1, int(os.environ.get("YUYUTSAVA_REPL_RING", "50")))
        except ValueError:
            ring_size = 50
        try:
            self._entry_cap = max(1024, int(os.environ.get("YUYUTSAVA_REPL_RING_ENTRY_KB", "100")) * 1024)
        except ValueError:
            self._entry_cap = 100 * 1024
        self._ring: deque[RingEntry] = deque(maxlen=ring_size)
        self._next_index = 1
        self._smoother = self._make_smoother()

    def begin_turn(self) -> None:
        """Turn-start hook. The rich subclass starts its spinner here."""

    @contextlib.contextmanager
    def pause(self):
        """No-op display pause. The rich subclass stops its Live region."""
        yield

    @staticmethod
    def _make_smoother() -> TokenSmoother | None:
        """Build a token smoother for prose, or None to print directly.

        Smoothing is for interactive terminals only: when stdout is not a TTY
        (piped output, tests) or the user disabled it via
        ``YUYUTSAVA_REPL_SMOOTH=0``, return None so the render path is
        byte-for-byte identical to printing chunks as they arrive.
        """
        flag = os.environ.get("YUYUTSAVA_REPL_SMOOTH", "1").strip().lower()
        if flag in ("0", "false", "no", "off"):
            return None
        try:
            if not sys.stdout.isatty():
                return None
        except (ValueError, AttributeError):
            return None
        try:
            base_cps = float(os.environ.get("YUYUTSAVA_REPL_SMOOTH_CPS", "180"))
        except ValueError:
            base_cps = 180.0
        return TokenSmoother(
            sys.stdout.write,
            flush=sys.stdout.flush,
            base_cps=base_cps,
        )

    async def render(self, ev: StreamEvent) -> None:
        if ev.kind == "token":
            if not self._in_ai_stream:
                # Open the AI line with a small chip; no big separator block.
                print(f"\n{_CYAN}🤖{_RESET}  ", end="", flush=True)
                self._in_ai_stream = True
            text = ev.data.get("text", "")
            if self._smoother is not None:
                self._smoother.feed(text)
            else:
                print(text, end="", flush=True)
            return

        # Any non-token event closes the AI stream visually. Flush the
        # smoother first so buffered prose lands before the tool/log line.
        if self._smoother is not None:
            await self._smoother.drain()
        if self._in_ai_stream:
            print(flush=True)
            self._in_ai_stream = False

        if ev.kind == "tool_call":
            name = ev.data.get("name", "?")
            args = ev.data.get("args", {})
            idx = self._push_ring("tool_call", name, self._pretty(args))
            if name == "write_todos":
                self._render_todos(args, idx)
                return
            if self._verbose:
                self._render_tool_call_verbose(name, args, idx)
            else:
                self._render_tool_call_compact(name, args, idx)
            return

        if ev.kind == "tool_result":
            name = ev.data.get("name", "tool")
            body = ev.data.get("preview", "") or ""
            full = ev.data.get("full", body) or body
            idx = self._push_ring("tool_result", name, full)
            # The write_todos call line already reflects the new state.
            if name == "write_todos":
                return
            is_err = self._looks_like_error(full or body)
            if self._verbose:
                self._render_tool_result_verbose(name, full or body, idx, is_err)
            elif is_err:
                self._render_tool_result_compact_error(name, full or body, idx)
            # else: suppress on success in non-verbose
            return

        if ev.kind == "log":
            text = ev.data.get("text", "")
            if text:
                print(f"{_YELLOW}{text}{_RESET}", file=sys.stderr, flush=True)
            return

        if ev.kind == "final":
            # The token stream above already covered the prose. Just newline.
            print(flush=True)

    async def end_of_turn(self) -> None:
        """Force-close any dangling streaming line."""
        if self._smoother is not None:
            await self._smoother.drain()
        if self._in_ai_stream:
            print(flush=True)
            self._in_ai_stream = False

    # ------------------------------------------------------------------
    # Ring buffer + slash command helpers
    # ------------------------------------------------------------------

    def _push_ring(self, kind: str, name: str, full: str) -> int:
        truncated = False
        if len(full) > self._entry_cap:
            full = full[: self._entry_cap] + f"\n…[truncated to {self._entry_cap // 1024}KB]"
            truncated = True
        idx = self._next_index
        self._next_index += 1
        self._ring.append(RingEntry(index=idx, kind=kind, name=name, full=full, truncated=truncated))
        return idx

    def _find(self, idx: int) -> RingEntry | None:
        for e in self._ring:
            if e.index == idx:
                return e
        return None

    def print_ring(self) -> None:
        if not self._ring:
            print(f"{_DIM}(ring empty){_RESET}", file=sys.stderr)
            return
        print(f"{_CYAN}ring (most recent last):{_RESET}", file=sys.stderr)
        for e in self._ring:
            summary = e.full.replace("\n", " ⏎ ")
            if len(summary) > 100:
                summary = summary[:100] + "…"
            tag = "·" if e.kind == "tool_call" else "↳"
            print(f"  {_DIM}[#{e.index}] {tag} {e.name}: {summary}{_RESET}", file=sys.stderr)

    def print_entry(self, idx: int) -> None:
        e = self._find(idx)
        if e is None:
            print(f"{_DIM}no ring entry #{idx} (try /ring){_RESET}", file=sys.stderr)
            return
        header = f"[#{e.index}] {e.kind} {e.name}"
        if e.truncated:
            header += " (truncated)"
        print(f"{_CYAN}{header}{_RESET}", file=sys.stderr)
        print(e.full, file=sys.stderr)

    def print_last(self, k: int = 1) -> None:
        if not self._ring:
            print(f"{_DIM}(ring empty){_RESET}", file=sys.stderr)
            return
        k = max(1, k)
        for e in list(self._ring)[-k:]:
            self.print_entry(e.index)

    @staticmethod
    def _pretty(args: Any) -> str:
        try:
            if isinstance(args, (dict, list)):
                return json.dumps(args, indent=2, default=str)
        except Exception:
            pass
        return str(args)

    @staticmethod
    def _fmt_args(args: Any, *, limit: int) -> str:
        try:
            if isinstance(args, dict):
                items = []
                for k, v in args.items():
                    sval = str(v)
                    if len(sval) > 40:
                        sval = sval[:40] + "…"
                    items.append(f"{k}={sval}")
                out = ", ".join(items)
            else:
                out = str(args)
        except Exception:
            out = "…"
        if len(out) > limit:
            out = out[:limit] + "…"
        return out

    # ------------------------------------------------------------------
    # Mode-specific renderers
    # ------------------------------------------------------------------

    def _render_tool_call_compact(self, name: str, args: Any, idx: int) -> None:
        summary = self._compact_summary(args)
        zone_chip = self._zone_chip(name, args)
        head = f"  {_DIM}·{_RESET} {_CYAN}{name}{_RESET}"
        body = f"{_DIM}: {summary}{_RESET}" if summary else ""
        tail = f"{_DIM}  [#{idx}]{_RESET}"
        print(f"{head}{body}{zone_chip}{tail}", file=sys.stderr, flush=True)

    def _render_tool_call_verbose(self, name: str, args: Any, idx: int) -> None:
        print(
            f"\n  {_DIM}·{_RESET} {_CYAN}{name}{_RESET}  {_DIM}[#{idx}]{_RESET}",
            file=sys.stderr,
        )
        if not isinstance(args, dict) or not args:
            return
        for k, v in args.items():
            sval = self._format_arg_value(v)
            if "\n" in sval:
                print(f"    {_DIM}{k}:{_RESET}", file=sys.stderr)
                for line in sval.splitlines():
                    print(f"      {line}", file=sys.stderr)
            else:
                if len(sval) > 200:
                    sval = sval[:200] + "…"
                print(f"    {_DIM}{k}:{_RESET} {sval}", file=sys.stderr)

    def _render_tool_result_verbose(
        self, name: str, body: str, idx: int, is_err: bool
    ) -> None:
        status = f"{_RED}✗ error{_RESET}" if is_err else f"{_GREEN}← ok{_RESET}"
        print(
            f"  {_DIM}↳{_RESET} {_CYAN}{name}{_RESET} {status}  {_DIM}[#{idx}]{_RESET}",
            file=sys.stderr,
        )
        pretty = self._pretty_body(body)
        if len(pretty) > self._entry_cap:
            pretty = (
                pretty[: self._entry_cap]
                + f"\n…[truncated; /expand {idx} for full]"
            )
        for line in pretty.splitlines():
            print(f"      {line}", file=sys.stderr)

    def _render_tool_result_compact_error(
        self, name: str, body: str, idx: int
    ) -> None:
        short = self._error_message(body)
        if short:
            tail = f"{_DIM}  [#{idx}]{_RESET}"
            print(
                f"  {_RED}↳ {name} ✗ {short}{_RESET}{tail}",
                file=sys.stderr,
                flush=True,
            )
        else:
            print(
                f"  {_RED}↳ {name} ✗ error{_RESET}{_DIM}  [#{idx}]{_RESET}",
                file=sys.stderr,
                flush=True,
            )

    def _render_todos(self, args: Any, idx: int) -> None:
        todos = args.get("todos") if isinstance(args, dict) else None
        if not isinstance(todos, list) or not todos:
            print(
                f"  {_DIM}· write_todos  [#{idx}]{_RESET}",
                file=sys.stderr,
                flush=True,
            )
            return
        print(
            f"\n  {_CYAN}TODO:{_RESET}  {_DIM}[#{idx}]{_RESET}",
            file=sys.stderr,
        )
        for t in todos:
            if not isinstance(t, dict):
                continue
            status = str(t.get("status", "")).lower()
            content = str(t.get("content", "")).strip()
            symbol = self._todo_symbol(status)
            if status == "completed":
                colour = _DIM
            elif status == "in_progress":
                colour = _GREEN
            else:
                colour = ""
            reset = _RESET if colour else ""
            print(
                f"    {colour}{symbol} {content}{reset}",
                file=sys.stderr,
            )
        print(file=sys.stderr)

    @staticmethod
    def _todo_symbol(status: str) -> str:
        if status == "completed":
            return "[✓]"
        if status == "in_progress":
            return "[▶]"
        return "[ ]"

    # ------------------------------------------------------------------
    # Format helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _compact_summary(args: Any) -> str:
        if not isinstance(args, dict):
            s = str(args)
            return s if len(s) <= 80 else s[:80] + "…"
        reason = args.get("reason")
        if isinstance(reason, str) and reason.strip():
            s = reason.strip()
            return s if len(s) <= 80 else s[:80] + "…"
        for key in ("path", "file_path", "target", "directory"):
            v = args.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        paths = args.get("paths")
        if isinstance(paths, list) and paths:
            head = ", ".join(str(p) for p in paths[:2])
            return head + ("…" if len(paths) > 2 else "")
        for key in ("query", "pattern", "name", "command", "question"):
            v = args.get(key)
            if isinstance(v, str) and v.strip():
                s = v.strip()
                if len(s) > 80:
                    s = s[:80] + "…"
                return f'{key}="{s}"'
        # Fallback: terse key=value summary.
        items: list[str] = []
        for k, v in args.items():
            sval = str(v)
            if len(sval) > 40:
                sval = sval[:40] + "…"
            items.append(f"{k}={sval}")
        out = ", ".join(items)
        return out if len(out) <= 80 else out[:80] + "…"

    def _zone_chip(self, name: str, args: Any) -> str:
        if not (name.startswith("tr_") and isinstance(args, dict)):
            return ""
        path: str | None = None
        for k in ("path", "file_path", "target", "directory"):
            v = args.get(k)
            if isinstance(v, str) and v.strip():
                path = v.strip()
                break
        if path is None:
            paths = args.get("paths")
            if isinstance(paths, list) and paths:
                p0 = paths[0]
                if isinstance(p0, str) and p0.strip():
                    path = p0.strip()
        if not path:
            return ""
        zone = self._classify_zone(path)
        if not zone:
            return ""
        colour = _YELLOW if zone == "external" else _DIM
        return f"  {colour}[{zone}]{_RESET}"

    def _classify_zone(self, path: str) -> str:
        from yuyutsava.platform import host_profile

        if self._workspace is not None and path.startswith(str(self._workspace)):
            return "workspace"
        if path.startswith(host_profile().temp_zone_prefixes()):
            return "sandbox"
        if os.path.isabs(path):
            return "external"
        return ""

    @staticmethod
    def _format_arg_value(v: Any) -> str:
        if isinstance(v, str):
            stripped = v.strip()
            if stripped.startswith(("{", "[")):
                try:
                    return json.dumps(json.loads(stripped), indent=2, default=str)
                except Exception:
                    pass
            return v
        if isinstance(v, (dict, list)):
            try:
                return json.dumps(v, indent=2, default=str)
            except Exception:
                return str(v)
        return str(v)

    @staticmethod
    def _pretty_body(body: str) -> str:
        if not isinstance(body, str):
            return str(body)
        stripped = body.strip()
        if stripped.startswith(("{", "[")):
            try:
                return json.dumps(json.loads(stripped), indent=2, default=str)
            except Exception:
                pass
        return body

    @staticmethod
    def _looks_like_error(body: str) -> bool:
        if not isinstance(body, str) or not body:
            return False
        stripped = body.strip()
        if stripped.startswith("{"):
            try:
                obj = json.loads(stripped)
                if isinstance(obj, dict):
                    status = str(obj.get("status", "")).lower()
                    if status in ("error", "rejected", "fail", "failed"):
                        return True
                    err = obj.get("error")
                    if err:
                        return True
            except Exception:
                pass
        head = stripped[:200].lower()
        markers = ("error:", "exception:", "traceback", "failed:", "✗")
        return any(m in head for m in markers)

    @staticmethod
    def _error_message(body: str) -> str:
        if not isinstance(body, str):
            return ""
        stripped = body.strip()
        if stripped.startswith("{"):
            try:
                obj = json.loads(stripped)
                if isinstance(obj, dict):
                    err = obj.get("error")
                    if isinstance(err, dict):
                        msg = err.get("message") or err.get("code")
                        if msg:
                            return str(msg)[:120]
                    elif isinstance(err, str) and err:
                        return err[:120]
                    msg = obj.get("message")
                    if isinstance(msg, str) and msg:
                        return msg[:120]
            except Exception:
                pass
        first = stripped.splitlines()[0] if stripped else ""
        return first[:120]
