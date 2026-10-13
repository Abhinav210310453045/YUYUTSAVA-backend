"""The catalog is the one place a setting is declared, for all three surfaces.

What must hold:

* the wizard, ``yuyutsava config`` and the Settings form read the same entries,
  so adding a setting is one entry and never a second edit somewhere else;
* a setting the wizard asks about can be checked and, where it needs one, can
  name the container that has to be running;
* the web layer's import path still works, because moving the catalog must not
  have broken the Settings form;
* the ``.env.example`` a person reads and this catalog do not disagree about
  what a setting is called.
"""

from __future__ import annotations

import re
import unittest

from test.support.paths import REPO
from yuyutsava.config import checks
from yuyutsava.config.catalog import config_groups, group, group_of, known_keys, variable


class TheCatalog(unittest.TestCase):
    def test_every_setting_is_reachable_by_name(self) -> None:
        for key in known_keys():
            with self.subTest(key=key):
                self.assertEqual(variable(key).key, key)
                self.assertIn(variable(key), group_of(key).vars)

    def test_a_name_it_does_not_know_says_what_to_run(self) -> None:
        with self.assertRaises(KeyError) as caught:
            variable("NOT_A_REAL_SETTING")
        self.assertIn("yuyutsava config list", str(caught.exception))

    def test_group_names_become_table_names_a_person_can_type(self) -> None:
        self.assertEqual(group("Execution & Docker").slug, "execution")
        self.assertEqual(group("search").slug, "search")
        for found in config_groups():
            with self.subTest(group=found.name):
                self.assertRegex(found.slug, r"^[a-z][a-z-]*$")

    def test_slugs_are_unique_so_a_table_belongs_to_one_group(self) -> None:
        slugs = [found.slug for found in config_groups()]
        self.assertEqual(sorted(slugs), sorted(set(slugs)))

    def test_keys_are_unique_so_one_setting_has_one_home(self) -> None:
        keys = [var.key for found in config_groups() for var in found.vars]
        self.assertEqual(sorted(keys), sorted(set(keys)))


class WhatTheWizardNeeds(unittest.TestCase):
    def test_diagrams_are_in_it_at_all(self) -> None:
        # Kroki was in no catalog, no Settings form and no wizard, so a user
        # could only find it by reading visuals/_kroki.py.
        kroki = variable("YUYUTSAVA_KROKI_URL")
        self.assertEqual(kroki.service, "kroki")
        self.assertTrue(kroki.question)

    def test_every_question_is_a_whole_sentence_not_a_field_label(self) -> None:
        for found in config_groups():
            for var in found.vars:
                if not var.question:
                    continue
                with self.subTest(key=var.key):
                    self.assertGreater(len(var.question.split()), 2, "a label is not a question")
                    self.assertNotEqual(var.question, var.label)

    def test_every_check_a_setting_names_exists(self) -> None:
        for found in config_groups():
            for var in found.vars:
                if var.check:
                    with self.subTest(key=var.key):
                        self.assertIn(var.check, checks.PROBES)

    def test_every_service_a_setting_names_can_be_started(self) -> None:
        from yuyutsava.setup.containers import SERVICES

        for found in config_groups():
            for var in found.vars:
                if var.service:
                    with self.subTest(key=var.key):
                        self.assertIn(var.service, SERVICES)

    def test_anything_asked_for_can_be_checked(self) -> None:
        # A question with no way to tell whether the answer works is how a setup
        # finishes successfully and leaves a chat that cannot start.
        for found in config_groups():
            for var in found.vars:
                if var.question and var.type != "password" and not var.secret:
                    with self.subTest(key=var.key):
                        self.assertTrue(var.check, f"{var.key} is asked but never verified")


class TheWebLayerStillHasIt(unittest.TestCase):
    def test_the_old_import_path_works(self) -> None:
        from yuyutsava.daemon.web.services.config_schema import ConfigVar
        from yuyutsava.daemon.web.services.config_schema import config_groups as old

        self.assertEqual([g.name for g in old()], [g.name for g in config_groups()])
        self.assertIs(ConfigVar, variable("EXA_API_KEY").__class__)

    def test_the_form_can_still_be_built_from_every_entry(self) -> None:
        import dataclasses

        from yuyutsava.daemon.web.schemas.config import ConfigVarDTO

        for found in config_groups():
            for var in found.vars:
                with self.subTest(key=var.key):
                    ConfigVarDTO(**dataclasses.asdict(var))


class TheDocumentedEnvironment(unittest.TestCase):
    def test_it_does_not_invent_names_env_example_has_never_heard_of(self) -> None:
        example = (REPO / ".env.example").read_text(encoding="utf-8")
        documented = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", example, re.M))
        missing = sorted(key for key in known_keys() if key not in documented)
        self.assertEqual(missing, [], ".env.example is what a person reads to learn these names")


if __name__ == "__main__":
    unittest.main()
