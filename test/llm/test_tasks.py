"""The tasks a model can be assigned to are one closed list, plus one entry per subagent."""

from __future__ import annotations

import unittest

from yuyutsava.llm.tasks import TASKS, is_task, task_names


class TheTaskList(unittest.TestCase):
    def test_every_task_has_a_label_and_a_unique_name(self) -> None:
        names = task_names()
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(task.label for task in TASKS))

    def test_known_tasks_and_named_subagents_are_tasks(self) -> None:
        for name in ("chat", "triage", "compaction", "subagent", "subagent.researcher"):
            with self.subTest(name=name):
                self.assertTrue(is_task(name))

    def test_anything_else_is_not(self) -> None:
        for name in ("compation", "subagent.", "file_organizer", ""):
            with self.subTest(name=name):
                self.assertFalse(is_task(name))


if __name__ == "__main__":
    unittest.main()
