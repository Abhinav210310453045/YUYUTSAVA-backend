#!/usr/bin/env python
"""Fail when a ratcheted lint count changes without the baseline moving with it.

    .venv/bin/python scripts/check_lint_ratchet.py            # check (CI runs this)
    .venv/bin/python scripts/check_lint_ratchet.py --update   # record a clean-up

Some rules in docs/code-review/v0.1.0/01-standards.md have too many existing
violations to gate at zero: imports inside functions, complexity, argument
counts, blind excepts, try/except/pass, and print. Each is counted per file and
per rule, and compared with ``scripts/lint_ratchet.json``.

Exit code 0 = every count equals its baseline. Non-zero when either:

* a count **grew**, or a file outside the baseline has a violation — fix it, or
  for a justified exception add ``# noqa: <RULE>  <reason>``;
* a count **fell** and the baseline was not updated — run ``--update`` in the
  same commit as the clean-up, so the ratchet cannot slip back.

``--update`` rewrites the baseline and refuses to record an increase. ruff is
run at the version pinned by ``required-version`` in ``pyproject.toml``.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tomllib
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BASELINE = REPO / "scripts" / "lint_ratchet.json"
RULES = ("BLE001", "C901", "PLC0415", "PLR0913", "S110", "T201")
PATHS = ("yuyutsava", "scripts")

Counts = dict[str, dict[str, int]]


def ruff_command() -> list[str]:
    """``uvx ruff@<pinned>`` when uv is available, else ``ruff`` from PATH."""
    config = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    pinned = config["tool"]["ruff"]["required-version"].lstrip("=")
    if shutil.which("uvx"):
        return ["uvx", f"ruff@{pinned}"]
    return ["ruff"]


def current_counts() -> Counts:
    proc = subprocess.run(
        [*ruff_command(), "check", *PATHS, "--select", ",".join(RULES),
         "--output-format", "json", "--exit-zero", "--no-cache"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    counts: dict[str, Counter[str]] = {}
    for violation in json.loads(proc.stdout):
        path = Path(violation["filename"]).resolve().relative_to(REPO).as_posix()
        counts.setdefault(path, Counter())[violation["code"]] += 1
    return {path: dict(sorted(rules.items())) for path, rules in sorted(counts.items())}


def compare(baseline: Counts, current: Counts) -> tuple[list[str], list[str]]:
    """(grown, fallen) as ``path: RULE before -> after`` lines."""
    grown, fallen = [], []
    for path in sorted(set(baseline) | set(current)):
        for rule in sorted(set(baseline.get(path, {})) | set(current.get(path, {}))):
            before = baseline.get(path, {}).get(rule, 0)
            after = current.get(path, {}).get(rule, 0)
            if after > before:
                grown.append(f"{path}: {rule} {before} -> {after}")
            elif after < before:
                fallen.append(f"{path}: {rule} {before} -> {after}")
    return grown, fallen


def totals(counts: Counts) -> str:
    by_rule: Counter[str] = Counter()
    for rules in counts.values():
        by_rule.update(rules)
    return ", ".join(f"{rule} {by_rule[rule]}" for rule in RULES)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--update", action="store_true", help="rewrite the baseline; refuses increases")
    args = parser.parse_args()

    baseline: Counts = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else {}
    current = current_counts()
    grown, fallen = compare(baseline, current)

    if grown and (baseline or not args.update):
        print(f"{len(grown)} ratcheted lint count(s) grew:")
        for line in grown:
            print(f"  {line}")
        print("\nFix the new violation, or mark a justified exception with `# noqa: <RULE>  <reason>`.")
        return 1

    if args.update:
        BASELINE.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
        print(f"baseline written — {totals(current)}")
        return 0

    if fallen:
        print(f"{len(fallen)} ratcheted lint count(s) fell — lock them in with --update:")
        for line in fallen:
            print(f"  {line}")
        return 1

    print(f"OK — ratcheted lint counts match the baseline ({totals(current)}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
