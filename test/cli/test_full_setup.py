"""``yuyutsava setup`` is the whole first run, not just the models half.

The failure it exists for: a first run that finished, said ``Saved``, and left a
chat that could not start because nothing had asked about storage and nothing
could start a container.

What must hold:

* a section's questions come from the catalog, so adding a setting is one entry
  there and no change here;
* nothing on the machine is touched without a yes, and a no is answered with
  the exact command and the reason, never a dead end;
* declining a container does not leave a setting pointing at something that is
  not running — the whole point of the exercise;
* ``yuyutsava models setup`` is unchanged: connecting a provider must not mean
  sitting through questions about containers.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from yuyutsava.cli.setup_wizard.full import SECTIONS, FullSetup, Services
from yuyutsava.cli.setup_wizard.prompter import Option
from yuyutsava.config.checks import Check
from yuyutsava.config.source import settings_source

#: Pick whatever is offered first, whatever it is.
FIRST = object()


class Script:
    """A :class:`Prompter` that answers from a list, in order, and records it all.

    An answer is a value, an option's label, or :data:`FIRST`. Running out of
    answers, or naming an option that is not offered, fails the test.
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

    def say(self, text: str = "", *, tone: str = "") -> None:
        self.said.append(text)

    def choose(self, question: str, options: list[Option], *, default: Any = None) -> Any:
        answer = self._next(question)
        if answer is FIRST:
            return options[0].value
        for option in options:
            if answer in (option.value, option.label):
                return option.value
        raise AssertionError(f"{answer!r} is not one of {[o.label for o in options]}")

    def choose_many(self, question: str, options: list[Option], *, chosen: Any = ()) -> list[Any]:
        return list(self._next(question))

    def ask(self, question: str, *, default: str = "") -> str:
        return self._next(question) or default

    def secret(self, question: str) -> str:
        return self._next(question)

    def confirm(self, question: str, *, default: bool = True) -> bool:
        answer = self._next(question)
        if not isinstance(answer, bool):
            raise AssertionError(f"{question!r} is a yes/no question; got {answer!r}")
        return answer


class Containers:
    """A container service that does what the test says, and records what it was asked."""

    def __init__(self, *, engine_ok: bool = True, starts: bool = True, launchable: bool = True):
        self._engine_ok = engine_ok
        self._starts = starts
        self._launchable = launchable
        self.started: list[str] = []
        self.launched = 0

    def engine(self) -> Check:
        if self._engine_ok:
            return Check("Docker", note="Docker Engine 29.6.1")
        return Check("Docker", problem="its engine is not responding", fix="Start Docker Desktop.")

    def engine_launch_command(self) -> str:
        return "Docker Desktop.exe" if self._launchable else ""

    def launch_engine(self) -> Check:
        self.launched += 1
        self._engine_ok = True
        return self.engine()

    def start(self, service: str) -> Check:
        self.started.append(service)
        if self._starts:
            return Check(service, note="healthy")
        return Check(service, problem="it did not start", fix=f"Run {self.command(service)}.")

    def command(self, service: str) -> str:
        return f"docker compose up -d {service}"

    def purpose(self, service: str) -> str:
        return "does something useful"


