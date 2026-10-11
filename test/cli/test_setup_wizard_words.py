"""What the setup wizard says, word for word — for someone who has never seen YUYUTSAVA.

A scripted run through every kind of step records each line said and each question
asked, with its choices, into one transcript. The transcript is kept in
``fixtures/setup_wizard_transcript.txt``: a change to the wording shows up as a
diff to read, never by accident. Regenerate it after a deliberate change with::

    YUYUTSAVA_WRITE_TRANSCRIPT=1 .venv/bin/python -m pytest test/cli/test_setup_wizard_words.py

The run also must not ask what YUYUTSAVA can decide itself, nor show its own
internal words without saying what they mean.
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any

from yuyutsava.cli.setup_wizard.flow import Services, SetupWizard
from yuyutsava.cli.setup_wizard.prompter import Option
from yuyutsava.llm.config.detect import Finding
from yuyutsava.llm.config.leftovers import Leftover
from yuyutsava.llm.config.store import ConfigStore
from yuyutsava.llm.discovery import Availability, DiscoveredModel, Discovery

TRANSCRIPT = Path(__file__).parent / "fixtures" / "setup_wizard_transcript.txt"


class _Recorder:
    """Answers from a script, and writes down everything it is shown."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.lines: list[str] = []
        self.questions: list[str] = []

    def _next(self, question: str, options: list[Option] = ()) -> Any:
        self.questions.append(question)
        self.lines.append(f"? {question}")
        for number, option in enumerate(options, 1):
            hint = f"   — {option.hint}" if option.hint else ""
            self.lines.append(f"    {number}. {option.label}{hint}")
        answer = self.answers.pop(0)
        return answer(options) if callable(answer) else answer

    def say(self, text: str = "", *, tone: str = "") -> None:
        self.lines.append(text)

    def choose(self, question, options, *, default=None):
        return self._next(question, list(options))

    def choose_many(self, question, options, *, chosen=()):
        return self._next(question, list(options))

    def ask(self, question, *, default=""):
        answer = self._next(f"{question} [{default}]" if default else question)
        return answer or default

    def secret(self, question):
        return self._next(f"{question} (hidden as you type)")

    def confirm(self, question, *, default=True):
        return self._next(f"{question} [{'yes' if default else 'no'}]")


def _lists(access) -> Discovery:
    models = (
        (
            DiscoveredModel(
                id="gemini-3.5-flash", availability=Availability.AVAILABLE, context_window=1_048_576
            ),
            DiscoveredModel(
                id="gemini-3.5-flash-lite",
                availability=Availability.AVAILABLE,
                context_window=1_048_576,
            ),
            DiscoveredModel(id="claude-sonnet-4-6", availability=Availability.NEEDS_QUOTA),
        )
        if access.connection.type == "vertex"
        else (
            DiscoveredModel(
                id="llama-3.3-70b-versatile",
                availability=Availability.AVAILABLE,
                context_window=131_072,
            ),
        )
    )
    return Discovery(access.provider, models=models)


FOUND = [
    Finding(
        "vertex",
        "vertex",
        "what YUYUTSAVA uses today: gemini-3.5-flash, set in ~/project/.env (LLM_PROVIDER=vertex)",
        {
            "type": "vertex",
            "project": "my-project",
            "location": "global",
            "auth": {"method": "adc"},
        },
        model="gemini-3.5-flash",
        model_settings={"thinking": {"budget": 8192}},
    ),
    Finding(
        "openrouter",
        "openrouter",
        "the key in your OPENROUTER_API_KEY setting",
        {"type": "openrouter", "auth": {"method": "api_key", "key": "env:OPENROUTER_API_KEY"}},
    ),
]


def _run() -> _Recorder:
    script = _Recorder(
        lambda options: [options[0].value, options[1].value],  # both found
        True,  # save the OpenRouter key
        True,  # connect another provider too
        "groq",  # which provider
        "gsk-1",  # its key
        False,  # not one more
        lambda options: options[0].value,  # the model: what is used today
        False,  # not for everything
        ["triage"],  # one job on another model
        lambda options: next(o.value for o in options if "lite" in o.label),
        False,  # leave the .env as it is
    )
    with tempfile.TemporaryDirectory() as tmp:
        store = ConfigStore(Path(tmp, "models.toml"), Path(tmp, "secrets.toml"))
        wizard = SetupWizard(
            script,
            Services(
                store=store,
                detect=lambda: list(FOUND),
                discover=_lists,
                environ={"OPENROUTER_API_KEY": "sk-or-1"},
                leftovers=lambda: [
                    Leftover("LLM_PROVIDER", "setting", (Path("~/project/.env"),)),
                    Leftover("VERTEX_MODEL", "setting", (Path("~/project/.env"),)),
                ],
            ),
        )
        assert wizard.setup()
    script.lines = [line.replace(tmp, "<home>") for line in script.lines]
    return script


class WhatTheWizardSays(unittest.TestCase):
    def test_is_the_reviewed_transcript(self) -> None:
        said = "\n".join(_run().lines) + "\n"
        if os.environ.get("YUYUTSAVA_WRITE_TRANSCRIPT") == "1":
            TRANSCRIPT.parent.mkdir(exist_ok=True)
            TRANSCRIPT.write_text(said, encoding="utf-8")
        self.assertEqual(said, TRANSCRIPT.read_text(encoding="utf-8"))

    def test_asks_nothing_yuyutsava_can_decide_itself(self) -> None:
        questions = _run().questions
        self.assertFalse([q for q in questions if q.startswith("Name")])

    def test_shows_none_of_its_own_words_unexplained(self) -> None:
        lines = _run().lines
        internal = re.compile(
            r"\buse which\b|\breference\b|\balias\b|models\.toml decides|\bdaemon\b|"
            r"\b(orchestrator|tier_light|tier_standard|tier_heavy|compaction)\b(?!\))"
        )
        offenders = [line for line in lines if internal.search(line) and "   — " not in line]
        self.assertEqual(offenders, [])
        # A model is offered by its name and provider, never as "<provider>/<id>".
        choices = [line for line in lines if re.match(r"    \d+\. ", line)]
        self.assertFalse([c for c in choices if re.match(r"    \d+\. (vertex|groq)/", c)])


if __name__ == "__main__":
    unittest.main()
