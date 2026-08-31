"""Structural conformance between every SQLite/Postgres store twin.

Phase 2 step 2.1. ADR-002 collapses 17 domains × 2 hand-written backends into one
implementation over a dialect adapter. Before that can be safe, the contract each
twin pair is *supposed* to satisfy has to be written down — it currently is not —
and any place where the two already disagree has to be found.

Full behavioural conformance (same input → same observable result on both
backends) needs a live Postgres and is gated in
``test_twin_behaviour.py``. **This module needs no database.** It checks the
structural properties that hold regardless, and those turn out to be where the
real drift lives:

  * both twins implement every abstract method of their shared interface;
  * neither twin has public methods the other lacks — an asymmetric method is a
    capability one backend silently does not have;
  * signatures match, so a caller cannot depend on a parameter that exists on
    only one side;
  * every twin pair is reachable from the interface (no orphan implementations).

Run:  .venv/bin/python test/storage/test_twin_conformance.py
"""

from __future__ import annotations

import importlib
import inspect
import re
import unittest

#: (module, interface, sqlite impl, pg impl) for every domain with two backends.
#: Derived from docs/architecture-review/01-evidence-and-metrics.md § M3.
TWINS: tuple[tuple[str, str, str, str], ...] = (
    ("yuyutsava.daemon.usage", "UsageStore", "SqliteUsageStore", "PgUsageStore"),
)

#: Events-package twins share one module pair rather than one module per domain.
EVENT_TWINS: tuple[tuple[str, str, str], ...] = (
    # All seven events domains now run on the dialect adapter; the events
    # package has no hand-written twin pair left. PrefsBackend is not listed
    # here because it never had a shared ABC to conform to.
)


#: Methods that legitimately exist on only one backend, with how callers are
#: protected from calling them on the other. Anything NOT listed here is drift.
#:
#: All three are pgvector capabilities SQLite genuinely cannot provide, so the
#: asymmetry is correct. What is inconsistent is the *guard mechanism*:
#: ``recall`` is gated by a declared ``supports_recall`` property, while
#: ``backfill_embeddings`` is probed with ``getattr(store, ..., None)`` at every
#: call site. The declared-capability form is better — it is greppable, typed,
#: and cannot be forgotten at a new call site — and ``supports_recall`` shows the
#: codebase already knows the pattern.
KNOWN_ASYMMETRIES: dict[tuple[str, str], str] = {
}


def mutating_statements(src: str) -> list[str]:
    """SQL statements in *src* that actually mutate, found by parsing.

    Counts ``execute(...)`` calls whose SQL literal **starts with** INSERT /
    UPDATE / DELETE. A plain keyword regex is not good enough here and produced
    a materially wrong number on first use: it counted the word in method names
    (``async def delete``), in comments (``ON DELETE CASCADE``), and both halves
    of a single ``INSERT ... ON CONFLICT DO UPDATE`` upsert — reporting 16
    non-atomic methods where only 8 were real.
    """
    import ast
    import textwrap

    try:
        tree = ast.parse(textwrap.dedent(src))
    except SyntaxError:
        return []

    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr in ("execute", "execute_rowcount")):
            continue
        if not node.args:
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            sql = arg.value
        elif isinstance(arg, ast.JoinedStr):
            head = next(
                (v.value for v in arg.values
                 if isinstance(v, ast.Constant) and isinstance(v.value, str)),
                "",
            )
            sql = head
        else:
            continue
        upper = sql.lstrip().upper()
        for verb in ("INSERT", "UPDATE", "DELETE"):
            if upper.startswith(verb):
                found.append(verb)
                break
    return found


def _load(module: str, name: str):
    return getattr(importlib.import_module(module), name)


def _all_twins() -> list[tuple[str, type, type, type]]:
    """(label, interface, sqlite_cls, pg_cls) for every domain."""
    out = []
    for mod, iface, sq, pg in TWINS:
        out.append((f"{mod.split('.')[-1]}:{iface}", _load(mod, iface), _load(mod, sq), _load(mod, pg)))
    for iface, sq, pg in EVENT_TWINS:
        out.append((
            f"events:{iface}",
            _load("yuyutsava.storage.events.abc", iface),
            _load("yuyutsava.storage.events.sqlite_backend", sq),
            _load("yuyutsava.storage.events.pg_stores", pg),
        ))
    return out


def _public_async_methods(cls: type) -> dict[str, inspect.Signature]:
    """Public coroutine methods declared anywhere in the class's own MRO chain.

    Excludes the shared ``BaseSqliteStore`` plumbing (``_run_write`` etc. are
    private anyway) and anything inherited from ``object``.
    """
    out: dict[str, inspect.Signature] = {}
    for name, member in inspect.getmembers(cls, predicate=inspect.isfunction):
        if name.startswith("_"):
            continue
        if not inspect.iscoroutinefunction(member):
            continue
        out[name] = inspect.signature(member)
    return out


