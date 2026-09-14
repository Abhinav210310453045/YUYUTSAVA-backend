#!/usr/bin/env python
"""Measure the code-health metrics the code reviews track.

    .venv/bin/python scripts/measure_code_health.py               # summary
    .venv/bin/python scripts/measure_code_health.py --details 10  # plus the worst offenders
    .venv/bin/python scripts/measure_code_health.py --json        # machine-readable

Every number in ``docs/code-review/<version>/02-metrics.md`` comes from this
script, so a review's "now" and "after" columns are measured the same way.

It only parses source text — it never imports the package — so it runs on a
tree that does not import, and it has no side effects. Cyclomatic complexity
here is an approximation (branch nodes + 1, nested functions included, as in
the 2026-08-08 baseline); ruff's ``C901`` is the authority for gating.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGE = "yuyutsava"
CONSUMER_DIRS = ("yuyutsava", "test", "scripts")
STDLIB = frozenset(sys.stdlib_module_names)
FRAMEWORK_PREFIXES = ("langchain", "langgraph", "deepagents")
HISTORY_PHRASES = re.compile(
    r"\b(Phase \d|Step \d(?:\.\d)?|was:|replaces the|no longer|used to be)\b"
)
BRANCH_NODES = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.IfExp,
    ast.ExceptHandler,
    ast.With,
    ast.AsyncWith,
    ast.Assert,
    ast.comprehension,
    ast.match_case,
)
FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)


@dataclass
class Module:
    path: Path
    name: str
    is_package: bool
    source: str
    tree: ast.Module

    @property
    def subpackage(self) -> str:
        return subpackage_of(self.name)


def subpackage_of(dotted: str) -> str:
    parts = dotted.split(".")
    return parts[1] if len(parts) > 1 and parts[0] == PACKAGE else "(root)"


def load_modules(directory: str) -> list[Module]:
    modules = []
    for path in sorted((REPO / directory).rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        parts = list(path.relative_to(REPO).with_suffix("").parts)
        is_package = parts[-1] == "__init__"
        if is_package:
            parts = parts[:-1]
        modules.append(
            Module(path.relative_to(REPO), ".".join(parts), is_package, source, ast.parse(source))
        )
    return modules


def parents_of(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def ancestors(node: ast.AST, parents: dict[ast.AST, ast.AST]):
    while node in parents:
        node = parents[node]
        yield node


def in_function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    return any(isinstance(a, FUNCTION_NODES) for a in ancestors(node, parents))


def in_try_within_function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    for a in ancestors(node, parents):
        if isinstance(a, FUNCTION_NODES):
            return False
        if isinstance(a, ast.Try):
            return True
    return False


def under_type_checking(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    return any(
        isinstance(a, ast.If) and "TYPE_CHECKING" in ast.unparse(a.test)
        for a in ancestors(node, parents)
    )


def import_targets(node: ast.Import | ast.ImportFrom, module: Module) -> list[str]:
    """Dotted module names an import statement loads."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if node.level:
        base = module.name.split(".")
        if not module.is_package:
            base = base[:-1]
        base = base[: len(base) - (node.level - 1)]
        resolved = ".".join(base + ([node.module] if node.module else []))
    else:
        resolved = node.module or ""
    if resolved == PACKAGE:  # from yuyutsava import platform
        return [f"{PACKAGE}.{alias.name}" for alias in node.names]
    return [resolved]


def is_internal(target: str) -> bool:
    return target == PACKAGE or target.startswith(PACKAGE + ".")


def complexity(function: ast.AST) -> int:
    score = 1
    for node in ast.walk(function):
        if isinstance(node, BRANCH_NODES):
            score += 1
        elif isinstance(node, ast.BoolOp):
            score += len(node.values) - 1
    return score


