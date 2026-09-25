"""A consent rule whose match cannot be read is named, then skipped.

Consent rules auto-approve or auto-skip events without asking the user. One
with a corrupt ``match_json`` used to be skipped without a word, so a rule the
user created by "approve and remember" could silently stop working. It is
still skipped — an unreadable predicate must never match — but the log says
which rule.
"""

from __future__ import annotations

import time
import unittest

from yuyutsava.daemon.consent import ConsentEvaluator
from yuyutsava.events.bus import EventEnvelope
from yuyutsava.storage.events import ConsentRule


class _Rules:
    def __init__(self, rules: list[ConsentRule]) -> None:
        self._rules = rules

    async def list_consent_rules(self) -> list[ConsentRule]:
        return self._rules


def _rule(rule_id: str, match_json: str) -> ConsentRule:
    return ConsentRule(
        rule_id=rule_id,
        topic_glob="fs.*",
        match_json=match_json,
        decision="auto_skip",
        created_ts=time.time(),
        expires_ts=None,
    )


EVENT = EventEnvelope(
    event_id="e1",
    topic="fs.changed",
    source="fs",
    ts=time.time(),
    severity=1,
    summary="a file",
    payload_ref="",
    hints={"ext": "pdf"},
)


class UnreadableRule(unittest.IsolatedAsyncioTestCase):
    async def test_it_is_named_and_the_next_rule_still_applies(self) -> None:
        good = _rule("good", '{"topic": "fs.*"}')
        evaluator = ConsentEvaluator(_Rules([_rule("broken", "{nope"), good]))
        with self.assertLogs("yuyutsava.daemon.consent", level="WARNING") as logs:
            decision = await evaluator.evaluate(EVENT)
        self.assertIs(decision.rule, good)
        self.assertIn("broken", logs.output[0])

    async def test_it_never_matches(self) -> None:
        evaluator = ConsentEvaluator(_Rules([_rule("broken", "{nope")]))
        with self.assertLogs("yuyutsava.daemon.consent", level="WARNING"):
            self.assertIsNone((await evaluator.evaluate(EVENT)).rule)


if __name__ == "__main__":
    unittest.main()
