"""No function in ``bootstrap.py`` reads a name it never binds.

Written after an extraction introduced exactly that bug and **every test suite
stayed green**.

Phase 3 step 3.3 moves blocks out of ``build_daemon`` into subsystem builders.
When the ``build_retention`` slice moved, it took
``from yuyutsava.todoboard.exchange import get_default_exchange`` with it — but
``build_daemon`` still called ``get_default_exchange()`` further down, in the
TODO-note-recall block. That block is guarded by
``if pg_pool is not None and embedder is not None``, so it only runs on
**Postgres**, and the SQLite-only suites never reached the ``NameError``.

Static analysis found it in milliseconds. That is the point of this file: an
extraction can strand a name in a branch no test exercises, and the failure only
appears on a live daemon boot in the backend you happen not to be running.

Run:  .venv/bin/python test/daemon/test_bootstrap_no_unbound_names.py
"""

from __future__ import annotations

import ast
import builtins
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2] / "yuyutsava"
MODULE = _ROOT / "daemon" / "bootstrap.py"

#: Modules where a name is assembled from many branches, so a stranded or
#: shadowed import hides in a path no test happens to run. ``engine.py`` earned
#: its place in Phase 4: a module-level ``LangChainPolicyAdapter`` import plus a
#: leftover *local* one in the same function made every CLI build raise
#: ``UnboundLocalError`` — the mirror image of the bootstrap bug above, and
#: invisible to the check that found that one.
SHADOW_MODULES = (MODULE, _ROOT / "core" / "engine.py")


def _module_level_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _bound_within(fn: ast.AST) -> set[str]:
    """Every name *fn* binds: assignments, imports, ``with``/``for`` targets,
    nested defs, comprehension targets, params of nested functions, and
    except-handler aliases."""
    bound: set[str] = set()

    def add_target(node: ast.AST) -> None:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name):
                bound.add(sub.id)

    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                               ast.Lambda)):
            # Lambda has params but no name. Omitting it here made the guard
            # report `middleware_factory=lambda sa: ...`'s parameter as unbound
            # the moment that lambda moved into a function that did not happen
            # to bind `sa` for some other reason. A false positive on a guard is
            # as costly as a miss: it trains you to override it.
            bound.add(getattr(node, "name", ""))
            args = getattr(node, "args", None)
            if args is not None:
                for a in (*args.args, *args.kwonlyargs, *args.posonlyargs):
                    bound.add(a.arg)
                for a in (args.vararg, args.kwarg):
                    if a is not None:
                        bound.add(a.arg)
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            add_target(node.optional_vars)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            add_target(node.target)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.Global) or isinstance(node, ast.Nonlocal):
            bound.update(node.names)
    return bound


class BootstrapNamesResolve(unittest.TestCase):
    def test_no_function_reads_an_unbound_name(self) -> None:
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        module_names = _module_level_names(tree) | set(dir(builtins))

        problems: list[str] = []
        for fn in tree.body:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            params = {a.arg for a in (*fn.args.args, *fn.args.kwonlyargs, *fn.args.posonlyargs)}
            for a in (fn.args.vararg, fn.args.kwarg):
                if a is not None:
                    params.add(a.arg)

            known = _bound_within(fn) | params | module_names
            for node in ast.walk(fn):
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                    if node.id not in known:
                        problems.append(f"{fn.name}:{node.lineno} reads unbound `{node.id}`")

        self.assertEqual(
            problems, [],
            "bootstrap.py reads names nothing binds:\n  " + "\n  ".join(problems)
            + "\n\nUsually an extraction moved an import into a subsystem builder "
              "while a use stayed behind. These raise NameError only when the "
              "enclosing branch runs — often only on one storage backend, which "
              "is why the test suites can stay green.",
        )


class NoLocalImportShadowsAModuleLevelOne(unittest.TestCase):
    """A local import makes the name local for the WHOLE function.

    Python decides scope per function, not per line: one
    ``from x import Y`` anywhere in a body makes every ``Y`` in that body local,
    including reads *above* it. So adding a module-level import while leaving an
    older local one behind turns working code into::

        UnboundLocalError: cannot access local variable 'Y' where it is not
        associated with a value

    ...on every call. That is exactly what happened when
    ``LangChainPolicyAdapter`` was promoted to a module-level import in
    ``engine.py`` while ``_policy_middleware`` kept its local copy further down.

    ``test_no_function_reads_an_unbound_name`` cannot see this: the name *is*
    bound, just later than it is read.
    """

    def _offenders(self, path: Path) -> list[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found: list[str] = []
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # name -> line of its first local import binding
            local_imports: dict[str, int] = {}
            for node in ast.walk(fn):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    for alias in node.names:
                        bound = (alias.asname or alias.name).split(".")[0]
                        local_imports.setdefault(bound, node.lineno)
            if not local_imports:
                continue
            for node in ast.walk(fn):
                if not (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)):
                    continue
                imported_at = local_imports.get(node.id)
                if imported_at is not None and node.lineno < imported_at:
                    found.append(
                        f"{path.name}:{fn.name} reads {node.id!r} at line "
                        f"{node.lineno} but imports it locally at line "
                        f"{imported_at}"
                    )
        return sorted(set(found))

    def test_no_read_precedes_its_local_import(self) -> None:
        problems: list[str] = []
        for path in SHADOW_MODULES:
            problems.extend(self._offenders(path))
        self.assertEqual(
            problems, [],
            "a local import shadows an earlier read — UnboundLocalError at "
            "runtime:\n  " + "\n  ".join(problems),
        )

    def test_the_modules_were_actually_parsed(self) -> None:
        """Negative control — a missing file would make the check vacuous."""
        for path in SHADOW_MODULES:
            with self.subTest(module=path.name):
                self.assertTrue(path.exists())
                self.assertGreater(len(path.read_text(encoding="utf-8")), 1000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