def parameter_count(function: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    args = function.args
    count = len(args.posonlyargs) + len(args.args) + len(args.kwonlyargs)
    positional = args.posonlyargs + args.args
    if positional and positional[0].arg in ("self", "cls"):
        count -= 1
    return count


def is_silent_broad_handler(handler: ast.ExceptHandler) -> bool:
    broad = handler.type is None or (
        isinstance(handler.type, ast.Name) and handler.type.id in ("Exception", "BaseException")
    )
    if not broad:
        return False

    def trivial(statement: ast.stmt) -> bool:
        if isinstance(statement, (ast.Pass, ast.Continue)):
            return True
        if isinstance(statement, ast.Return):
            value = statement.value
            return value is None or (
                isinstance(value, ast.Constant) and value.value in (None, False, "", 0)
            )
        return (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and statement.value.value is Ellipsis
        )

    return all(trivial(s) for s in handler.body)


def module_level_imports(tree: ast.Module):
    """Imports at module level, including inside a top-level ``try`` or a non-TYPE_CHECKING ``if``."""
    for statement in tree.body:
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            yield statement
        elif isinstance(statement, ast.Try) or (
            isinstance(statement, ast.If) and "TYPE_CHECKING" not in ast.unparse(statement.test)
        ):
            for inner in statement.body:
                if isinstance(inner, (ast.Import, ast.ImportFrom)):
                    yield inner


def mid_file_imports(module: Module) -> list[str]:
    """Module-level imports placed after the first top-level ``def`` or ``class``."""
    first_definition = next(
        (s.lineno for s in module.tree.body if isinstance(s, (*FUNCTION_NODES, ast.ClassDef))), None
    )
    if first_definition is None:
        return []
    return [
        f"{module.path}:{n.lineno}"
        for n in module_level_imports(module.tree)
        if n.lineno > first_definition
    ]


def imports_after_code(module: Module) -> list[str]:
    """Module-level imports preceded by executable statements (the E402 shape)."""
    found, seen_code = [], False
    for index, statement in enumerate(module.tree.body):
        if (
            index == 0
            and isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
        ):
            continue
        if isinstance(statement, ast.ImportFrom) and statement.module == "__future__":
            continue
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            if seen_code:
                found.append(f"{module.path}:{statement.lineno}")
            continue
        if isinstance(statement, ast.If) and "TYPE_CHECKING" in ast.unparse(statement.test):
            continue
        if isinstance(statement, ast.Try) and all(
            isinstance(s, (ast.Import, ast.ImportFrom)) for s in statement.body
        ):
            if seen_code:
                found.append(f"{module.path}:{statement.lineno}")
            continue
        seen_code = True
    return found


def strongly_connected_components(edges: dict[str, set[str]]) -> list[list[str]]:
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[list[str]] = []

    def visit(node: str) -> None:
        index[node] = low[node] = len(index)
        stack.append(node)
        on_stack.add(node)
        for successor in edges.get(node, ()):
            if successor not in index:
                visit(successor)
                low[node] = min(low[node], low[successor])
            elif successor in on_stack:
                low[node] = min(low[node], index[successor])
        if low[node] == index[node]:
            component = []
            while True:
                member = stack.pop()
                on_stack.discard(member)
                component.append(member)
                if member == node:
                    break
            components.append(sorted(component))

    for node in sorted(edges):
        if node not in index:
            visit(node)
    return components


def measure() -> dict[str, object]:
    modules = load_modules(PACKAGE)
    consumers = modules + load_modules("test") + load_modules("scripts")

    deferred = Counter()
    deferred_by_file = Counter()
    internal_top = internal_deferred = 0
    private_imports: list[str] = []
    edges: dict[str, set[str]] = defaultdict(set)
    edge_example: dict[tuple[str, str], str] = {}
    framework_modules = 0
    functions: list[tuple[int, int, int, str]] = []  # lines, params, complexity, label
    classes: list[tuple[int, str]] = []
    silent_handlers: list[str] = []
    prints_outside_cli: list[str] = []
    global_statements: list[str] = []
    set_default_definitions: list[str] = []
    default_accessor_calls = 0
    mid_file: list[str] = []
    after_code: list[str] = []
    history = Counter()
    todo_markers = 0
    create_table_modules: list[str] = []

    for module in modules:
        parents = parents_of(module.tree)
        uses_framework = False
        for node in ast.walk(module.tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                targets = import_targets(node, module)
                if any(t.split(".")[0].startswith(FRAMEWORK_PREFIXES) for t in targets):
                    uses_framework = True
                internal = any(is_internal(t) for t in targets)
                deferred_here = in_function(node, parents)
                if deferred_here:
                    top = targets[0].split(".")[0]
                    kind = (
                        "internal" if internal else ("stdlib" if top in STDLIB else "third-party")
                    )
                    if in_try_within_function(node, parents):
                        kind += " in try"
                    deferred[kind] += 1
                    deferred_by_file[str(module.path)] += 1
                if not internal:
                    continue
                if deferred_here:
                    internal_deferred += 1
                else:
                    internal_top += 1
                if isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        target_sub = subpackage_of(targets[0])
                        if (
                            alias.name.startswith("_")
                            and not alias.name.startswith("__")
                            and target_sub != module.subpackage
                        ):
                            private_imports.append(
                                f"{module.path}:{node.lineno} {targets[0]}.{alias.name}"
                            )
                if (
                    not deferred_here
                    and not under_type_checking(node, parents)
                    and module.subpackage != "(root)"
                ):
                    for target in targets:
                        target_sub = subpackage_of(target)
                        if target_sub not in (module.subpackage, "(root)"):
                            edges[module.subpackage].add(target_sub)
                            edge_example.setdefault(
                                (module.subpackage, target_sub), f"{module.path}:{node.lineno}"
                            )
            elif isinstance(node, FUNCTION_NODES):
                length = node.end_lineno - node.lineno + 1
                functions.append(
                    (
                        length,
                        parameter_count(node),
                        complexity(node),
                        f"{module.path}:{node.lineno} {node.name}",
                    )
                )
                if node.name.startswith("set_default_"):
                    set_default_definitions.append(f"{module.path}:{node.lineno} {node.name}")
            elif isinstance(node, ast.ClassDef):
                classes.append(
                    (node.end_lineno - node.lineno + 1, f"{module.path}:{node.lineno} {node.name}")
                )
            elif isinstance(node, ast.ExceptHandler) and is_silent_broad_handler(node):
                silent_handlers.append(f"{module.path}:{node.lineno}")
            elif isinstance(node, ast.Global):
                global_statements.append(f"{module.path}:{node.lineno}")
            elif isinstance(node, ast.Call):
                name = (
                    node.func.id
                    if isinstance(node.func, ast.Name)
                    else getattr(node.func, "attr", "")
                )
                if (
                    name == "print"
                    and module.subpackage != "cli"
                    and not module.path.name.endswith("_proc.py")
                ):
                    prints_outside_cli.append(f"{module.path}:{node.lineno}")
                if name.startswith(("set_default_", "get_default_")):
                    default_accessor_calls += 1
        framework_modules += uses_framework
        mid_file += mid_file_imports(module)
        after_code += imports_after_code(module)
        phrases = len(HISTORY_PHRASES.findall(module.source))
        if phrases:
            history[str(module.path)] = phrases
        todo_markers += len(re.findall(r"\b(TODO|FIXME|XXX|HACK)\b", module.source))
        if re.search(r"CREATE TABLE", module.source, re.IGNORECASE):
            create_table_modules.append(str(module.path))

    # __init__.py audit. A name is used from a package root when it is imported
    # from it, or read as an attribute of the bound package
    # (``from yuyutsava import platform as yplatform`` ... ``yplatform.kill_tree``).
    root_imports: dict[str, set[str]] = defaultdict(set)
    for consumer in consumers:
        bound_packages: dict[str, str] = {}
        for node in ast.walk(consumer.tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.level == 0
                and node.module
                and is_internal(node.module)
            ):
                root_imports[node.module].update(alias.name for alias in node.names)
                if node.module == PACKAGE:
                    for alias in node.names:
                        bound_packages[alias.asname or alias.name] = f"{PACKAGE}.{alias.name}"
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if is_internal(alias.name):
                        bound_packages[alias.asname or alias.name] = alias.name
        if not bound_packages:
            continue
        for node in ast.walk(consumer.tree):
            if isinstance(node, ast.Attribute):
                owner = ast.unparse(node.value)
                if owner in bound_packages:
                    root_imports[bound_packages[owner]].add(node.attr)
    inits_without_docstring, inits_with_logic, unused_exports = [], [], {}
    for module in (m for m in modules if m.is_package):
        if ast.get_docstring(module.tree) is None:
            inits_without_docstring.append(str(module.path))
        exported: list[str] = []
        for statement in module.tree.body:
            if isinstance(statement, (*FUNCTION_NODES, ast.ClassDef)):
                inits_with_logic.append(str(module.path))
                break
        for statement in module.tree.body:
            if isinstance(statement, ast.Assign) and any(
                getattr(t, "id", None) == "__all__" for t in statement.targets
            ):
                exported = [
                    e.value
                    for e in getattr(statement.value, "elts", [])
                    if isinstance(e, ast.Constant)
                ]
        unused = [name for name in exported if name not in root_imports.get(module.name, set())]
        if unused:
            unused_exports[module.name] = f"{len(unused)} of {len(exported)}"

    components = strongly_connected_components(edges)
    largest_component = max(components, key=len)
    two_cycles = sorted(
        f"{a}<->{b} ({edge_example[(a, b)]} / {edge_example[(b, a)]})"
        for a in edges
        for b in edges[a]
        if a < b and a in edges.get(b, ())
    )

    def count_over(values, threshold):
        return sum(1 for v in values if v > threshold)

    lengths = [f[0] for f in functions]
    params = [f[1] for f in functions]
    complexities = [f[2] for f in functions]
    worst = lambda key: [f[3] + f" ({f[key]})" for f in sorted(functions, key=lambda f: -f[key])]  # noqa: E731

    return {
        "modules": len(modules),
        "lines": sum(len(m.source.splitlines()) for m in modules),
        "functions": len(functions),
        "in_function_imports": sum(deferred.values()),
        "in_function_imports_by_kind": dict(deferred),
        "deferred_internal_import_ratio": round(
            internal_deferred / max(1, internal_deferred + internal_top), 3
        ),
        "imports_after_code": len(after_code),
        "mid_file_imports": len(mid_file),
        "private_cross_package_imports": len(private_imports),
        "subpackage_cycle_largest_component": len(largest_component),
        "subpackage_two_cycles": len(two_cycles),
        "functions_over_200_lines": count_over(lengths, 200),
        "functions_over_100_lines": count_over(lengths, 100),
        "functions_over_60_lines": count_over(lengths, 60),
        "functions_over_10_params": count_over(params, 10),
        "functions_over_7_params": count_over(params, 7),
        "max_cyclomatic_approx": max(complexities),
        "functions_cyclomatic_over_25": count_over(complexities, 25),
        "functions_cyclomatic_over_15": count_over(complexities, 15),
        "classes_over_400_lines": count_over([c[0] for c in classes], 400),
        "silent_broad_excepts": len(silent_handlers),
        "prints_outside_cli": len(prints_outside_cli),
        "global_statements": len(global_statements),
        "set_default_definitions": len(set_default_definitions),
        "default_accessor_call_sites": default_accessor_calls,
        "history_narration_phrases": sum(history.values()),
        "history_narration_files": len(history),
        "todo_markers": todo_markers,
        "create_table_modules": len(create_table_modules),
        "framework_importing_modules": framework_modules,
        "inits_without_docstring": len(inits_without_docstring),
        "inits_with_logic": len(inits_with_logic),
        "inits_with_unused_exports": len(unused_exports),
        "details": {
            "in_function_imports_by_file": deferred_by_file.most_common(),
            "imports_after_code": after_code,
            "mid_file_imports": mid_file,
            "private_cross_package_imports": private_imports,
            "largest_cycle_component": largest_component,
            "two_cycles": two_cycles,
            "longest_functions": worst(0),
            "most_parameters": worst(1),
            "most_complex": worst(2),
            "largest_classes": [
                f"{label} ({size})" for size, label in sorted(classes, reverse=True)
            ],
            "silent_broad_excepts": silent_handlers,
            "prints_outside_cli": prints_outside_cli,
            "global_statements": global_statements,
            "set_default_definitions": set_default_definitions,
            "history_narration_by_file": history.most_common(),
            "create_table_modules": create_table_modules,
            "inits_without_docstring": inits_without_docstring,
            "inits_with_logic": inits_with_logic,
            "inits_with_unused_exports": unused_exports,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--json", action="store_true", help="print every metric and detail as JSON")
    parser.add_argument(
        "--details",
        type=int,
        default=0,
        metavar="N",
        help="also list the N worst offenders per metric",
    )
    args = parser.parse_args()

    metrics = measure()
    details = metrics.pop("details")
    if args.json:
        print(json.dumps({**metrics, "details": details}, indent=2))
        return 0

    width = max(len(key) for key in metrics)
    for key, value in metrics.items():
        print(f"{key:<{width}}  {value}")
    if args.details:
        for key, items in details.items():
            entries = list(items.items()) if isinstance(items, dict) else list(items)
            if not entries:
                continue
            print(f"\n{key} ({len(entries)}):")
            for entry in entries[: args.details]:
                print(f"  {entry}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
