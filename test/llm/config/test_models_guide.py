"""docs/guides/models.md: every block in it loads as written, and every provider and every
way of logging in to it has one to copy."""

from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path

from yuyutsava.llm.config.guide import provider_forms
from yuyutsava.llm.config.loader import load_text

GUIDE = Path(__file__).resolve().parents[3] / "docs" / "guides" / "models.md"


def _blocks() -> list[str]:
    return re.findall(r"```toml\n(.*?)```", GUIDE.read_text(encoding="utf-8"), re.S)


def _as_a_file(block: str) -> str:
    """*block* as a whole file: a provider block gains the default a first provider needs."""
    if "version" in block:
        return block
    provider = next(iter(tomllib.loads(block)["providers"]))
    return f'version = 1\n[defaults]\nmodel = "{provider}/m"\n{block}'


class TheModelsGuide(unittest.TestCase):
    def test_every_block_loads(self) -> None:
        blocks = _blocks()
        self.assertGreater(len(blocks), 10)
        for block in blocks:
            with self.subTest(block=block.splitlines()[0]):
                load_text(_as_a_file(block))

    def test_shows_every_provider_with_every_login(self) -> None:
        shown = {
            (connection.type, connection.auth.method)
            for block in _blocks()
            for connection in load_text(_as_a_file(block)).providers.values()
        }
        offered = {(form.type, login.method) for form in provider_forms() for login in form.logins}
        self.assertEqual(offered - shown, set())


if __name__ == "__main__":
    unittest.main()
