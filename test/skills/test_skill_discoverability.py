"""Skills have to be findable, and the macOS pack has to be right.

Thirty-one skills were indexed during the 12 Sep session, including one for
driving Chrome. The agent searched none of them: it re-derived a Chrome
profile lookup and a WhatsApp Core Data schema from web searches over ~26 tool
calls. Two causes, both fixed here — ``sk_search_skill`` sat behind
``tool_search`` (two hops for something the model has to think of in the first
place), and nothing in the prompt told it to look before deriving.

The pack's own facts are pinned too. A skill that confidently states a wrong
column name is worse than no skill, so the WhatsApp entry must keep the two
gotchas that session actually proved: status/channel sessions are not chats,
and pinned chats carry a sentinel date.

Run:  .venv/bin/python test/skills/test_skill_discoverability.py
"""

from __future__ import annotations

import asyncio
import unittest

import yuyutsava.core.config  # noqa: F401 — import first (core/__init__ cycle)
from yuyutsava.core.prompts import _tool_discovery_section
from yuyutsava.core.tool_filter_policy import should_suppress
from yuyutsava.retrieval.hit import Hit
from yuyutsava.skills import injector as skill_injector
from yuyutsava.skills.registry import SkillRegistry

PACK = (
    "macos-open-app-or-url",
    "chrome-profile-by-email",
    "whatsapp-mac-chat-db",
    "macos-app-local-data",
)


class SearchIsReachableInOneMove(unittest.TestCase):
    def test_skill_search_is_always_visible(self):
        self.assertFalse(should_suppress("sk_search_skill"))

    def test_the_rest_of_the_family_still_lazy_loads(self):
        # Only the entry point is exempt; bodies are pulled on demand.
        self.assertTrue(should_suppress("sk_read_skill"))
        self.assertTrue(should_suppress("sk_write_skill"))

    def test_other_prefixes_are_unaffected(self):
        for name in ("tr_write_file", "ws_tavily_search", "todo_add", "um_note"):
            with self.subTest(name=name):
                self.assertTrue(should_suppress(name))
        for name in ("tool_search", "ctx_history", "ctx_fetch_artifact"):
            with self.subTest(name=name):
                self.assertFalse(should_suppress(name))


class PromptTellsItToLook(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules = _tool_discovery_section()

    def test_rules_name_the_tool_and_the_trigger(self):
        self.assertIn("sk_search_skill", self.rules)
        self.assertIn("sk_read_skill", self.rules)
        # The trigger is "before deriving", not "if you happen to think of it".
        self.assertIn("BEFORE you start deriving", self.rules)
        self.assertIn("always available", self.rules)

    def test_reflection_section_survives(self):
        # The new section sits beside the existing write-side rule, which is
        # what keeps the library growing.
        self.assertIn("SKILL REFLECTION", self.rules)
        self.assertIn("sk_write_skill", self.rules)


class RecallIsObservable(unittest.TestCase):
    def test_injection_records_and_logs_the_names(self):
        class FakeStore:
            async def search(self, q, k=5, agent=None):
                return [
                    Hit(id="a", text="read the chat db", score=0.9,
                        payload={"name": "whatsapp-mac-chat-db"}),
                    Hit(id="b", text="open an app", score=0.8,
                        payload={"name": "macos-open-app-or-url"}),
                ]

        inj = skill_injector.SkillInjector(FakeStore())
        with self.assertLogs("yuyutsava.skills.injector", level="INFO") as logs:
            block = asyncio.run(inj.build_block("read my whatsapp chats"))

        self.assertIn("whatsapp-mac-chat-db", block)
        self.assertEqual(
            skill_injector.last_recalled(),
            ("whatsapp-mac-chat-db", "macos-open-app-or-url"),
        )
        self.assertIn("whatsapp-mac-chat-db", logs.records[0].getMessage())

    def test_no_hits_clears_the_record(self):
        class Empty:
            async def search(self, q, k=5, agent=None):
                return []

        inj = skill_injector.SkillInjector(Empty())
        asyncio.run(inj.build_block("something unrelated"))
        self.assertEqual(skill_injector.last_recalled(), ())

    def test_a_store_failure_still_yields_no_block(self):
        class Broken:
            async def search(self, q, k=5, agent=None):
                raise RuntimeError("pgvector down")

        inj = skill_injector.SkillInjector(Broken())
        self.assertEqual(asyncio.run(inj.build_block("x")), "")


class MacosPack(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = SkillRegistry()
        cls.metas = {s.name: s for s in cls.registry.scan(agent="cli")}

    def test_every_skill_is_discovered_for_the_chat_agent(self):
        for name in PACK:
            with self.subTest(name=name):
                self.assertIn(name, self.metas)
                self.assertEqual(self.metas[name].scope, "bundled")

    def test_all_are_tagged_macos_only(self):
        # Untagged, a Chrome-on-macOS playbook would surface on Windows.
        for name in PACK:
            with self.subTest(name=name):
                self.assertEqual(self.metas[name].platforms, ("macos",))

    def test_descriptions_carry_the_trigger_phrases_a_search_would_use(self):
        # The description is what semantic search matches against, so the
        # user's own words have to appear in it.
        self.assertIn("profile", self.metas["chrome-profile-by-email"].description.lower())
        self.assertIn("whatsapp", self.metas["whatsapp-mac-chat-db"].description.lower())
        for name in PACK:
            with self.subTest(name=name):
                self.assertGreater(len(self.metas[name].description), 80)

    def test_chrome_gotcha_is_the_one_that_actually_bit(self):
        body = self.registry.get_body("macos-open-app-or-url")
        # `open -a … --args` is ignored when Chrome is already running: the
        # command returned 0 and nothing happened, twice, in that session.
        self.assertIn("--profile-directory", body)
        self.assertIn("ALREADY RUNNING", body)
        self.assertIn("watch?v=", body)  # a channel page plays nothing

    def test_chrome_profile_skill_names_the_real_source_of_truth(self):
        body = self.registry.get_body("chrome-profile-by-email")
        self.assertIn("Local State", body)
        self.assertIn("info_cache", body)
        self.assertIn("user_name", body)

    def test_whatsapp_schema_matches_what_the_session_proved(self):
        body = self.registry.get_body("whatsapp-mac-chat-db")
        for fact in (
            "group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite",
            "ZWACHATSESSION",
            "ZWAMESSAGE",
            "ZLASTMESSAGE",
            "ZPARTNERNAME",
            "ZUNREADCOUNT",
            "978307200",          # Core Data epoch offset
            "@s.whatsapp.net",
            "@g.us",
        ):
            with self.subTest(fact=fact):
                self.assertIn(fact, body)

    def test_whatsapp_skill_keeps_both_gotchas_and_the_copy_rule(self):
        body = self.registry.get_body("whatsapp-mac-chat-db")
        # The user's actual complaint: status/channel rows listed as chats.
        self.assertIn("Status updates, channels", body)
        # Pinned chats carry a 12-digit sentinel date.
        self.assertIn("sentinel", body)
        # Never query the live database the app holds open.
        self.assertIn("SANDBOX", body)
        self.assertIn("-wal", body)

    def test_generic_app_data_skill_points_at_the_specific_one(self):
        body = self.registry.get_body("macos-app-local-data")
        self.assertIn("whatsapp-mac-chat-db", body)
        self.assertIn("Full Disk Access", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
