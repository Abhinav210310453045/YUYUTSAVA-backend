#!/usr/bin/env python
"""Run every framework-contract tripwire in one go.

**Run this after any dependency change** — `uv sync`, `uv lock --upgrade`, a
bumped pin, a new machine. It is the canary for the couplings that would
otherwise change agent behaviour silently.

    .venv/bin/python scripts/verify_framework_contract.py

Exit code 0 = every contract holds. Non-zero = at least one framework coupling
has moved; the failure message names the file to fix.

Why this exists: three of the couplings it checks fail with **no exception, no
log line, and no test failure** — the agent simply behaves differently and the
change gets blamed on the model. See
docs/architecture-review/04-findings-thirdparty-coupling.md (F-T04).

Covers:
  1. deepagents filesystem prompt block is still stripped     (silent failure)
  2. general-purpose name-match override still applies        (silent failure)
  3. that override is still dispatched on spec["name"]        (silent failure)
  4. DockerSandboxBackend still satisfies the backend protocol
  5. callable `backend=` factories still accepted             (breaks 3/4 builds)
  6. declared dependency ranges still admit what is installed
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "test"))
sys.path.insert(0, str(REPO / "test" / "framework_contract"))


def _check_declared_ranges() -> None:
    """Every installed framework package satisfies its declared specifier.

    Catches the case where a ceiling is added that excludes what is actually
    installed — a pin that looks protective and silently breaks resolution on
    the next clean install.
    """
    import tomllib
    from importlib.metadata import PackageNotFoundError, version

    from packaging.requirements import Requirement

    data = tomllib.loads((REPO / "pyproject.toml").read_text())
    reqs = list(data["project"]["dependencies"])
    for extra_reqs in data["project"].get("optional-dependencies", {}).values():
        reqs += extra_reqs

    violations, uncapped = [], []
    for raw in reqs:
        try:
            req = Requirement(raw)
        except Exception:  # noqa: BLE001 — malformed entries are not our concern here
            continue
        if req.marker and not req.marker.evaluate():
            continue
        is_framework = any(
            k in req.name for k in ("langchain", "langgraph", "deepagents")
        )
        if is_framework and "<" not in str(req.specifier):
            uncapped.append(req.name)
        try:
            installed = version(req.name)
        except PackageNotFoundError:
            continue
        if req.specifier and not req.specifier.contains(installed, prereleases=True):
            violations.append(f"{req.name}: installed {installed} not in {req.specifier}")

    if violations:
        raise AssertionError(
            "declared ranges exclude installed versions:\n  " + "\n  ".join(violations)
        )
    if uncapped:
        raise AssertionError(
            "framework dependencies without an upper bound: "
            + ", ".join(sorted(uncapped))
            + "\nAn unbounded framework dep can be upgraded into a silent behaviour "
            "change. Add a ceiling in pyproject.toml."
        )


def main() -> int:
    import test_deepagents_contract as contract
    import test_filesystem_prompt_override as fs

    checks = [
        ("filesystem block stripped (silent-failure seam)", fs.test_filesystem_block_removed),
        ("lazy-discovery invariant holds", fs.test_lazy_discovery_invariant_holds),
        ("general-purpose override applies (silent-failure seam)", contract.test_general_purpose_override_contract),
        ("override still keyed on spec['name'] (silent-failure seam)", contract.test_general_purpose_auto_add_still_name_keyed),
        ("docker backend satisfies protocol", contract.test_docker_backend_satisfies_protocol),
        ("backend= accepts a protocol instance", contract.test_backend_accepts_protocol_instance),
        ("no build path reintroduced a backend factory", contract.test_no_build_path_passes_a_backend_factory),
        ("declared dependency ranges are sane", _check_declared_ranges),
    ]

    failures: list[tuple[str, str]] = []
    for label, fn in checks:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 — report every failure, never stop at the first
            failures.append((label, str(exc)))
            print(f"  FAIL  {label}")
            if not isinstance(exc, AssertionError):
                traceback.print_exc()
        else:
            print(f"  ok    {label}")

    print()
    if failures:
        print(f"{len(failures)} of {len(checks)} framework contracts BROKEN:\n")
        for label, msg in failures:
            print(f"--- {label}\n{msg}\n")
        print(
            "A dependency has moved under the code. Do not upgrade past this point "
            "until each item above is resolved."
        )
        return 1

    print(f"OK — all {len(checks)} framework contracts hold.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