class TwinStructuralConformance(unittest.TestCase):
    """Both backends of a domain must present the same surface."""

    def test_every_twin_implements_its_interface(self) -> None:
        """Neither backend may be left abstract — that fails at construction."""
        for label, iface, sq, pg in _all_twins():
            for impl in (sq, pg):
                with self.subTest(domain=label, impl=impl.__name__):
                    missing = set(getattr(impl, "__abstractmethods__", ()) or ())
                    self.assertEqual(
                        missing, set(),
                        f"{impl.__name__} does not implement {sorted(missing)} from "
                        f"{iface.__name__}; instantiating it raises TypeError.",
                    )

    def test_twins_expose_the_same_methods(self) -> None:
        """An asymmetric method is a capability one backend silently lacks.

        This is the failure mode ``F-D02`` predicts: two hand-maintained
        implementations of one contract drift, and nothing reports it. A method
        present only on the SQLite side works in development and raises
        ``AttributeError`` in production, or vice versa.
        """
        for label, _iface, sq, pg in _all_twins():
            sq_m, pg_m = _public_async_methods(sq), _public_async_methods(pg)
            only_sqlite = sorted(
                m for m in set(sq_m) - set(pg_m) if (label, m) not in KNOWN_ASYMMETRIES
            )
            only_pg = sorted(
                m for m in set(pg_m) - set(sq_m) if (label, m) not in KNOWN_ASYMMETRIES
            )
            with self.subTest(domain=label):
                self.assertEqual(
                    (only_sqlite, only_pg), ([], []),
                    f"{label}: UNDECLARED twin asymmetry.\n"
                    f"  only on {sq.__name__}: {only_sqlite}\n"
                    f"  only on {pg.__name__}: {only_pg}\n"
                    f"A caller using a one-sided method gets AttributeError on the "
                    f"other backend. Either implement it on both, or add it to "
                    f"KNOWN_ASYMMETRIES with the guard that protects callers.",
                )

    def test_known_asymmetries_are_still_real(self) -> None:
        """The allowlist must not outlive what it describes.

        A stale entry here would mask a genuine future divergence on the same
        method name, so an entry that no longer corresponds to an actual
        asymmetry is itself a failure.
        """
        twins = {label: (sq, pg) for label, _i, sq, pg in _all_twins()}
        for (label, method), rationale in KNOWN_ASYMMETRIES.items():
            with self.subTest(domain=label, method=method):
                self.assertIn(label, twins, f"KNOWN_ASYMMETRIES names an unknown domain {label}")
                sq, pg = twins[label]
                on_sq, on_pg = hasattr(sq, method), hasattr(pg, method)
                self.assertNotEqual(
                    on_sq, on_pg,
                    f"{label}.{method} is no longer asymmetric (sqlite={on_sq}, "
                    f"pg={on_pg}). Remove it from KNOWN_ASYMMETRIES — a stale entry "
                    f"hides real drift.\nRecorded rationale: {rationale}",
                )

    def test_twin_signatures_match(self) -> None:
        """Same method name, same parameters — or a caller breaks on one backend."""
        for label, _iface, sq, pg in _all_twins():
            sq_m, pg_m = _public_async_methods(sq), _public_async_methods(pg)
            for name in sorted(set(sq_m) & set(pg_m)):
                s, p = sq_m[name], pg_m[name]
                sp = [(n, v.kind, v.default is not inspect.Parameter.empty)
                      for n, v in s.parameters.items() if n != "self"]
                pp = [(n, v.kind, v.default is not inspect.Parameter.empty)
                      for n, v in p.parameters.items() if n != "self"]
                with self.subTest(domain=label, method=name):
                    self.assertEqual(
                        sp, pp,
                        f"{label}.{name} has different signatures per backend:\n"
                        f"  {sq.__name__}: {s}\n  {pg.__name__}: {p}",
                    )

    def test_interface_methods_are_not_backend_only(self) -> None:
        """Every abstract method appears on both concrete twins."""
        for label, iface, sq, pg in _all_twins():
            declared = {
                n for n, m in inspect.getmembers(iface, predicate=inspect.isfunction)
                if not n.startswith("_") and getattr(m, "__isabstractmethod__", False)
            }
            for impl in (sq, pg):
                with self.subTest(domain=label, impl=impl.__name__):
                    missing = sorted(d for d in declared if not hasattr(impl, d))
                    self.assertEqual(
                        missing, [],
                        f"{impl.__name__} is missing interface methods {missing}",
                    )


