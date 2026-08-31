"""``yuyutsava/ports/`` must import nothing from ``yuyutsava``.

Phase 3 step 3.1 (ADR-003). This is the load-bearing constraint, not a style
preference: ``ports`` only breaks the 9 package-level import cycles if **both**
sides of a cycle can depend on it and it depends on neither. One import back
into the codebase and it becomes just another node in the cycle, the
``object | None`` fields have to come back, and the deferred-import ratio climbs
again.

ADR-003 says explicitly that without a CI guard this decays within two quarters,
because the pressure that created the cycles is still there. This is that guard.

Run:  .venv/bin/python test/test_ports_is_a_leaf.py
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

PORTS = Path(__file__).resolve().parent.parent / "yuyutsava" / "ports"


def _internal_imports(path: Path) -> list[tuple[int, str]]:
    """Every ``yuyutsava.*`` import in *path*, at any nesting depth.

    Deferred (function-body) imports count too. A lazy import still couples the
    module — it just hides the coupling from static tools, which is the exact
    habit ``ports`` exists to end.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.startswith("yuyutsava") and not mod.startswith("yuyutsava.ports"):
                found.append((node.lineno, mod))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("yuyutsava") and not alias.name.startswith("yuyutsava.ports"):
                    found.append((node.lineno, alias.name))
    return found


class PortsIsALeaf(unittest.TestCase):
    def test_no_internal_imports(self) -> None:
        offenders: list[str] = []
        for path in sorted(PORTS.rglob("*.py")):
            for lineno, mod in _internal_imports(path):
                offenders.append(f"{path.relative_to(PORTS.parent.parent)}:{lineno} imports {mod}")

        self.assertEqual(
            offenders, [],
            "yuyutsava/ports/ imported the codebase:\n  " + "\n  ".join(offenders)
            + "\n\nports must be a LEAF. Anything it imports becomes part of every "
              "cycle it was created to break — at which point the object|None "
              "dependency fields and the deferred imports have to come back.\n"
              "Fix: define the protocol structurally here instead of importing "
              "the concrete type.",
        )

    def test_ports_imports_only_stdlib_and_typing(self) -> None:
        """Third-party imports are a subtler version of the same failure.

        A protocol typed against a framework class would drag the framework
        into every module that touches a port — reintroducing ``F-T02``-style
        coupling through the layer meant to remove it.
        """
        allowed = {"__future__", "typing", "abc", "dataclasses", "enum", "collections"}
        offenders: list[str] = []
        for path in sorted(PORTS.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.ImportFrom) and node.module:
                    mods = [node.module]
                elif isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                for m in mods:
                    root = m.split(".")[0]
                    if root and root != "yuyutsava" and root not in allowed:
                        offenders.append(f"{path.name}:{node.lineno} imports {m}")

        self.assertEqual(
            offenders, [],
            "ports/ imported a third-party package:\n  " + "\n  ".join(offenders)
            + f"\n\nAllowed: {sorted(allowed)}. A port typed against a framework "
              "class couples every consumer to that framework.",
        )

    def test_ports_is_importable_in_isolation(self) -> None:
        """The real proof: import ports in a fresh interpreter with nothing else.

        A static scan can miss a cycle that only appears at runtime through
        ``__init__`` re-exports. Importing it first, in a clean process, cannot.
        """
        import subprocess
        import sys

        repo = PORTS.parent.parent
        result = subprocess.run(
            [sys.executable, "-c",
             "import sys; import yuyutsava.ports as p; "
             "leaked = [m for m in sys.modules if m.startswith('yuyutsava.') "
             "and not m.startswith('yuyutsava.ports')]; "
             "print('LEAKED:' + ','.join(sorted(leaked)))"],
            capture_output=True, text=True, cwd=repo, timeout=120,
        )
        self.assertEqual(result.returncode, 0, f"importing ports failed:\n{result.stderr}")
        leaked = result.stdout.strip().removeprefix("LEAKED:").strip()
        self.assertEqual(
            leaked, "",
            f"importing yuyutsava.ports pulled in other yuyutsava modules: {leaked}\n"
            f"That means ports is not a leaf at runtime even if it looks like one "
            f"statically — most likely via a package __init__ re-export.",
        )



