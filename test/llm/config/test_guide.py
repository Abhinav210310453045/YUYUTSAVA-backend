"""The setup forms both wizards ask from: shaped by the schema, worded once."""

from __future__ import annotations

import unittest

from yuyutsava.llm.config.guide import provider_form, provider_forms, secret_name
from yuyutsava.llm.config.schema import CONNECTIONS, connection_type


class TheForms(unittest.TestCase):
    def test_one_per_provider_type_in_the_schemas_order(self) -> None:
        self.assertEqual(
            [f.type for f in provider_forms()], [connection_type(c) for c in CONNECTIONS]
        )

    def test_every_type_and_login_is_worded(self) -> None:
        for form in provider_forms():
            with self.subTest(type=form.type):
                self.assertTrue(form.introduction)
                self.assertTrue(all(login.label != login.method for login in form.logins))

    def test_vertex_asks_for_its_project_and_offers_three_logins(self) -> None:
        vertex = provider_form("vertex")
        self.assertEqual(
            [(f.name, f.default) for f in vertex.settings],
            [("project", ""), ("location", "global")],
        )
        self.assertEqual(
            [login.method for login in vertex.logins], ["adc", "service_account", "access_token"]
        )

    def test_a_key_pair_is_two_secrets_and_the_optional_token_is_not_asked(self) -> None:
        keys = next(x for x in provider_form("bedrock").logins if x.method == "aws_keys")
        self.assertEqual(
            [(f.name, f.secret) for f in keys.fields],
            [("access_key_id", True), ("secret_access_key", True)],
        )

    def test_secret_names(self) -> None:
        self.assertEqual(secret_name("groq", "key"), "groq")
        self.assertEqual(secret_name("aws", "secret_access_key"), "aws-secret-access-key")


if __name__ == "__main__":
    unittest.main()
