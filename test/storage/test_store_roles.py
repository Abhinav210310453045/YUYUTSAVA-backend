"""Consumers declare the narrow role they use, not the 30-method ``Store``.

Phase 2 step 2.7, closing the ISP half of ``F-S03``.

``Store`` aggregates eight unrelated concerns behind ~30 methods, and 22 modules
depended on it. Measured usage: the median consumer needs **one or two** of
those methods; the widest needs four. So ``store: Store`` in a signature told a
reader nothing about what the function touches, and a change to consent-grant
storage had 22 modules in its type-level blast radius when two used it.

The roles in ``storage/events/roles.py`` are structural ``Protocol``s, so
``Store`` satisfies them all without inheriting anything and no call site
changed — only the declared types moved. That was the point: the facade is a
legitimate construction-time aggregate, and the defect was in what consumers
*declared*, not in the facade existing.

These tests keep it that way.

Run:  .venv/bin/python test/storage/test_store_roles.py
"""

from __future__ import annotations

import inspect
import unittest

from yuyutsava.storage.events import Store
from yuyutsava.storage.events import roles

#: Modules narrowed in step 2.7, with the role each now declares. A regression
#: here means a signature widened back to the god object.
NARROWED: dict[str, str] = {
    "yuyutsava.storage.prefs": "PrefsBackend",
    "yuyutsava.daemon.consent": "ConsentRuleReader",
    "yuyutsava.daemon.triage_loop": "TriageStore",
    "yuyutsava.daemon.task_submission": "TriageStore",
    "yuyutsava.daemon.orchestrator_loop": "DecisionWriter",
    "yuyutsava.storage.sweeper": "EventPayloadSweeper",
    "yuyutsava.agents.orchestrator.spawn": "DecisionWriter",
}


class StoreSatisfiesEveryRole(unittest.TestCase):
    """Structural conformance: no inheritance, no call-site churn."""

    def test_store_implements_all_roles(self) -> None:
        for name in roles.__all__:
            proto = getattr(roles, name)
            required = {m for m in dir(proto) if not m.startswith("_")}
            missing = sorted(m for m in required if not hasattr(Store, m))
            with self.subTest(role=name):
                self.assertEqual(
                    missing, [],
                    f"Store no longer satisfies {name}: missing {missing}. Either "
                    f"the facade lost a method or the role drifted from it.",
                )

    def test_roles_are_actually_narrow(self) -> None:
        """A role wider than ~half the facade is not a role, it is the facade."""
        surface = len({m for m in dir(Store) if not m.startswith("_")})
        for name in roles.__all__:
            proto = getattr(roles, name)
            width = len({m for m in dir(proto) if not m.startswith("_")})
            with self.subTest(role=name):
                self.assertLessEqual(
                    width, surface // 2,
                    f"{name} declares {width} of {surface} Store methods — that is "
                    f"not a narrowing. Split it, or have the consumer take Store "
                    f"and say why.",
                )

    def test_every_role_has_a_consumer(self) -> None:
        """Roles are justified by real usage, not invented taxonomy.

        A Protocol nobody declares is speculative abstraction — the thing this
        review criticises elsewhere. Roles awaiting a consumer are listed
        explicitly so the exemption is deliberate rather than silent.
        """
        declared = set(NARROWED.values())
        # Not yet applied: these consumers take the store untyped or via a
        # dataclass field, so narrowing them is a Phase 3 job (it needs the
        # ports/ extraction to break the import cycles first).
        awaiting = {
            "ConsentRuleWriter",   # folded into TriageStore for now
            "DecisionReader",      # web/routers/decisions.py — untyped hub access
            "EventPayloadWriter",  # events/source.py — untyped
            "PendingAskRegistry",  # daemon/ask_registry.py — untyped
            "ProposalWriter",      # web/services/decision_service.py — untyped
            "RecallReader",        # events/tools.py, context/tools.py — closures
            "ToolCallCounter",     # core/policy.py — untyped, avoids a cycle
        }
        orphans = sorted(set(roles.__all__) - declared - awaiting)
        self.assertEqual(
            orphans, [],
            f"these roles have no consumer and no recorded reason: {orphans}",
        )


