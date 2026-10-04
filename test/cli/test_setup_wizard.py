"""The setup wizard, driven by scripts of answers — and its terminal prompter by a fake terminal.

Nothing calls a provider: listing models is a double that answers as a provider
would, so every branch — found logins, a provider typed by hand, a login that
fails — runs here.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

from yuyutsava.cli.setup_wizard.flow import Services, SetupWizard
from yuyutsava.cli.setup_wizard.prompter import Option, TerminalPrompter
from yuyutsava.llm.config.detect import Finding
from yuyutsava.llm.config.loader import load_text
from yuyutsava.llm.config.store import ConfigStore
from yuyutsava.llm.discovery import Availability, DiscoveredModel, Discovery


def FIRST_OPTION(options):  # noqa: N802 — reads as the script step it is
    return options[0].value


class Script:
    """A :class:`Prompter` that answers from a list, in order, and records what it was told.

    A step is a value to return, an option label to pick, or a function of the
    options. Running out of answers, or a label no option has, fails the test.
    """

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.said: list[str] = []
        self.questions: list[str] = []

    def _next(self, question: str) -> Any:
        self.questions.append(question)
        if not self.answers:
            raise AssertionError(f"no answer scripted for: {question}")
        return self.answers.pop(0)

    def _pick(self, answer: Any, options: list[Option]) -> Any:
        if callable(answer):
            return answer(options)
        for option in options:
            if answer in (option.value, option.label):
                return option.value
        raise AssertionError(f"{answer!r} is not one of {[o.label for o in options]}")

    def say(self, text: str = "", *, tone: str = "") -> None:
        self.said.append(text)

    def choose(self, question, options, *, default=None):
        return self._pick(self._next(question), list(options))

    def choose_many(self, question, options, *, chosen=()):
        answer = self._next(question)
        return (
            answer(list(options))
            if callable(answer)
            else [self._pick(a, list(options)) for a in answer]
        )

    def ask(self, question, *, default=""):
        return self._next(question) or default

    def secret(self, question):
        return self._next(question)

    def confirm(self, question, *, default=True):
        answer = self._next(question)
        if not isinstance(answer, bool):
            raise AssertionError(f"{question!r} wants yes or no, the script says {answer!r}")
        return answer


def _lists(*ids: str, problem: str = "") -> Callable[[Any], Discovery]:
    def discover(access):
        if problem:
            return Discovery(access.provider, problem=problem, fix="do the thing")
        models = tuple(DiscoveredModel(id=i, availability=Availability.AVAILABLE) for i in ids)
        return Discovery(access.provider, models=models)

    return discover


class _Wizard(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.store = ConfigStore(self.dir / "models.toml", self.dir / "secrets.toml")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def wizard(self, script, *, found=(), discover=_lists("m1"), environ=None) -> SetupWizard:
        return SetupWizard(
            script,
            Services(
                store=self.store,
                detect=lambda: list(found),
                discover=discover,
                environ=environ or {},
            ),
        )

    def config(self):
        return load_text(self.store.read().text)

    def secrets(self) -> str:
        path = self.dir / "secrets.toml"
        return path.read_text() if path.exists() else ""


GROQ_FOUND = Finding(
    name="groq",
    type="groq",
    origin="your current setup (LLM_PROVIDER=groq)",
    connection={"type": "groq", "auth": {"method": "api_key", "key": "env:GROQ_API_KEY"}},
    model="llama-3.3-70b-versatile",
)


class AFirstRunWithASetupFound(_Wizard):
    def test_imports_it_keeps_its_key_and_makes_its_model_the_default(self) -> None:
        script = Script(
            lambda options: [options[0].value],  # use what was found
            True,  # keep the key in secrets.toml
            False,  # no provider by hand
            False,  # no other provider
            "groq/llama-3.3-70b-versatile",  # the default
            True,  # for every task
        )
        written = self.wizard(
            script,
            found=[GROQ_FOUND],
            discover=_lists("llama-3.3-70b-versatile", "llama-3.1-8b-instant"),
            environ={"GROQ_API_KEY": "gsk-1"},
        ).setup()
        self.assertTrue(written)
        config = self.config()
        self.assertEqual(config.defaults.model, "groq/llama-3.3-70b-versatile")
        self.assertEqual(config.providers["groq"].auth.key, "secret:groq")
        self.assertIn('groq = "gsk-1"', self.secrets())
        self.assertEqual(config.tasks, {})
        self.assertTrue(any("2 ready" in line for line in script.said))


class SettingsTheModelRunsWithToday(_Wizard):
    def test_are_kept_on_an_alias_offered_as_the_default(self) -> None:
        found = Finding(
            name="vertex",
            type="vertex",
            origin="your current setup (LLM_PROVIDER=vertex)",
            connection={
                "type": "vertex",
                "project": "p",
                "location": "global",
                "auth": {"method": "adc"},
            },
            model="gemini-3.5-flash",
            model_settings={"thinking": {"budget": 1024}},
        )
        script = Script(lambda options: [options[0].value], False, False, FIRST_OPTION, True)
        self.assertTrue(self.wizard(script, found=[found]).setup())
        config = self.config()
        self.assertEqual(config.defaults.model, "gemini-3.5-flash")
        self.assertEqual(config.models["gemini-3.5-flash"].thinking.budget, 1024)


class AProviderByHand(_Wizard):
    def test_asks_only_what_the_type_needs_and_keeps_a_failed_login_on_request(self) -> None:
        script = Script(
            "Google Vertex AI",  # which provider
            "work",  # its name
            "p-1",  # project
            "",  # location: the default
            "adc",  # login
            "keep",  # the login check failed: keep it anyway
            False,  # no other provider
            "",  # the default: type one
            "gemini-3.5-flash",
            False,  # not for every task
            ["triage"],
            "",  # the model for triage: type one
            "gemini-3.5-flash-lite",
        )
        written = self.wizard(script, discover=_lists(problem="no gcloud login")).setup()
        self.assertTrue(written)
        config = self.config()
        work = config.providers["work"]
        self.assertEqual((work.project, work.location, work.auth.method), ("p-1", "global", "adc"))
        self.assertEqual(config.defaults.model, "work/gemini-3.5-flash")
        self.assertEqual(config.tasks, {"triage": "work/gemini-3.5-flash-lite"})
        self.assertTrue(any("no gcloud login" in line for line in script.said))

    def test_a_typed_key_goes_to_secrets_and_the_login_check_can_use_it(self) -> None:
        seen = {}

        def discover(access):
            seen["key"] = access.secret("secret:anthropic")
            return _lists("claude-haiku-4-5")(access)

        script = Script("Anthropic", "", "sk-ant-1", False, "anthropic/claude-haiku-4-5", True)
        self.assertTrue(self.wizard(script, discover=discover).setup())
        self.assertEqual(seen["key"], "sk-ant-1")
        self.assertEqual(self.config().providers["anthropic"].auth.key, "secret:anthropic")
        self.assertNotIn("sk-ant-1", self.store.read().text)

    def test_a_dropped_provider_leaves_nothing_to_write(self) -> None:
        script = Script("Groq", "", "gsk-x", "drop", False)
        self.assertFalse(self.wizard(script, discover=_lists(problem="401")).setup())
        self.assertFalse((self.dir / "models.toml").exists())


class LeavingEarly(_Wizard):
    def test_writes_no_models_toml(self) -> None:
        class Interrupted(Script):
            def choose(self, question, options, *, default=None):
                raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.wizard(Interrupted()).setup()
        self.assertFalse((self.dir / "models.toml").exists())


class AddingToAnExistingFile(_Wizard):
    def test_adds_a_provider_and_leaves_the_default_alone(self) -> None:
        (self.dir / "models.toml").write_text(
            '# mine\nversion = 1\n[defaults]\nmodel = "local/qwen3:8b"\n'
            '[providers.local]\ntype = "ollama"\n',
            encoding="utf-8",
        )
        script = Script("Groq", "", "gsk-x", False)
        self.assertTrue(self.wizard(script).add())
        config = self.config()
        self.assertEqual(set(config.providers), {"local", "groq"})
        self.assertEqual(config.defaults.model, "local/qwen3:8b")
        self.assertIn("# mine", self.store.read().text)

    def test_a_taken_name_gets_another(self) -> None:
        (self.dir / "models.toml").write_text(
            'version = 1\n[defaults]\nmodel = "groq/x"\n[providers.groq]\ntype = "ollama"\n',
            encoding="utf-8",
        )
        script = Script("Groq", "groq", "gsk-x", False)
        self.assertTrue(self.wizard(script).add())
        self.assertIn("groq-2", self.config().providers)


class TheTerminalPrompter(unittest.TestCase):
    """The prompt_toolkit side, on a fake terminal: keys in, answers out."""

    def _answers(self, keys: str, ask: Callable[[TerminalPrompter], Any]) -> Any:
        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            pipe.send_text(keys)
            with mock.patch.dict(os.environ, {"YUYUTSAVA_THEME": "dark"}):
                return ask(TerminalPrompter())

    def test_choose_moves_with_the_arrow_keys(self) -> None:
        options = [Option("a", "first"), Option("b", "second"), Option("c", "third")]
        picked = self._answers("\x1b[B\r", lambda ui: ui.choose("which?", options))
        self.assertEqual(picked, "b")

    def test_ask_returns_the_default_on_enter(self) -> None:
        self.assertEqual(
            self._answers("\r", lambda ui: ui.ask("where?", default="global")), "global"
        )

    def test_choose_many_reads_numbers(self) -> None:
        options = [Option("a", "first"), Option("b", "second"), Option("c", "third")]
        self.assertEqual(
            self._answers("1,3\r", lambda ui: ui.choose_many("which?", options)), ["a", "c"]
        )

    def test_a_secret_is_read(self) -> None:
        self.assertEqual(self._answers("sk-1\r", lambda ui: ui.secret("key")), "sk-1")


if __name__ == "__main__":
    unittest.main()
