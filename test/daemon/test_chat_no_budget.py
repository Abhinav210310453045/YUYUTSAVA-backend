"""The conversational bundle carries no BudgetPolicy.

``BudgetPolicy`` accumulates each model call's input tokens for the lifetime of
the middleware and is never reset (``reset()`` has no callers). The daemon chat
bundle is shared across every conversation, so the 120k cap it used to be
built with was crossed within a few calls of any real session — 30-70k input
tokens per call once tool results are in context — and the agent was handed
"Stop calling tools. Summarise what you have done so far" in the middle of a
task, for a reason the user could not see.

Compaction bounds the context instead of bounding the conversation, so it is
the correct control here. The orchestrator keeps its BudgetPolicy, where the
cap is per task and exhausting it genuinely means the task is over-spending —
that asymmetry is the thing worth pinning.

Run:  .venv/bin/python test/daemon/test_chat_no_budget.py
"""

from __future__ import annotations

import inspect
import unittest

import yuyutsava.core.config  # noqa: F401 — import first (core/__init__ cycle)
from yuyutsava.core.agent_profiles import CLI_PROFILE, ORCHESTRATOR_PROFILE, Policy
from yuyutsava.daemon import conversation_manager as cm


class ChatBundleHasNoBudget(unittest.TestCase):
    def test_manager_does_not_pass_budget_tokens(self):
        src = inspect.getsource(cm.ConversationManager._build_master_bundle)
        self.assertNotIn("budget_tokens", src)

    def test_the_env_knob_is_gone(self):
        self.assertFalse(hasattr(cm, "_chat_budget_tokens"))
        self.assertNotIn(
            "YUYUTSAVA_CHAT_BUDGET_TOKENS",
            inspect.getsource(cm.ConversationManager._build_master_bundle),
        )

    def test_removal_is_wiring_not_capability(self):
        # The profile still DECLARES the policy: a standalone CLI or another
        # caller may pass budget_tokens and get budgeting. Dropping it from
        # the profile would disable it everywhere, which is a different and
        # larger decision (ADR-001 keeps the two questions separate).
        self.assertIn(Policy.BUDGET, CLI_PROFILE.policies)
        self.assertIn(Policy.BUDGET, ORCHESTRATOR_PROFILE.policies)


class TinkerBundleUnchanged(unittest.TestCase):
    def test_tinker_build_is_untouched_by_this_change(self):
        src = inspect.getsource(cm.ConversationManager._build_tinker_bundle)
        self.assertNotIn("budget_tokens", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
