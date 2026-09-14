"""Imports point down the layer map, and private names stay inside their package.

Code Review v0.1.0 — rules STD-LAY-1 and STD-IMP-5 in
``docs/code-review/v0.1.0/01-standards.md``; findings R-LAY-01, R-LAY-02, R-IMP-07.

A module may import only from its own layer or a lower one. **Every** import
counts, at module level or inside a function: moving an import does not remove
the dependency, so the rule must not care where the statement sits. Only
``if TYPE_CHECKING:`` imports are exempt, because they do not exist at runtime.

The unit of the map is the sub-package, except that storage's foundation
modules (``paths``, ``ids``, ``dialect``) are units of their own.

The violations that exist today are listed below, and both lists are ratchets:
a new violation fails, and so does an entry that no longer occurs. Delete the
entry in the same commit that removes the import, so the lists only shrink.

Same-layer cycles (``agents`` <-> ``core``, ``channels`` <-> ``daemon``) are not
this test's concern; ``scripts/measure_code_health.py`` tracks them.

Run:  .venv/bin/python -m unittest test.architecture.test_import_layers
"""

from __future__ import annotations

import ast
import unittest
from collections.abc import Iterator
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PACKAGE = "yuyutsava"
STORAGE_FOUNDATIONS = frozenset({"paths", "ids", "dialect"})

#: Lowest layer first. Keep in step with the table in 01-standards.md §3.
LAYERS: dict[str, int] = {
    # L0 — leaves
    "(root)": 0,
    "ports": 0,
    "models": 0,
    "aio": 0,
    "platform": 0,
    "config": 0,
    # L1 — foundations
    "storage.paths": 1,
    "storage.ids": 1,
    "storage.dialect": 1,
    "llm": 1,
    # L2 — domains and stores
    "storage": 2,
    "retrieval": 2,
    "memory": 2,
    "skills": 2,
    "prefs": 2,
    "visuals": 2,
    "artifacts": 2,
    "events": 2,
    "io": 2,
    "audio_io": 2,
    # L3 — agent capabilities
    "policy": 3,
    "context": 3,
    "discovery": 3,
    "mcp": 3,
    "consent": 3,
    "tools": 3,
    # L4 — agent assembly
    "core": 4,
    "agents": 4,
    "conversation": 4,
    "todoboard": 4,
    "async_subagents": 4,
    "sessions": 4,
    # L5 — composition roots
    "cli": 5,
    "daemon": 5,
    "channels": 5,
}

#: Upward imports that exist today, as (importer unit, imported unit). Shrinks only.
ALLOWED_UPWARD: frozenset[tuple[str, str]] = frozenset(
    {
        ("agents", "cli"),
        ("agents", "daemon"),
        ("artifacts", "context"),
        ("artifacts", "todoboard"),
        ("async_subagents", "daemon"),
        ("context", "core"),
        ("core", "daemon"),
        ("events", "core"),
        ("llm", "core"),
        ("memory", "core"),
        ("prefs", "core"),
        ("skills", "core"),
        ("skills", "discovery"),
        ("storage", "consent"),
        ("storage", "context"),
        ("storage", "core"),
        ("storage", "daemon"),
        ("storage", "todoboard"),
        ("storage.dialect", "storage"),
        ("tools", "core"),
        ("visuals", "context"),
    }
)

#: Private names imported across packages today, as (importer, imported module, name).
ALLOWED_PRIVATE: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("yuyutsava.agents.tinker.agent", "yuyutsava.cli.agent_stack", "_build_retrieval_stores"),
        ("yuyutsava.agents.tinker.prompts", "yuyutsava.core.prompts", "_rules_section"),
        ("yuyutsava.agents.tinker.prompts", "yuyutsava.core.prompts", "_tool_discovery_section"),
        ("yuyutsava.conversation.service", "yuyutsava.sessions.runner", "_CoalescedTicker"),
        ("yuyutsava.conversation.service", "yuyutsava.sessions.runner", "_count_memory_files"),
        ("yuyutsava.conversation.service", "yuyutsava.sessions.runner", "_resolve_session"),
        ("yuyutsava.core.engine", "yuyutsava.agents.orchestrator.agent", "_make_ask_user_tool"),
        (
            "yuyutsava.daemon.web.routers.sessions",
            "yuyutsava.core.streaming",
            "_artifact_event_from_result",
        ),
    }
)

