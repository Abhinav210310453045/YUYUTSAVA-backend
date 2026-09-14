#!/usr/bin/env python
"""Import every ``yuyutsava`` module on its own, each in a fresh interpreter.

**Run this after moving an import, splitting or moving a module, or editing an
``__init__.py``.**

    .venv/bin/python scripts/verify_imports.py                     # whole package
    .venv/bin/python scripts/verify_imports.py --only yuyutsava.core
    .venv/bin/python scripts/verify_imports.py --save baseline.json
    .venv/bin/python scripts/verify_imports.py --compare baseline.json

Exit code 0 = every module imports. With ``--compare``, 0 = nothing that
imported in the baseline fails now, and no module added since fails.

Why one fresh interpreter per module: a circular import only breaks when the
cycle is *entered* from a particular module. Importing the whole package in one
process hides that, because whichever module loads first leaves the rest
already initialised. Importing each module first, alone, is the only
order-independent check — and moving imports to the top of a file is exactly
the change that turns a latent cycle into a startup crash.

No model or network call is made: this only imports modules. The billable-test
switch is removed from the child environment regardless.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "yuyutsava"

# Runs inside the child. Prints one JSON line on success; any exception exits
# non-zero with the traceback on stderr.
_CHILD = """
import importlib, json, sys, time
t = time.perf_counter()
importlib.import_module(sys.argv[1])
print(json.dumps({"ms": (time.perf_counter() - t) * 1000, "modules": len(sys.modules)}))
"""


@dataclass
class Result:
    module: str
    ok: bool
    ms: float | None = None
    modules_loaded: int | None = None
    error: str | None = None


def discover_modules(only: list[str]) -> list[str]:
    """Every importable module under ``yuyutsava/``, as dotted names."""
    names = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        parts = list(path.relative_to(REPO).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        name = ".".join(parts)
        if only and not any(name == o or name.startswith(o + ".") for o in only):
            continue
        names.append(name)
    return names


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("YUYUTSAVA_ALLOW_BILLABLE", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def import_one(module: str, timeout: float, env: dict[str, str]) -> Result:
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _CHILD, module],
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return Result(module, ok=False, error=f"timed out after {timeout:.0f}s")
    if proc.returncode != 0:
        lines = [line for line in proc.stderr.strip().splitlines() if line.strip()]
        return Result(module, ok=False, error=lines[-1] if lines else f"exit {proc.returncode}")
    stats = json.loads(proc.stdout.strip().splitlines()[-1])
    return Result(module, ok=True, ms=stats["ms"], modules_loaded=stats["modules"])


def run_all(modules: list[str], timeout: float, workers: int) -> list[Result]:
    env = _child_env()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda m: import_one(m, timeout, env), modules))


def _selected(module: str, only: list[str]) -> bool:
    return not only or any(module == o or module.startswith(o + ".") for o in only)


def compare(results: list[Result], baseline_path: Path, only: list[str]) -> int:
    baseline = {
        r["module"]: r for r in json.loads(baseline_path.read_text()) if _selected(r["module"], only)
    }
    current = {r.module: r for r in results}

    broken = [m for m, r in current.items() if not r.ok and baseline.get(m, {}).get("ok", False)]
    new_failing = [m for m, r in current.items() if not r.ok and m not in baseline]
    still_failing = [m for m, r in current.items() if not r.ok and m in baseline and not baseline[m]["ok"]]
    fixed = [m for m, r in current.items() if r.ok and m in baseline and not baseline[m]["ok"]]
    removed = sorted(set(baseline) - set(current))

    for label, names in (
        ("BROKEN (imported in the baseline, fails now)", broken),
        ("NEW MODULE FAILS", new_failing),
        ("still failing (already failed in the baseline)", still_failing),
        ("fixed", fixed),
        ("no longer present", removed),
    ):
        if names:
            print(f"\n{label}: {len(names)}")
            for name in names:
                err = current[name].error if name in current else ""
                print(f"  {name}" + (f"  —  {err}" if err else ""))

    if broken or new_failing:
        print(f"\nFAIL — {len(broken) + len(new_failing)} module(s) regressed.")
        return 1
    print(f"\nOK — no regressions against {baseline_path.name} ({len(current)} modules).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--only", action="append", default=[], metavar="PREFIX",
                        help="restrict to a dotted package prefix (repeatable)")
    parser.add_argument("--save", type=Path, metavar="PATH", help="write results as a JSON baseline")
    parser.add_argument("--compare", type=Path, metavar="PATH", help="fail only on regressions against a baseline")
    parser.add_argument("--timeout", type=float, default=120.0, help="seconds per module (default 120)")
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 4)
    parser.add_argument("--slowest", type=int, default=0, metavar="N", help="also list the N slowest imports")
    args = parser.parse_args()

    modules = discover_modules(args.only)
    started = time.perf_counter()
    results = run_all(modules, args.timeout, args.workers)
    elapsed = time.perf_counter() - started

    failures = [r for r in results if not r.ok]
    print(f"{len(results)} modules imported individually in {elapsed:.0f}s — {len(failures)} failed.")

    if args.slowest:
        print(f"\nslowest {args.slowest}:")
        for r in sorted((r for r in results if r.ok), key=lambda r: -(r.ms or 0))[: args.slowest]:
            print(f"  {r.ms:7.0f} ms  {r.modules_loaded:5d} modules  {r.module}")

    if args.save:
        args.save.write_text(json.dumps([asdict(r) for r in results], indent=2))
        print(f"\nbaseline written to {args.save}")

    if args.compare:
        return compare(results, args.compare, args.only)

    for r in failures:
        print(f"  FAIL  {r.module}  —  {r.error}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
