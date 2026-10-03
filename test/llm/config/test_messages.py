"""The fixes ``models.toml`` problems suggest are themselves correct.

Every problem the loader reports ends in a line to copy. These tests paste each
of those lines into a file and load it, so a suggested fix can never be one that
fails in turn — and check that no provider type or login method is missing its
explanation.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from yuyutsava.llm.config.loader import load_text
from yuyutsava.llm.config.messages import LOGIN_EXAMPLES, TYPE_LABELS, login_example
from yuyutsava.llm.config.schema import (
    CONNECTIONS,
    connection_type,
    connection_types,
    login_methods,
)

REPO = Path(__file__).resolve().parents[3]

#: The settings each provider type needs besides ``type`` and ``auth``.
_REQUIRED = {
    "vertex": 'project = "p"\nlocation = "global"\n',
    "bedrock": 'region = "us-east-1"\n',
    "azure": 'endpoint = "https://r.openai.azure.com"\n',
    "openai_compatible": 'base_url = "https://api.example.com/v1"\n',
}


def provider_file(provider_type: str, auth_line: str) -> str:
    """A complete ``models.toml`` with one provider of *provider_type*."""
    return (
        'version = 1\n[defaults]\nmodel = "p/some-model"\n'
        f'[providers.p]\ntype = "{provider_type}"\n{_REQUIRED.get(provider_type, "")}{auth_line}\n'
    )


class EveryTypeAndMethodIsExplained(unittest.TestCase):
    def test_every_provider_type_has_a_label(self) -> None:
        self.assertEqual(sorted(TYPE_LABELS), sorted(connection_types()))

    def test_every_login_method_has_an_example(self) -> None:
        methods = {m for cls in CONNECTIONS for m in login_methods(cls)}
        self.assertEqual(sorted(LOGIN_EXAMPLES), sorted(methods))


class EverySuggestedLoginLoads(unittest.TestCase):
    """The ``auth = …`` line suggested for a method works for each type that accepts it."""

    def test_each_example_validates_on_each_type_that_accepts_it(self) -> None:
        for cls in CONNECTIONS:
            kind = connection_type(cls)
            for method in login_methods(cls):
                with self.subTest(type=kind, method=method):
                    config = load_text(provider_file(kind, login_example(method, kind)))
                    self.assertEqual(config.providers["p"].auth.method, method)


class TheDocumentedExampleLoads(unittest.TestCase):
    """The example in the design's §1 is a file that loads, not an illustration."""

    def test_the_section_one_example(self) -> None:
        design = (REPO / "docs" / "design" / "model-config.md").read_text(encoding="utf-8")
        section = design.split("## 1. The file", 1)[1].split("\n## ", 1)[0]
        example = re.search(r"```toml\n(.*?)```", section, re.S).group(1)
        config = load_text(example)
        self.assertEqual(config.defaults.model, "gemini-flash")
        self.assertEqual(config.providers["aws"].auth.method, "aws_profile")


class TheCheckFails(unittest.TestCase):
    """Negative control: a login a type does not accept is refused."""

    def test_a_vertex_api_key_is_refused(self) -> None:
        from yuyutsava.llm.config.loader import ModelsConfigError

        with self.assertRaises(ModelsConfigError):
            load_text(provider_file("vertex", login_example("api_key", "vertex")))


if __name__ == "__main__":
    unittest.main()
