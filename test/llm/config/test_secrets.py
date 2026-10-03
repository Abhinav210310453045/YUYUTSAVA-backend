"""A secret reference finds its value, says how to provide it when it cannot, and never shows it."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yuyutsava.llm.config.secrets import (
    Secret,
    SecretRef,
    SecretRefError,
    SecretResolver,
    SecretSource,
    SecretUnavailable,
    parse_secret_ref,
    read_secrets_file,
)


class ParsingAReference(unittest.TestCase):
    def test_each_source(self) -> None:
        self.assertEqual(
            parse_secret_ref("secret:openrouter"), SecretRef(SecretSource.SECRET, "openrouter")
        )
        self.assertEqual(
            parse_secret_ref("env:GROQ_API_KEY"), SecretRef(SecretSource.ENV, "GROQ_API_KEY")
        )
        self.assertEqual(
            parse_secret_ref("file:~/keys/a.txt"), SecretRef(SecretSource.FILE, "~/keys/a.txt")
        )

    def test_it_prints_as_written(self) -> None:
        self.assertEqual(str(parse_secret_ref("env:GROQ_API_KEY")), "env:GROQ_API_KEY")

    def test_a_pasted_key_is_named_as_one(self) -> None:
        with self.assertRaises(SecretRefError) as caught:
            parse_secret_ref("sk-ant-api03-abcdefghijklmnopqrstuvwxyz")
        self.assertIn("pasted in directly", caught.exception.problem)
        self.assertIn("secret:<name>", caught.exception.fix)

    def test_an_unknown_source(self) -> None:
        with self.assertRaises(SecretRefError) as caught:
            parse_secret_ref("vault:groq")
        self.assertEqual(caught.exception.problem, "'vault:groq' is not a reference to a secret")

    def test_a_bad_environment_variable_name(self) -> None:
        with self.assertRaises(SecretRefError) as caught:
            parse_secret_ref("env:GROQ-KEY")
        self.assertIn('e.g. "env:GROQ_API_KEY"', caught.exception.fix)


class ResolvingAReference(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_stored_secret(self) -> None:
        resolver = SecretResolver({"openrouter": "sk-or-1"})
        self.assertEqual(
            resolver.resolve(parse_secret_ref("secret:openrouter")).reveal(), "sk-or-1"
        )

    def test_an_environment_variable(self) -> None:
        with mock.patch.dict("os.environ", {"YT_TEST_KEY": " gsk-1 \n"}):
            value = SecretResolver({}).resolve(parse_secret_ref("env:YT_TEST_KEY"))
        self.assertEqual(value.reveal(), "gsk-1")

    def test_a_file_with_a_trailing_newline(self) -> None:
        key = self.dir / "key.txt"
        key.write_text("tok-1\n", encoding="utf-8")
        value = SecretResolver({}).resolve(parse_secret_ref(f"file:{key}"))
        self.assertEqual(value.reveal(), "tok-1")

    def test_a_missing_stored_secret_says_how_to_store_it(self) -> None:
        with self.assertRaises(SecretUnavailable) as caught:
            SecretResolver({}).resolve(parse_secret_ref("secret:groq"))
        self.assertEqual(caught.exception.problem, "no secret named 'groq' is stored")
        self.assertIn("yuyutsava models secrets set groq", caught.exception.fix)

    def test_an_unset_variable(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(SecretUnavailable) as caught:
                SecretResolver({}).resolve(parse_secret_ref("env:YT_NOT_SET"))
        self.assertEqual(caught.exception.problem, "the environment variable YT_NOT_SET is not set")

    def test_a_missing_or_empty_file(self) -> None:
        empty = self.dir / "empty.txt"
        empty.write_text("\n", encoding="utf-8")
        for ref, problem in (
            (f"file:{self.dir / 'absent.txt'}", "does not exist"),
            (f"file:{empty}", "is empty"),
        ):
            with self.subTest(ref=ref), self.assertRaises(SecretUnavailable) as caught:
                SecretResolver({}).resolve(parse_secret_ref(ref))
            self.assertIn(problem, caught.exception.problem)


class TheSecretsFile(unittest.TestCase):
    def test_a_missing_file_holds_nothing(self) -> None:
        self.assertEqual(
            read_secrets_file(Path(tempfile.gettempdir()) / "yt-absent-secrets.toml"), {}
        )

    def test_only_text_values_are_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secrets.toml"
            path.write_text('# comment\ngroq = "gsk-1"\nport = 5\n', encoding="utf-8")
            self.assertEqual(read_secrets_file(path), {"groq": "gsk-1"})


class AValueNeverShows(unittest.TestCase):
    def test_repr_and_str_hide_it(self) -> None:
        secret = Secret("sk-very-secret")
        self.assertNotIn(
            "sk-very-secret", repr(secret) + str(secret) + f"{secret}" + repr([secret])
        )

    def test_two_secrets_with_the_same_value_are_equal(self) -> None:
        self.assertEqual(Secret("a"), Secret("a"))
        self.assertNotEqual(Secret("a"), Secret("b"))


if __name__ == "__main__":
    unittest.main()
