"""No test module changes the environment when it is imported.

pytest imports every test module before running any test, so an
``os.environ[...] = …`` at module level is in force for the whole run — for
every test collected after it, in whatever order the run happens to use. One
such line (a scratch ``YUYUTSAVA_BLOBS_DIR``) made the artifacts-store tests
list another module's records, and passed only when that file ran alone.
Scope environment changes with ``mock.patch.dict`` in ``setUp`` or
``setUpModule`` instead.

Run:  .venv/bin/python -m pytest test/architecture/test_tests_leave_the_environment_alone.py
"""

from __future__ import annotations

import ast
import unittest

from test.support.paths import REPO

_MUTATORS = {"setdefault", "update", "pop", "clear", "__setitem__", "__delitem__"}


def _is_os_environ(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def import_time_env_writes() -> list[str]:
    """``path:line`` of each module-level statement that mutates ``os.environ``."""
    found = []
    for path in sorted((REPO / "test").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for stmt in tree.body:  # module level only; functions run later, scoped
            for node in ast.walk(stmt):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    break
                hit = (
                    isinstance(node, (ast.Assign, ast.AugAssign, ast.Delete))
                    and any(
                        isinstance(t, ast.Subscript) and _is_os_environ(t.value)
                        for t in (node.targets if hasattr(node, "targets") else [node.target])
                    )
                ) or (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in _MUTATORS
                    and _is_os_environ(node.func.value)
                )
                if hit:
                    found.append(f"{path.relative_to(REPO).as_posix()}:{node.lineno}")
    return found


class TestsLeaveTheEnvironmentAlone(unittest.TestCase):
    def test_no_module_mutates_os_environ_at_import(self) -> None:
        self.assertEqual(
            import_time_env_writes(),
            [],
            "scope environment changes with mock.patch.dict in setUp/setUpModule",
        )


if __name__ == "__main__":
    unittest.main()