Upward = dict[tuple[str, str], list[str]]
Private = dict[tuple[str, str, str], list[str]]


def unit_of(dotted: str) -> str | None:
    """The layer-map unit a dotted module name belongs to; None outside the package."""
    parts = dotted.split(".")
    if parts[0] != PACKAGE:
        return None
    if len(parts) == 1:
        return "(root)"
    if parts[1] == "storage" and len(parts) > 2 and parts[2] in STORAGE_FOUNDATIONS:
        return f"storage.{parts[2]}"
    return parts[1]


def _import_targets(node: ast.Import | ast.ImportFrom, module: str, is_package: bool) -> list[str]:
    """Dotted module names an import statement loads, with relative imports resolved."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if node.level:
        base = module.split(".")
        if not is_package:
            base = base[:-1]
        base = base[: len(base) - (node.level - 1)]
        resolved = ".".join(base + ([node.module] if node.module else []))
    else:
        resolved = node.module or ""
    if resolved == PACKAGE:  # from yuyutsava import platform
        return [f"{PACKAGE}.{alias.name}" for alias in node.names]
    return [resolved]


def _under_type_checking(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    while node in parents:
        node = parents[node]
        if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.unparse(node.test):
            return True
    return False


def scan_source(
    source: str,
    module: str,
    is_package: bool = False,
    origin: str = "<source>",
) -> tuple[Upward, Private]:
    """Upward imports and private cross-package imports in one module's source."""
    importer = unit_of(module)
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    upward: Upward = {}
    private: Private = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)) or _under_type_checking(
            node, parents
        ):
            continue
        site = f"{origin}:{node.lineno}"
        for target in _import_targets(node, module, is_package):
            imported = unit_of(target)
            if imported is None or imported == importer:
                continue
            if LAYERS.get(imported, 0) > LAYERS.get(importer, 0):
                upward.setdefault((importer, imported), []).append(site)
            if (
                isinstance(node, ast.ImportFrom)
                and imported.split(".")[0] != importer.split(".")[0]
            ):
                for alias in node.names:
                    if alias.name.startswith("_") and not alias.name.startswith("__"):
                        private.setdefault((module, target, alias.name), []).append(site)
    return upward, private


