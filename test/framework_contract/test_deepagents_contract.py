"""Tripwires for the places YUYUTSAVA depends on ``deepagents`` internals.

Three couplings in this codebase reach past the library's documented surface.
Two of them fail **silently** on upgrade — no exception, no log line, just
different agent behaviour discovered days later and blamed on the model. These
tests convert those silent failures into loud ones.

Each test pins the *contract*, not the mechanism, and each is cheap: no graph is
built, no model is called, nothing hits the network.

  seam 1  filesystem prompt block      -> test/test_filesystem_prompt_override.py
          (lives there because asserting it needs a rendered prompt)
  seam 2  general-purpose name match   -> test_general_purpose_override_contract
  seam 3  sandbox backend protocol     -> test_docker_backend_satisfies_protocol
  seam 4  backend factory deprecation  -> test_backend_factory_still_accepted

Run directly:  .venv/bin/python test/framework_contract/test_deepagents_contract.py
Or via pytest: pytest test/framework_contract/

See docs/architecture/review/04-findings-thirdparty-coupling.md (F-T04).
"""

from __future__ import annotations

import inspect


# ---------------------------------------------------------------------------
# Seam 2 — the general-purpose name-match override
# ---------------------------------------------------------------------------


def test_general_purpose_override_contract() -> None:
    """Our GeneralPurposeAgent must keep overriding the library's built-in default.

    ``deepagents.graph`` auto-adds its own permissive general-purpose subagent
    unless a caller-supplied spec already carries that exact name::

        if ... and not any(spec["name"] == GENERAL_PURPOSE_SUBAGENT["name"]
                           for spec in inline_subagents):
            # auto-add the built-in default

    So the override is a **string collision**, and the string belongs to the
    library. If it ever changes, our tighter spec silently stops applying and
    the library's broader default takes over instead — a capability-scope
    regression with no error to notice.

    This asserts the collision still holds. It is the whole contract.
    """
    from deepagents.middleware.subagents import GENERAL_PURPOSE_SUBAGENT

    from yuyutsava.agents.general_purpose.agent import GeneralPurposeAgent

    library_name = GENERAL_PURPOSE_SUBAGENT["name"]
    ours = GeneralPurposeAgent.__dict__["name"]
    # ``name`` is a read-only property on the class; read its constant return.
    our_name = ours.fget(None) if isinstance(ours, property) else ours

    assert our_name == library_name, (
        f"general-purpose override BROKEN: deepagents now names its built-in default "
        f"{library_name!r}, our subagent is {our_name!r}. The name-match override no "
        f"longer applies, so the library's permissive default subagent is being used "
        f"instead of our restricted one. Fix: rename ours to match, in "
        f"yuyutsava/agents/general_purpose/agent.py."
    )


def test_general_purpose_auto_add_still_name_keyed() -> None:
    """The override must remain keyed on ``name``, not on some new field.

    ``test_general_purpose_override_contract`` proves our string matches theirs.
    It cannot prove the library still *dispatches* on that string. If deepagents
    switches to matching on an id, a type, or a profile flag, our name would
    still match while the override quietly stopped working.
    """
    from deepagents import graph as dg

    src = inspect.getsource(dg)
    assert 'spec["name"] == GENERAL_PURPOSE_SUBAGENT["name"]' in src, (
        "deepagents no longer selects its built-in general-purpose subagent by "
        "comparing spec['name']. Our override in core/engine.py relies on that "
        "exact mechanism. Re-read deepagents.graph and confirm our "
        "GeneralPurposeAgent still displaces the built-in default."
    )


# ---------------------------------------------------------------------------
# Seam 3 — the sandbox backend protocol
# ---------------------------------------------------------------------------