class ConsumersDeclareNarrowRoles(unittest.TestCase):
    def test_narrowed_signatures_did_not_widen_back(self) -> None:
        import importlib

        for module_name, role in NARROWED.items():
            mod = importlib.import_module(module_name)
            src = inspect.getsource(mod)
            with self.subTest(module=module_name):
                self.assertIn(
                    f": {role}", src,
                    f"{module_name} no longer declares {role}; it may have "
                    f"reverted to `store: Store`, which re-widens its blast "
                    f"radius from {role}'s methods to all ~30.",
                )

    def test_narrowed_modules_do_not_annotate_store(self) -> None:
        """The whole point: these must not say ``store: Store`` any more."""
        import importlib
        import re

        for module_name in NARROWED:
            mod = importlib.import_module(module_name)
            src = inspect.getsource(mod)
            hits = re.findall(r"store:\s*\"?Store\"?\b", src)
            with self.subTest(module=module_name):
                self.assertEqual(
                    hits, [],
                    f"{module_name} still annotates a parameter as `Store`: {hits}",
                )



class EveryRoleHasAConsumer(unittest.TestCase):
    """A Protocol nobody annotates with narrows nothing.

    Phase 2 step 2.7 wired 7 declared-but-unused roles to their real consumers.
    This keeps the set honest: a role added "for completeness" and never used is
    documentation pretending to be a constraint.
    """

    #: Composed into ``TriageStore`` rather than used directly — the triage loop
    #: genuinely spans three concerns and says so by inheriting all three.
    _COMPOSED = frozenset({"ConsentRuleWriter", "DecisionWriter"})

    #: Consumed through an attribute (``hub.store``) rather than a parameter, so
    #: narrowing it means typing the hub's field — a separate change, not this
    #: one. Listed rather than quietly tolerated.
    _ATTRIBUTE_CONSUMED = frozenset({"DecisionReader"})

    def _roles(self) -> list[str]:
        import ast
        import pathlib as _p

        root = _p.Path(__file__).resolve().parents[2] / "yuyutsava"
        tree = ast.parse((root / "storage/events/roles.py").read_text(encoding="utf-8"))
        return [n.name for n in tree.body if isinstance(n, ast.ClassDef)]

    def _consumers(self, role: str) -> list[str]:
        import pathlib as _p
        import re

        root = _p.Path(__file__).resolve().parents[2] / "yuyutsava"
        return [
            str(f.relative_to(root.parent))
            for f in root.rglob("*.py")
            if f.name != "roles.py" and re.search(rf"\b{role}\b", f.read_text(encoding="utf-8"))
        ]

    def test_roles_were_found(self) -> None:
        """Negative control — the checks below are vacuous on an empty list."""
        self.assertGreaterEqual(len(self._roles()), 10)

    def test_every_role_is_used_somewhere(self) -> None:
        orphans = [
            r for r in self._roles()
            if r not in self._COMPOSED
            and r not in self._ATTRIBUTE_CONSUMED
            and not self._consumers(r)
        ]
        self.assertEqual(
            orphans, [],
            f"these Protocols narrow nothing — no module annotates with them:\n"
            f"  {orphans}\n"
            f"Either wire them to their consumer or delete them; an unused "
            f"Protocol reads like a constraint and is not one.",
        )

    def test_composed_roles_really_are_composed(self) -> None:
        """The exemption must be earned, not asserted."""
        from yuyutsava.storage.events.roles import TriageStore

        bases = {b.__name__ for b in TriageStore.__mro__}
        for role in self._COMPOSED:
            with self.subTest(role=role):
                self.assertIn(
                    role, bases,
                    f"{role} is exempted as 'composed into TriageStore' but is "
                    f"not in its MRO — the exemption is stale",
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
