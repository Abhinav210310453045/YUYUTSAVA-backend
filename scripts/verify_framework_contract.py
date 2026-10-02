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
F-T04 in docs/code-review/v0.1.0/10-legacy-ledger.md.

Covers:
  1. deepagents filesystem prompt block is still stripped     (silent failure)
  2. general-purpose name-match override still applies        (silent failure)
  3. that override is still dispatched on spec["name"]        (silent failure)
  4. DockerSandboxBackend still satisfies the backend protocol
  5. callable `backend=` factories still accepted             (breaks 3/4 builds)
  6. the Gemini chat classes still take the reply-bounding kwargs (silent failure)
  7. the empty-turn retry's node still leads to the model
  8. the MCP SDK still names its fields one of the ways the adapter reads (silent failure)
  9. langgraph-api's run_server still declares every keyword the host passes (silent failure)
 10. langgraph-sdk still declares every keyword the watchers pass
 11. declared dependency ranges still admit what is installed, and every
     coupled dependency has a ceiling
 12. every chat SDK still accepts portable history written by any other   (silent failure)
 13. a first-chunk selection stamp still survives stream merging          (silent failure)
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# The repo root, so the `test` package imports when this runs as a script.
sys.path.insert(0, str(REPO))


def _check_declared_ranges() -> None:
    """Every installed framework package satisfies its declared specifier.

    Catches the case where a ceiling is added that excludes what is actually
    installed — a pin that looks protective and silently breaks resolution on
    the next clean install.
    """
    import tomllib
    from importlib.metadata import PackageNotFoundError, version

    from packaging.requirements import Requirement

    from yuyutsava.config.dependencies import (  # noqa: PLC0415 — resolved after the checkout joins sys.path, above
        is_coupled,
    )

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
        # The coupled set is the one the daemon checks its install against
        # (yuyutsava/config/dependencies.COUPLED); each needs a ceiling.
        if is_coupled(req.name) and "<" not in str(req.specifier):
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
            "coupled dependencies without an upper bound: "
            + ", ".join(sorted(uncapped))
            + "\nAn unbounded coupled dep can be upgraded into a silent behaviour "
            "change. Add a ceiling in pyproject.toml."
        )


def main() -> int:
    from test.framework_contract import (  # noqa: PLC0415 — `test` joins sys.path at run time, above
        test_cross_provider_history_contract as cross_provider,
    )
    from test.framework_contract import test_deepagents_contract as contract
    from test.framework_contract import (  # noqa: PLC0415 — `test` joins sys.path at run time, above
        test_gemini_output_contract as gemini,
    )
    from test.framework_contract import (  # noqa: PLC0415 — `test` joins sys.path at run time, above
        test_langgraph_api_contract as langgraph_api,
    )
    from test.framework_contract import (  # noqa: PLC0415 — `test` joins sys.path at run time, above
        test_mcp_sdk_contract as mcp_sdk,
    )
    from test.framework_contract import (  # noqa: PLC0415 — `test` joins sys.path at run time, above
        test_retry_node_contract as retry_node,
    )
    from test.policy import test_filesystem_prompt_override as fs

    checks = [
        ("filesystem block stripped (silent-failure seam)", fs.test_filesystem_block_removed),
        ("lazy-discovery invariant holds", fs.test_lazy_discovery_invariant_holds),
        (
            "general-purpose override applies (silent-failure seam)",
            contract.test_general_purpose_override_contract,
        ),
        (
            "override still keyed on spec['name'] (silent-failure seam)",
            contract.test_general_purpose_auto_add_still_name_keyed,
        ),
        ("docker backend satisfies protocol", contract.test_docker_backend_satisfies_protocol),
        ("backend= accepts a protocol instance", contract.test_backend_accepts_protocol_instance),
        (
            "no build path reintroduced a backend factory",
            contract.test_no_build_path_passes_a_backend_factory,
        ),
        (
            "Gemini chat classes take the reply-bounding kwargs (silent-failure seam)",
            gemini.test_gemini_chat_classes_accept_the_output_kwargs,
        ),
        (
            "the empty-turn retry's node leads to the model",
            retry_node.test_the_retry_node_leads_to_the_model,
        ),
        (
            "the MCP SDK's field names are ones the adapter reads (silent-failure seam)",
            mcp_sdk.test_the_mcp_models_declare_a_spelling_the_adapter_reads,
        ),
        (
            "langgraph-api's run_server takes the host's keywords (silent-failure seam)",
            langgraph_api.test_run_server_declares_every_keyword_the_host_passes,
        ),
        (
            "langgraph-sdk takes the watchers' keywords",
            langgraph_api.test_the_sdk_declares_every_keyword_the_watchers_pass,
        ),
        (
            "portable history crosses every chat SDK (silent-failure seam)",
            cross_provider.test_portable_history_is_accepted_by_every_builder,
        ),
        (
            "a first-chunk selection stamp survives stream merging (silent-failure seam)",
            cross_provider.test_a_first_chunk_stamp_survives_stream_merging,
        ),
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