def test_docker_backend_satisfies_protocol() -> None:
    """``DockerSandboxBackend`` must implement every SandboxBackendProtocol member.

    We subclass ``deepagents.backends.sandbox.BaseSandbox`` and are handed to
    ``create_deep_agent(backend=...)``. A member added to the protocol upstream
    surfaces as an ``AttributeError`` deep inside a tool call — at agent runtime,
    on a user's turn, rather than at construction.
    """
    from yuyutsava.core.docker_sandbox_backend import DockerSandboxBackend

    # Check ``__abstractmethods__``, NOT ``hasattr``. DockerSandboxBackend
    # inherits from the protocol itself (MRO: DockerSandboxBackend ->
    # BaseSandbox -> SandboxBackendProtocol -> BackendProtocol -> ABC), so
    # every protocol member is reachable via ``hasattr`` whether or not anyone
    # implements it — a hasattr-based check here is vacuous and always passes.
    #
    # ``__abstractmethods__`` is the set Python itself computes at class
    # creation: abstract members with no concrete implementation anywhere in
    # the MRO. Non-empty means ``DockerSandboxBackend(...)`` raises TypeError,
    # which is exactly the failure we want to catch — and it costs no I/O.
    unimplemented = set(DockerSandboxBackend.__abstractmethods__)

    assert not unimplemented, (
        f"DockerSandboxBackend cannot be instantiated: {len(unimplemented)} abstract "
        f"member(s) have no implementation: {sorted(unimplemented)}. deepagents "
        f"added these to the backend protocol and neither BaseSandbox nor we "
        f"implement them. Docker execution mode is broken. Implement them in "
        f"yuyutsava/core/docker_sandbox_backend.py."
    )


# ---------------------------------------------------------------------------
# Seam 4 — the backend-factory deprecation (a scheduled, dated break)
# ---------------------------------------------------------------------------


def test_backend_accepts_protocol_instance() -> None:
    """Every build path passes a ``BackendProtocol`` *instance* as ``backend=``.

    History: three of four paths used to pass a callable factory, which
    deepagents deprecated and removes in 0.7.0 — that would have stopped every
    non-Docker agent from building. ``core/engine.py`` now passes instances
    (``_local_shell_backend`` for CLI/tinker/orchestrator, ``DockerSandboxBackend``
    for Docker), which is valid on both 0.6.x and 0.7+.

    This pins what we now depend on: the instance form. It is the *surviving*
    half of the union, so it is the half worth guarding.
    """
    from deepagents.graph import create_deep_agent

    annotation = str(inspect.signature(create_deep_agent).parameters["backend"].annotation)

    assert "BackendProtocol" in annotation, (
        f"deepagents `backend=` no longer accepts a BackendProtocol instance "
        f"(annotation is now {annotation!r}). All four agent build paths in "
        f"core/engine.py pass instances. Re-read the deepagents backend docs "
        f"before changing anything."
    )


def test_no_build_path_passes_a_backend_factory() -> None:
    """Guard the 0.7 migration against regression.

    The callable-factory form still works on 0.6.x, so nothing stops a future
    edit from reintroducing it — and it would keep working locally right up
    until the deepagents upgrade that breaks production. This asserts the
    migration stays migrated.
    """
    import re
    from pathlib import Path

    engine = Path(__file__).resolve().parents[2] / "yuyutsava" / "core" / "engine.py"
    src = engine.read_text(encoding="utf-8")

    # A factory is a def whose body returns a backend and which is handed to
    # `backend=`. Catch the shape that was removed: `backend=<name>` where
    # <name> is a locally-defined function taking a runtime argument.
    factory_defs = set(re.findall(r"def\s+(\w*backend\w*factory\w*)\s*\(", src, re.I))
    passed = set(re.findall(r"backend\s*=\s*(\w+)\s*,", src))

    reintroduced = factory_defs & passed
    assert not reintroduced, (
        f"core/engine.py passes a backend *factory* again: {sorted(reintroduced)}. "
        f"deepagents 0.7.0 removes callable `backend=` support, so this breaks the "
        f"agent build on upgrade. Pass a BackendProtocol instance instead — see "
        f"_local_shell_backend."
    )


if __name__ == "__main__":
    tests = [
        test_general_purpose_override_contract,
        test_general_purpose_auto_add_still_name_keyed,
        test_docker_backend_satisfies_protocol,
        test_backend_accepts_protocol_instance,
        test_no_build_path_passes_a_backend_factory,
    ]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"OK — {len(tests)} deepagents contract tripwires green.")