def _package_modules() -> Iterator[tuple[Path, str, bool]]:
    for path in sorted((REPO / PACKAGE).rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        parts = list(path.relative_to(REPO).with_suffix("").parts)
        is_package = parts[-1] == "__init__"
        if is_package:
            parts = parts[:-1]
        yield path, ".".join(parts), is_package


def scan_package() -> tuple[Upward, Private, set[str]]:
    """Upward imports, private cross-package imports, and every unit seen."""
    upward: Upward = {}
    private: Private = {}
    units: set[str] = set()
    for path, module, is_package in _package_modules():
        units.add(unit_of(module))
        found_up, found_private = scan_source(
            path.read_text(encoding="utf-8"),
            module,
            is_package,
            str(path.relative_to(REPO)),
        )
        for key, sites in found_up.items():
            upward.setdefault(key, []).extend(sites)
        for key, sites in found_private.items():
            private.setdefault(key, []).extend(sites)
    return upward, private, units


def ratchet(allowed: frozenset, found: dict) -> tuple[list, list]:
    """(new, stale): found but not allowed, and allowed but no longer found."""
    return sorted(key for key in found if key not in allowed), sorted(
        key for key in allowed if key not in found
    )


class ImportsPointDownTheLayerMap(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.upward, cls.private, cls.units = scan_package()

    def test_the_scan_saw_the_package(self) -> None:
        """Negative control — an empty scan would pass every check below."""
        self.assertGreater(len(self.units), 25)
        self.assertTrue(
            self.upward, "the tree has known upward imports; finding none means the scan broke"
        )

    def test_every_unit_has_a_layer(self) -> None:
        missing = sorted(unit for unit in self.units if unit not in LAYERS)
        self.assertEqual(
            missing,
            [],
            "these sub-packages have no place in the layer map: "
            + ", ".join(missing)
            + "\nDecide where each belongs and add it to LAYERS and to 01-standards.md §3.",
        )

    def test_no_new_upward_import(self) -> None:
        new, _ = ratchet(ALLOWED_UPWARD, self.upward)
        self.assertEqual(
            [
                f"{a} (L{LAYERS[a]}) -> {b} (L{LAYERS[b]}) at {', '.join(self.upward[(a, b)])}"
                for a, b in new
            ],
            [],
            "\nA lower layer now imports a higher one. Depend on a Protocol in yuyutsava/ports/ "
            "that a composition root injects, or move the code to the layer it belongs in "
            "(docs/code-review/v0.1.0/01-standards.md §3).",
        )

    def test_upward_allowlist_has_no_stale_entry(self) -> None:
        _, stale = ratchet(ALLOWED_UPWARD, self.upward)
        self.assertEqual(
            stale,
            [],
            "\nThese upward imports no longer exist — delete them from ALLOWED_UPWARD in the "
            "same commit, so they cannot come back unnoticed.",
        )

    def test_no_new_private_import(self) -> None:
        new, _ = ratchet(ALLOWED_PRIVATE, self.private)
        self.assertEqual(
            [
                f"{importer} imports {module}.{name} at {', '.join(self.private[(importer, module, name)])}"
                for importer, module, name in new
            ],
            [],
            "\nA private name is now imported from another package. If both packages need it, "
            "it is public API: rename it without the underscore and document it (STD-IMP-5).",
        )

    def test_private_allowlist_has_no_stale_entry(self) -> None:
        _, stale = ratchet(ALLOWED_PRIVATE, self.private)
        self.assertEqual(
            stale,
            [],
            "\nThese private imports no longer exist — delete them from ALLOWED_PRIVATE.",
        )


class TheDetectorWorks(unittest.TestCase):
    """Negative controls: every rule fires on a synthetic violation."""

    def test_flags_an_upward_import_however_it_is_spelled(self) -> None:
        for source in (
            "import yuyutsava.daemon.main\n",
            "from yuyutsava.daemon import main\n",
            "from yuyutsava import daemon\n",
            "from ..daemon import main\n",
            "def later():\n    from yuyutsava.daemon.main import run\n",
        ):
            with self.subTest(source=source):
                upward, _ = scan_source(source, "yuyutsava.models.example")
                self.assertIn(("models", "daemon"), upward)

    def test_type_checking_imports_are_exempt(self) -> None:
        source = "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from yuyutsava.daemon.main import run\n"
        upward, _ = scan_source(source, "yuyutsava.models.example")
        self.assertEqual(upward, {})

    def test_downward_and_same_unit_imports_pass(self) -> None:
        source = "from yuyutsava.ports import CapEnforcer\nfrom yuyutsava.daemon.channels import ChannelRouter\n"
        upward, _ = scan_source(source, "yuyutsava.daemon.bootstrap")
        self.assertEqual(upward, {})

    def test_storage_foundations_are_their_own_units(self) -> None:
        upward, _ = scan_source(
            "from yuyutsava.storage.factory import StoreFactory\n", "yuyutsava.storage.paths"
        )
        self.assertIn(("storage.paths", "storage"), upward)

    def test_flags_a_private_cross_package_import(self) -> None:
        _, private = scan_source(
            "from yuyutsava.core.config import _env\n", "yuyutsava.memory.config"
        )
        self.assertIn(("yuyutsava.memory.config", "yuyutsava.core.config", "_env"), private)

    def test_private_names_within_one_package_pass(self) -> None:
        _, private = scan_source(
            "from yuyutsava.core.config import _env\n", "yuyutsava.core.engine"
        )
        self.assertEqual(private, {})

    def test_ratchet_reports_new_and_stale_entries(self) -> None:
        new, stale = ratchet(frozenset({("a", "b")}), {("c", "d"): ["x.py:1"]})
        self.assertEqual((new, stale), ([("c", "d")], [("a", "b")]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