class _Setup(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.home = Path(self._dir.name)
        patcher = patch.dict(os.environ, {"YUYUTSAVA_HOME": str(self.home)}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("TAVILY_API_KEY", "EXA_API_KEY", "YUYUTSAVA_STORAGE_BACKEND"):
            os.environ.pop(name, None)
        self.models_ran = 0

    def wizard(
        self,
        ui: Script,
        *,
        containers: Containers | None = None,
        fails: tuple[str, ...] = (),
    ) -> FullSetup:
        def check(name: str, **_options: object) -> Check:
            if name in fails:
                return Check(f"{name} somewhere", problem="it is not answering", fix="Start it.")
            return Check(name)

        def models() -> bool:
            self.models_ran += 1
            return True

        return FullSetup(
            ui,
            Services(
                models=models,
                check=check,
                containers=containers or Containers(),
                source=settings_source,
            ),
        )

    def written(self) -> dict[str, str]:
        return settings_source().values()


class TheSectionsComeFromTheCatalog(_Setup):
    def test_a_section_asks_what_the_catalog_says_and_saves_it(self) -> None:
        ui = Script(True, "tvly-key", False, "", False)  # yes; key; no live check; no exa key
        self.wizard(ui).section("search")
        self.assertEqual(self.written(), {"TAVILY_API_KEY": "tvly-key"})

    def test_a_section_can_be_declined_and_nothing_is_written(self) -> None:
        ui = Script(False)
        self.wizard(ui).section("search")
        self.assertEqual(self.written(), {})

    def test_a_required_setting_is_not_offered_it_is_asked(self) -> None:
        ui = Script("sqlite")  # no "set up storage?" question comes first
        self.wizard(ui).section("storage")
        self.assertEqual(self.written()["YUYUTSAVA_STORAGE_BACKEND"], "sqlite")

    def test_a_section_nobody_has_heard_of_says_so(self) -> None:
        ui = Script()
        self.wizard(ui).section("nothing-like-this")
        self.assertTrue(any("nothing-like-this" in line for line in ui.said))

    def test_every_section_it_offers_is_a_real_one(self) -> None:
        from yuyutsava.config.catalog import group

        for name in SECTIONS:
            if name == "models":
                continue
            with self.subTest(section=name):
                self.assertTrue(group(name).vars)


class NothingHappensWithoutAYes(_Setup):
    def test_a_key_is_never_checked_for_real_unless_asked(self) -> None:
        asked: list[dict[str, object]] = []

        def check(name: str, **options: object) -> Check:
            asked.append({"name": name, **options})
            return Check(name)

        ui = Script(True, "tvly-key", False, "", False)
        setup = self.wizard(ui)
        setup.s.check = check
        setup.section("search")
        self.assertNotIn(True, [call.get("live") for call in asked], "a credit was spent unasked")

    def test_the_question_says_a_credit_is_spent(self) -> None:
        ui = Script(True, "tvly-key", False, "", False)
        self.wizard(ui).section("search")
        self.assertTrue(
            any("credit" in question for question in ui.questions),
            "a check that costs must say so before it is offered",
        )

    def test_a_container_is_started_only_when_asked_for(self) -> None:
        containers = Containers()
        ui = Script("postgres", False, False)  # postgres; don't start it; don't fall back
        self.wizard(ui, containers=containers, fails=("postgres",)).section("storage")
        self.assertEqual(containers.started, [])

    def test_docker_desktop_is_started_only_when_asked_for(self) -> None:
        containers = Containers(engine_ok=False)
        # postgres; no, don't launch Docker; no, I won't start it myself either;
        # no, don't fall back to sqlite.
        ui = Script("postgres", False, False, False)
        self.wizard(ui, containers=containers, fails=("postgres",)).section("storage")
        self.assertEqual(containers.launched, 0)
        self.assertEqual(containers.started, [])

    def test_saying_yes_starts_the_engine_then_the_container(self) -> None:
        containers = Containers(engine_ok=False)
        ui = Script("postgres", True, True)  # postgres; launch Docker; start postgres
        self.wizard(ui, containers=containers, fails=("postgres",)).section("storage")
        self.assertEqual(containers.launched, 1)
        self.assertEqual(containers.started, ["postgres"])


class ANoIsAnsweredWithTheCommand(_Setup):
    def test_declining_names_the_exact_command(self) -> None:
        ui = Script("postgres", False, False)
        setup = self.wizard(ui, fails=("postgres",))
        setup.section("storage")
        setup._summary()
        self.assertTrue(
            any("docker compose up -d postgres" in line for line in setup.ui.said),
            "a no must leave the person with the command",
        )

    def test_declining_does_not_leave_a_setting_pointing_at_nothing(self) -> None:
        # The whole failure this wizard exists for: Postgres chosen, Postgres
        # not running, so `yuyutsava chat` dies on connection timeout.
        ui = Script("postgres", False, True)  # postgres; don't start it; yes, use sqlite for now
        self.wizard(ui, fails=("postgres",)).section("storage")
        self.assertEqual(self.written()["YUYUTSAVA_STORAGE_BACKEND"], "sqlite")

    def test_keeping_the_broken_choice_is_allowed_and_warned_about(self) -> None:
        ui = Script("postgres", False, False)  # postgres; don't start; no, keep postgres
        setup = self.wizard(ui, fails=("postgres",))
        setup.section("storage")
        self.assertEqual(self.written()["YUYUTSAVA_STORAGE_BACKEND"], "postgres")
        setup._summary()
        self.assertTrue(any("is not running" in line for line in setup.ui.said))


class TheWholeRun(_Setup):
    def test_it_asks_where_settings_go_before_anything_is_written(self) -> None:
        ui = Script(FIRST, False, False, "sqlite", False)
        self.wizard(ui).run()
        self.assertIn(
            "Where should YUYUTSAVA keep these settings?",
            ui.questions,
            "a person chooses the file before anything is put in one",
        )

    def test_one_section_can_be_run_again_on_its_own(self) -> None:
        ui = Script(True, "tvly-key", False, "", False)
        self.wizard(ui).run(["search"])
        self.assertNotIn("Where should YUYUTSAVA keep these settings?", ui.questions)
        self.assertEqual(self.models_ran, 0, "re-running one section must not redo the models")

    def test_the_models_section_runs_the_models_wizard_unchanged(self) -> None:
        ui = Script()
        self.assertTrue(self.wizard(ui).run(["models"]))
        self.assertEqual(self.models_ran, 1)

    def test_it_says_where_it_wrote_and_what_is_still_not_running(self) -> None:
        ui = Script(True, "tvly-key", False, "", False)
        setup = self.wizard(ui)
        setup.run(["search"])
        self.assertTrue(any(str(self.home) in line for line in setup.ui.said))


class EveryQuestionNamesWhatItIsAbout(_Setup):
    """Not "that file" or "it" — the path, the container, the variable."""

    VAGUE = ("that file", "this file", "those files", "that one")

    def test_no_question_points_at_something_unnamed(self) -> None:
        ui = Script(FIRST, False, True, "", "sqlite", False)
        setup = self.wizard(ui)
        setup.run()
        for line in setup.ui.questions + setup.ui.said:
            for vague in self.VAGUE:
                with self.subTest(line=line[:60]):
                    self.assertNotIn(vague, line.lower(), "name the file, do not point at it")


if __name__ == "__main__":
    unittest.main()