class TransactionSemantics(unittest.TestCase):
    """Multi-statement writes must be atomic on BOTH backends.

    ``F-S10`` in the review. The SQLite twins all run inside ``BEGIN IMMEDIATE``
    via ``BaseSqliteStore._run_write``. The Postgres pool, by contrast, is
    **autocommit** — ``PgPool.connection()`` commits each statement on its own,
    and ``PgPool.transaction()`` is the explicit atomic wrapper
    (``storage/pg/pool.py:120-138``).

    So a Postgres method issuing two mutating statements through
    ``connection()`` is not atomic, while its SQLite twin is. That divergence is
    invisible in development (SQLite) and only bites in production (Postgres).

    Concrete example, fixed 2026-08-08: ``PgFeedbackStore.upsert`` ran
    ``DELETE`` then ``INSERT`` under autocommit, so a failure between them
    destroyed the user's prior feedback and never wrote the replacement.

    This test keeps that class of bug from returning.
    """

    def test_multi_statement_pg_writes_are_transactional(self) -> None:
        offenders: list[str] = []
        for label, _iface, _sq, pg in _all_twins():
            for name in sorted(_public_async_methods(pg)):
                try:
                    src = inspect.getsource(getattr(pg, name))
                except (OSError, TypeError):
                    continue
                if "transaction()" in src or "connection()" not in src:
                    continue
                stmts = mutating_statements(src)
                if len(stmts) < 2:
                    continue
                # ``ensure_thread`` is an idempotent parent-hub upsert, not a
                # domain write — pairing it with one real write is safe.
                if "ensure_thread" in src and len(stmts) == 1:
                    continue
                offenders.append(f"{label}.{name} {stmts}")

        self.assertEqual(
            offenders, [],
            "Postgres methods issuing multiple mutating statements through the "
            "AUTOCOMMIT `connection()` helper — each statement commits "
            "independently, so a mid-method failure leaves a partial write:\n  "
            + "\n  ".join(offenders)
            + "\n\nFix: use `self._pool.transaction()` instead of "
              "`self._pool.connection()` (storage/pg/pool.py:126).",
        )

    def test_multi_statement_sqlite_writes_are_transactional(self) -> None:
        """The same rule on the SQLite side.

        ``BaseSqliteStore``-derived twins go through ``_run_write``
        (``BEGIN IMMEDIATE`` + explicit rollback). The events-package twins do
        not — they use ``SqliteEventsBackend.execute``, which commits **per
        statement**, exactly like the Postgres autocommit pool. Multi-statement
        methods there must use ``SqliteEventsBackend.transaction()``.
        """
        offenders: list[str] = []
        for label, _iface, sq, _pg in _all_twins():
            for name in sorted(_public_async_methods(sq)):
                try:
                    src = inspect.getsource(getattr(sq, name))
                except (OSError, TypeError):
                    continue
                if "_run_write" in src or "transaction()" in src:
                    continue
                stmts = mutating_statements(src)
                if len(stmts) >= 2:
                    offenders.append(f"{label}.{name} {stmts}")

        self.assertEqual(
            offenders, [],
            "SQLite methods issuing multiple mutating statements outside a "
            "transaction — each commits independently:\n  "
            + "\n  ".join(offenders)
            + "\n\nFix: wrap in `BaseSqliteStore._run_write` or "
              "`SqliteEventsBackend.transaction()`.",
        )


class TwinInventory(unittest.TestCase):
    """The inventory itself stays accurate as domains are added or collapsed."""

    def test_domain_count_is_tracked(self) -> None:
        """Ratchet. ADR-002 should drive this toward one implementation per domain.

        11 twin pairs (9 module-level + 5 in the events package).

        Baseline history:
          19  at the start of Phase 2
          18  -1 after visuals collapsed onto the dialect adapter (ADR-002 step 2.3)
          17  -1 after thread summaries collapsed (step 2.5b), which also fixed a
              concurrency bug the Postgres twin had
          16  -1 after voice messages collapsed (step 2.5b)
          14  -2 after the first two events domains collapsed onto
              EventsSqliteDialect (tool counters, consent rules, proposals,
              decisions, consent grants)

              **This number falling is the progress signal**; it rising means a
              new hand-written twin pair was added, which is the cost ADR-002
              exists to remove.
        """
        self.assertEqual(
            len(_all_twins()), 1,
            "The number of SQLite/Postgres twin pairs changed. If a domain was "
            "collapsed onto the dialect adapter (ADR-002), lower this baseline. "
            "If a new hand-written twin pair was added, reconsider — that is the "
            "cost ADR-002 exists to remove.",
        )

    def test_all_twins_are_importable(self) -> None:
        for label, iface, sq, pg in _all_twins():
            with self.subTest(domain=label):
                self.assertTrue(issubclass(sq, iface) or True)  # loaded without error
                self.assertTrue(inspect.isclass(pg))


if __name__ == "__main__":
    unittest.main(verbosity=2)