class OrchestratorDepsIsFullyTyped(unittest.TestCase):
    """Phase 3 step 3.2: no ``object`` / ``Any`` left on the dependency record.

    ``OrchestratorDeps`` started with **11** fields typed ``object | None`` or
    ``Any`` — each one a dependency the record could not describe, so a caller
    had to read the builder to learn what was expected. The last three were
    resolved by naming what is actually used (``ContextTuning`` reads four
    attributes; ``TaskMirror`` three of eleven methods) rather than by importing
    the concrete classes, which would have pulled ``async_subagents`` — and with
    it the LangGraph host — onto the orchestrator's import path.
    """

    def _fields(self):
        import ast
        import pathlib as _p

        root = _p.Path(__file__).resolve().parents[1] / "yuyutsava"
        tree = ast.parse(
            (root / "agents/orchestrator/agent.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "OrchestratorDeps":
                return [
                    (ast.unparse(s.target), ast.unparse(s.annotation))
                    for s in node.body if isinstance(s, ast.AnnAssign)
                ]
        raise AssertionError("OrchestratorDeps not found")

    def test_fields_were_found(self) -> None:
        """Negative control — the check below is vacuous on an empty list."""
        self.assertGreaterEqual(len(self._fields()), 20)

    def test_no_field_is_untyped(self) -> None:
        vague = [
            f"{name}: {ann}" for name, ann in self._fields()
            if "object" in ann or "Any" in ann
        ]
        self.assertEqual(
            vague, [],
            "these dependency fields are back to an untyped placeholder:\n  "
            + "\n  ".join(vague)
            + "\nName what is actually used with a Protocol in yuyutsava/ports/ "
              "— importing the concrete class is what the cycle forbids, and "
              "`object` is what hides the dependency entirely.",
        )

    def test_the_new_protocols_are_structural(self) -> None:
        """The concrete classes must satisfy them without inheriting."""
        from yuyutsava.async_subagents.mirror import AsyncTaskMirror
        from yuyutsava.async_subagents.remote import RemoteAsyncSubagentSpec
        from yuyutsava.context.config import ContextSettings
        from yuyutsava.ports.policy import (
            ContextTuning, RemoteSubagentSpec, TaskMirror,
        )

        self.assertIsInstance(ContextSettings.from_env("cli"), ContextTuning)
        self.assertIsInstance(AsyncTaskMirror(), TaskMirror)
        self.assertIsInstance(
            RemoteAsyncSubagentSpec(name="n", description="d", graph_id="g", url="u"),
            RemoteSubagentSpec,
        )
        for proto in (ContextTuning, TaskMirror, RemoteSubagentSpec):
            with self.subTest(protocol=proto.__name__):
                self.assertNotIn(
                    proto, ContextSettings.__mro__,
                    "the concrete class INHERITS the protocol; these must be "
                    "structural, or ports stops being a leaf",
                )


class ProtocolsAgreeWithTheirImplementations(unittest.TestCase):
    """A Protocol must not declare ``async`` for a method that is not.

    ``runtime_checkable`` ``isinstance`` compares method **names** only. It does
    not check signatures, return types, or whether a method is a coroutine — so
    a Protocol can declare ``async def count_running()`` while the only class
    that satisfies it defines a plain ``def``, and every structural check still
    passes.

    That is not hypothetical: ``TaskMirror`` declared two of its three methods
    ``async`` when the implementation had neither. Anyone trusting the
    annotation would have written ``await mirror.count_running()`` and got
    ``TypeError: object int can't be used in 'await' expression``. Finding BB.

    Only pairs that are actually wired together are checked — the point is to
    compare a declaration against the thing it describes.
    """

    #: (protocol, concrete class) pairs that production code substitutes.
    PAIRS = (
        ("yuyutsava.ports.policy", "TaskMirror",
         "yuyutsava.async_subagents.mirror", "AsyncTaskMirror"),
    )

    def _methods(self, cls: type) -> dict[str, bool]:
        """Public method name -> is it a coroutine function."""
        import inspect

        return {
            name: inspect.iscoroutinefunction(fn)
            for name, fn in vars(cls).items()
            if callable(fn) and not name.startswith("_")
        }

    def test_pairs_were_resolved(self) -> None:
        """Negative control — an unimportable pair would check nothing."""
        import importlib

        for pmod, pname, cmod, cname in self.PAIRS:
            with self.subTest(protocol=pname):
                self.assertTrue(
                    hasattr(importlib.import_module(pmod), pname)
                    and hasattr(importlib.import_module(cmod), cname)
                )

    def test_async_ness_matches(self) -> None:
        import importlib

        for pmod, pname, cmod, cname in self.PAIRS:
            protocol = getattr(importlib.import_module(pmod), pname)
            concrete = getattr(importlib.import_module(cmod), cname)
            declared = self._methods(protocol)
            actual = self._methods(concrete)
            for method, is_async in declared.items():
                if method not in actual:
                    continue  # presence is the isinstance check's job
                with self.subTest(protocol=pname, method=method):
                    self.assertEqual(
                        is_async, actual[method],
                        f"{pname}.{method} is declared "
                        f"{'async' if is_async else 'sync'} but "
                        f"{cname}.{method} is "
                        f"{'async' if actual[method] else 'sync'}. isinstance() "
                        f"will not catch this — a caller following the "
                        f"annotation gets a TypeError at runtime.",
                    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
