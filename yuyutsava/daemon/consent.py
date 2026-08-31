"""Tier-1 consent-rule evaluation.

Extracted from the triage loop so the rule-match logic can be reasoned
about independently from the loop's bus subscription + LLM-budget code.
The triage loop now just calls :meth:`ConsentEvaluator.evaluate` and
branches on the typed :class:`ConsentDecision`.

Rules live in ``state.db::consent_rules`` and are loaded fresh on every
call (the table is small; cache invalidation is not worth the bug
surface). Each rule has a topic glob + a JSON predicate over event hints
plus an optional ``expires_ts``. First matching, non-expired rule wins.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from yuyutsava.events.bus import EventEnvelope
from yuyutsava.storage.events import ConsentRule, Store
from yuyutsava.storage.events.roles import ConsentRuleReader

logger = logging.getLogger("yuyutsava.daemon.consent")


@dataclass(frozen=True)
class ConsentDecision:
    """Outcome of evaluating consent rules against one event.

    ``rule is None`` means no match — caller falls back to LLM triage.
    Otherwise ``rule.decision`` is ``"auto_approve"`` or ``"auto_skip"``.
    """

    rule: ConsentRule | None

    @property
    def matched(self) -> bool:
        return self.rule is not None


class ConsentEvaluator:
    """Match an ``EventEnvelope`` against ``state.db::consent_rules``."""

    def __init__(self, store: ConsentRuleReader) -> None:
        self._store = store

    async def evaluate(self, event: EventEnvelope) -> ConsentDecision:
        rules = await self._store.list_consent_rules()
        now = time.time()
        for rule in rules:
            if rule.expires_ts is not None and rule.expires_ts < now:
                continue
            if not fnmatch.fnmatchcase(event.topic, rule.topic_glob):
                continue
            try:
                predicate = json.loads(rule.match_json)
            except Exception:
                continue
            if not self._match_predicate(event, predicate):
                continue
            return ConsentDecision(rule=rule)
        return ConsentDecision(rule=None)

    @staticmethod
    def _match_predicate(event: EventEnvelope, predicate: dict[str, Any]) -> bool:
        # Dotted hint paths: "hints.ext" => event.hints["ext"]
        for key, expected in predicate.items():
            if key == "topic":
                if not fnmatch.fnmatchcase(event.topic, str(expected)):
                    return False
                continue
            if key.startswith("hints."):
                hint_key = key.split(".", 1)[1]
                actual = event.hints.get(hint_key, "")
                if not fnmatch.fnmatchcase(str(actual), str(expected)):
                    return False
                continue
            if key in ("ext", "kind"):
                actual = event.hints.get(key, "")
                if not fnmatch.fnmatchcase(str(actual), str(expected)):
                    return False
                continue
        return True
