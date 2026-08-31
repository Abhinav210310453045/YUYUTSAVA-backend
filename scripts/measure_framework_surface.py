#!/usr/bin/env python
"""How much of this codebase is built *inside* the agent framework.

Phase 4's tracking metric (ADR-004, `F-T01`/`F-T02`/`F-T03`).

ADR-004 proposes measuring it as::

    grep -rl "langchain\\|langgraph\\|deepagents" yuyutsava | wc -l

That number is not usable as a gate. It counts any file that *mentions* the
words, so writing a docstring explaining why a module avoids the framework makes
the score worse — measured: the count rose 93 → 99 while the first policy was
being migrated *off* the framework, entirely from prose in new modules.

This counts imports, by parsing. Three numbers, because they answer different
questions:

    importers      modules that import a framework symbol at all
    subclasses     classes extending AgentMiddleware — the F-T01 headline
    policies       migrated Policy implementations, the other side of that trade

Run:  .venv/bin/python scripts/measure_framework_surface.py
      .venv/bin/python scripts/measure_framework_surface.py --list
"""

from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PKG = ROOT / "yuyutsava"
FRAMEWORKS = ("langchain", "langchain_core", "langgraph", "deepagents")


def _is_framework(name: str) -> bool:
    return any(name == fw or name.startswith(fw + ".") for fw in FRAMEWORKS)


def _modules() -> list[pathlib.Path]:
    return sorted(p for p in PKG.rglob("*.py") if "__pycache__" not in p.parts)


def scan() -> dict[str, list[str]]:
    importers: list[str] = []
    subclasses: list[str] = []
    policies: list[str] = []

    for path in _modules():
        rel = str(path.relative_to(ROOT))
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue

        imports_framework = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if _is_framework(node.module):
                    imports_framework = True
            elif isinstance(node, ast.Import):
                if any(_is_framework(a.name) for a in node.names):
                    imports_framework = True
            elif isinstance(node, ast.ClassDef):
                for base in node.bases:
                    label = None
                    if isinstance(base, ast.Name):
                        label = base.id
                    elif isinstance(base, ast.Subscript) and isinstance(base.value, ast.Name):
                        label = base.value.id
                    elif isinstance(base, ast.Attribute):
                        label = base.attr
                    if label == "AgentMiddleware":
                        subclasses.append(f"{rel}:{node.name}")
                    if label == "Policy" and "policy" not in path.parts[-1:]:
                        policies.append(f"{rel}:{node.name}")
        if imports_framework:
            importers.append(rel)

    return {
        "importers": importers,
        "subclasses": subclasses,
        "policies": policies,
        "descendants": _middleware_descendants(),
    }


def _middleware_descendants() -> list[str]:
    """Every class that IS an ``AgentMiddleware``, however deep the inheritance.

    The AST scan above matches the literal base name, so it counts classes
    written ``class X(AgentMiddleware)`` and misses ones that inherit through a
    concrete framework middleware. ``YuyutsavaCompactionMiddleware`` is exactly
    that — it extends ``SummarizationMiddleware``, which extends
    ``AgentMiddleware`` — so reporting only the AST count would have claimed a
    clean sweep while a framework subclass was still there.

    This resolves it by import and ``issubclass``, which is slower and exact.
    Both numbers are printed: the AST count is what ADR-004's "one adapter"
    target is about, and this is the honest total.
    """
    import importlib

    found: list[str] = []
    try:
        from langchain.agents.middleware import AgentMiddleware
    except Exception:  # pragma: no cover - framework absent
        return found

    for path in _modules():
        rel = str(path.relative_to(ROOT))
        parts = list(path.relative_to(ROOT).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        try:
            module = importlib.import_module(".".join(parts))
        except Exception:
            continue
        for name, obj in vars(module).items():
            if (isinstance(obj, type) and issubclass(obj, AgentMiddleware)
                    and obj is not AgentMiddleware
                    and getattr(obj, "__module__", "") == ".".join(parts)):
                found.append(f"{rel}:{name}")
    return sorted(set(found))


def main() -> int:
    result = scan()
    total = len(_modules())
    print(f"modules scanned            {total}")
    print(f"import a framework         {len(result['importers'])} "
          f"({len(result['importers']) * 100 // max(total, 1)}%)")
    print(f"AgentMiddleware subclasses {len(result['subclasses'])}  (written as such)")
    print(f"  ...including inherited   {len(result['descendants'])}  (issubclass, exact)")
    print(f"migrated Policy classes    {len(result['policies'])}")
    if "--list" in sys.argv:
        for key in ("subclasses", "descendants", "policies", "importers"):
            print(f"\n--- {key}")
            for item in result[key]:
                print(" ", item)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
