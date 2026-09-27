"""The converse fakes still have the shape of what they stand in for (R-TST-03).

A fake that drifts from the real interface lets a socket test pass against
something production no longer is. Each method the fakes implement must exist
on the real class, accept every parameter the real one requires, and invent no
parameter the real one lacks.

Run:  .venv/bin/python -m pytest test/daemon/web/test_fake_conformance.py
"""

from __future__ import annotations

import inspect
import unittest

from test.support.converse import FakeConversation, FakeConversationManager
from yuyutsava.conversation.service import ConversationService
from yuyutsava.daemon.conversation_manager import ConversationManager

PAIRS = (
    (FakeConversationManager, ConversationManager),
    (FakeConversation, ConversationService),
)


def _public_members(cls: type) -> list[str]:
    return [n for n, _ in inspect.getmembers(cls) if not n.startswith("_")]


def _named(sig: inspect.Signature) -> dict[str, inspect.Parameter]:
    return {
        n: p
        for n, p in sig.parameters.items()
        if n != "self" and p.kind not in (p.VAR_KEYWORD, p.VAR_POSITIONAL)
    }


class FakesMatchWhatTheyFake(unittest.TestCase):
    def test_every_member_exists_on_the_real_class(self) -> None:
        for fake, real in PAIRS:
            for name in _public_members(fake):
                with self.subTest(fake=fake.__name__, member=name):
                    self.assertTrue(hasattr(real, name), f"{real.__name__} has no {name}")

    def test_signatures_agree(self) -> None:
        for fake, real in PAIRS:
            for name in _public_members(fake):
                fake_attr = getattr(fake, name)
                real_attr = getattr(real, name, None)
                if not (inspect.isfunction(fake_attr) and inspect.isfunction(real_attr)):
                    continue
                f_sig, r_sig = inspect.signature(fake_attr), inspect.signature(real_attr)
                f_params, r_params = _named(f_sig), _named(r_sig)
                takes_any_kw = any(p.kind is p.VAR_KEYWORD for p in f_sig.parameters.values())
                with self.subTest(fake=fake.__name__, method=name):
                    invented = set(f_params) - set(r_params)
                    self.assertEqual(invented, set(), f"the fake's {name} takes {invented}")
                    required = {n for n, p in r_params.items() if p.default is p.empty}
                    missing = required - set(f_params)
                    self.assertEqual(missing, set(), f"the fake's {name} lacks {missing}")
                    if not takes_any_kw:
                        dropped = set(r_params) - set(f_params)
                        self.assertEqual(
                            dropped, set(), f"the fake's {name} would reject {dropped}"
                        )
                    self.assertEqual(
                        inspect.iscoroutinefunction(fake_attr),
                        inspect.iscoroutinefunction(real_attr),
                        f"{name}: sync/async differs",
                    )


if __name__ == "__main__":
    unittest.main()
