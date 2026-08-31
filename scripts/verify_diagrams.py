#!/usr/bin/env python
"""Every Mermaid diagram in the architecture docs actually renders.

Written after a hand-edited diagram silently stopped appearing. The edit added
``AskUser: HITL without interrupt()`` to the section-15 mindmap — and in a
Mermaid mindmap ``word(...)`` is *node-shape* syntax, so empty parens are a
parse error::

    Parse error on line 41: ...L without interrupt()
    Expecting 'NODE_DESCR', got 'NODE_DEND'

The failure mode is the nasty kind: the Markdown is valid, the fence is
balanced, the file looks right in an editor, and the diagram simply is not
there when rendered. A structural check on the *document* cannot see it —
only rendering can.

Renders each block through the **local** Kroki service (the one in
``docker-compose.yml``); nothing leaves the machine and nothing is billable.
Skips cleanly when Kroki is not running, so it never blocks a commit — but it
says so rather than reporting success.

Run:  .venv/bin/python scripts/verify_diagrams.py
      .venv/bin/python scripts/verify_diagrams.py --kroki http://host:8000
"""

from __future__ import annotations

import pathlib
import re
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCS = ("Architecture.md", "DAEMON_ARCHITECTURE.md")
DEFAULT_KROKI = "http://127.0.0.1:8000"

#: Characters that are structural in a Mermaid mindmap node label. Flagged
#: before rendering because the parse error Kroki returns points at a line
#: number inside the block, which is tedious to map back to the file.
_MINDMAP_TRAPS = ("(", ")", "[", "]", "{", "}")


def _kroki_base() -> str:
    if "--kroki" in sys.argv:
        return sys.argv[sys.argv.index("--kroki") + 1].rstrip("/")
    return DEFAULT_KROKI


def _kroki_up(base: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def _blocks(text: str) -> list[tuple[int, str]]:
    """``(line number of the opening fence, source)`` for each mermaid block."""
    out: list[tuple[int, str]] = []
    for m in re.finditer(r"```mermaid\n(.*?)```", text, flags=re.S):
        out.append((text[: m.start()].count("\n") + 1, m.group(1)))
    return out


def _mindmap_traps(src: str) -> list[str]:
    """Node labels containing characters a mindmap parses as shape syntax."""
    if not src.lstrip().startswith("mindmap"):
        return []
    bad = []
    for line in src.splitlines():
        label = line.strip()
        if not label or label.startswith("mindmap") or label.startswith("root"):
            continue
        if any(ch in label for ch in _MINDMAP_TRAPS):
            bad.append(label)
    return bad


def main() -> int:
    base = _kroki_base()
    if not _kroki_up(base):
        print(f"SKIP — no Kroki at {base}. Start it with:  docker compose up -d kroki")
        print("       (diagrams NOT verified — this is a skip, not a pass)")
        return 0

    failures = 0
    for name in DOCS:
        path = ROOT / name
        if not path.exists():
            print(f"{name}: MISSING")
            failures += 1
            continue
        blocks = _blocks(path.read_text(encoding="utf-8"))
        bad: list[str] = []
        for index, (line_no, src) in enumerate(blocks, 1):
            for label in _mindmap_traps(src):
                bad.append(f"    block {index} (line {line_no}): mindmap label has "
                           f"shape characters — {label!r}")
            req = urllib.request.Request(
                f"{base}/mermaid/svg", data=src.encode(),
                headers={"Content-Type": "text/plain"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    r.read()
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace").strip().replace("\n", " ")
                bad.append(f"    block {index} (line {line_no}): {detail[:200]}")
            except Exception as e:  # noqa: BLE001
                bad.append(f"    block {index} (line {line_no}): "
                           f"{type(e).__name__}: {e}")
        status = "OK" if not bad else f"{len(bad)} BROKEN"
        print(f"{name:<26} {len(blocks):>2} diagrams  {status}")
        for b in bad:
            print(b)
        failures += len(bad)

    print()
    if failures:
        print(f"{failures} diagram(s) will not render. Fix before committing.")
        return 1
    print("All diagrams render.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
