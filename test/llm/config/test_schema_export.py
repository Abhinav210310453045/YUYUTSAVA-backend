"""The JSON Schema editors use accepts and refuses the same files the loader does."""

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path

import jsonschema

from yuyutsava.llm.config.loader import ModelsConfigError, load_text
from yuyutsava.llm.config.schema_export import models_config_json_schema, models_config_reference

VALID = """\
version = 1
[defaults]
model = "work/gemini-3.5-flash"
[providers.work]
type = "vertex"
project = "p"
location = "global"
auth = { method = "service_account", key_file = "~/sa.json" }
[providers.aws]
type = "bedrock"
region = "us-east-1"
auth = { method = "aws_keys", access_key_id = "secret:id", secret_access_key = "env:AWS_SECRET" }
[models.sonnet]
provider = "work"
id = "claude-sonnet-4-6"
location = "global"
thinking = { budget = 4096 }
"""

#: Each is refused by the schema alone — no cross-reference needed to see it.
INVALID = {
    "a login the type does not accept": VALID.replace('method = "aws_keys"', 'method = "adc"'),
    "an unknown provider type": VALID.replace('type = "bedrock"', 'type = "bedrok"'),
    "a misspelt setting": VALID.replace('region = "us-east-1"', 'regoin = "us-east-1"'),
    "a missing required setting": VALID.replace('project = "p"\n', ""),
    "a negative thinking budget": VALID.replace("budget = 4096", "budget = -1"),
}


class TheSchemaAgreesWithTheLoader(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = jsonschema.Draft202012Validator(models_config_json_schema())

    def test_the_schema_is_itself_valid(self) -> None:
        jsonschema.Draft202012Validator.check_schema(models_config_json_schema())

    def test_a_valid_file_passes_both(self) -> None:
        load_text(VALID)
        self.assertEqual(list(self.validator.iter_errors(tomllib.loads(VALID))), [])

    def test_each_invalid_file_is_refused_by_both(self) -> None:
        for name, text in INVALID.items():
            with self.subTest(name=name):
                with self.assertRaises(ModelsConfigError):
                    load_text(text)
                self.assertTrue(list(self.validator.iter_errors(tomllib.loads(text))))

    def test_editors_are_offered_every_provider_type(self) -> None:
        schema_text = str(models_config_json_schema())
        for provider_type in ("vertex", "bedrock", "openrouter", "ollama", "openai_compatible"):
            self.assertIn(f"'{provider_type}'", schema_text)


class TheReferencePage(unittest.TestCase):
    """docs/reference/models-config.md is the schema, as generated — never edited apart."""

    PAGE = Path(__file__).resolve().parents[3] / "docs" / "reference" / "models-config.md"

    def test_is_current(self) -> None:
        self.assertEqual(
            self.PAGE.read_text(encoding="utf-8"),
            models_config_reference(),
            "regenerate it: yuyutsava models schema --markdown > docs/reference/models-config.md",
        )

    def test_names_every_provider_type_and_its_logins(self) -> None:
        page = models_config_reference()
        for kind in ("vertex", "bedrock", "ollama", "openai_compatible", "azure"):
            self.assertIn(f'### `type = "{kind}"`', page)
        self.assertIn('`key = "secret:…"`', page)
        self.assertNotIn("<provider>", page)  # escaped, or Markdown would hide it


if __name__ == "__main__":
    unittest.main()
