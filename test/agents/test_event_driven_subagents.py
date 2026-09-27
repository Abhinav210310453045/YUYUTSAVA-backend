"""The two subagents a timeline event wakes, and what they inherit.

`R-DRY-05`: `face-watcher` and `file-organizer` each wrote the same
constructor and the same one-line `extra_tools`. `EventDrivenSubAgent` holds
both now; what the two still disagree about is whether they learn skills by
default, and both answers had to survive the merge — hence
`can_write_skills=None` meaning "use mine" rather than a signature default that
only one of them could have.

Worth knowing while reading this: the daemon passes `can_write_skills=True`
explicitly to *both*, so the face watcher's own answer is currently overridden
at the only place it is constructed. That is recorded in `R-DOC-01`, not fixed
here — whether a purely reactive agent should learn is not a refactor's call.
"""

from __future__ import annotations

import unittest

from test.support.paths import REPO
from yuyutsava.agents.event_driven import EventDrivenSubAgent
from yuyutsava.agents.face_watcher.agent import FaceWatcherAgent
from yuyutsava.agents.file_organizer.agent import FileOrganizerAgent
from yuyutsava.agents.task_runner.agent import TaskRunnerAgent


def _runner() -> TaskRunnerAgent:
    workspace = (REPO / "workspace").resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    return TaskRunnerAgent(
        workspace_root=workspace, sandbox_root=(workspace / "_sandbox").resolve()
    )


class _Store:
    """Only ever handed to make_fetch_event_tool, which keeps the reference."""


AGENTS = (FaceWatcherAgent, FileOrganizerAgent)


class BothAreEventDriven(unittest.TestCase):
    def test_each_derives_from_the_shared_base(self) -> None:
        for cls in AGENTS:
            with self.subTest(agent=cls.__name__):
                self.assertTrue(issubclass(cls, EventDrivenSubAgent))

    def test_each_can_read_what_woke_it(self) -> None:
        """Without fetch_event the agent is handed an event id it cannot
        resolve, which is the whole reason these two take a store."""
        for cls in AGENTS:
            with self.subTest(agent=cls.__name__):
                agent = cls(_runner(), _Store())
                names = [t.name for t in agent.extra_tools()]
                self.assertIn("fetch_event", names)

    def test_neither_redeclares_the_constructor(self) -> None:
        for cls in AGENTS:
            with self.subTest(agent=cls.__name__):
                self.assertNotIn("__init__", vars(cls))
                self.assertNotIn("extra_tools", vars(cls))


class TheyDisagreeAboutLearning(unittest.TestCase):
    """The one difference the merge had to keep."""

    def test_the_file_organizer_learns_by_default(self) -> None:
        agent = FileOrganizerAgent(_runner(), _Store())
        self.assertTrue(agent._can_write_skills)

    def test_the_face_watcher_does_not(self) -> None:
        """It identifies a face; there is no lesson to generalise from that."""
        agent = FaceWatcherAgent(_runner(), _Store())
        self.assertFalse(agent._can_write_skills)

    def test_an_explicit_answer_wins_for_either(self) -> None:
        """Which is what the daemon does, for both."""
        self.assertTrue(
            FaceWatcherAgent(_runner(), _Store(), can_write_skills=True)._can_write_skills
        )
        self.assertFalse(
            FileOrganizerAgent(_runner(), _Store(), can_write_skills=False)._can_write_skills
        )

    def test_the_defaults_are_declared_not_hidden_in_a_signature(self) -> None:
        self.assertFalse(FaceWatcherAgent.learns_by_default)
        self.assertTrue(FileOrganizerAgent.learns_by_default)


class TheyKeepTheirOwnIdentities(unittest.TestCase):
    def test_names_and_prompts_are_their_own(self) -> None:
        face = FaceWatcherAgent(_runner(), _Store())
        organiser = FileOrganizerAgent(_runner(), _Store())
        self.assertEqual(face.name, "face-watcher")
        self.assertEqual(organiser.name, "file-organizer")
        self.assertNotEqual(face.system_prompt, organiser.system_prompt)


if __name__ == "__main__":
    unittest.main()
