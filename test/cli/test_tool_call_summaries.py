"""A tool call's one-line summary names the argument the tool really takes.

`R-DRY-06`. The CLI summarises a call by the first argument its table lists for
that tool. The table is written by hand, and it had drifted: `tr_run_python`
was keyed on `code`/`script` while its parameter is `script_path`, so a script
run showed its reason instead of the script, and `tr_chmod` named a tool that
does not exist. Each task-runner entry is checked against the schemas of the
tools the agent really binds.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from yuyutsava.agents.task_runner.agent import TaskRunnerAgent
from yuyutsava.agents.task_runner.tools import bind_tools
from yuyutsava.cli.render.tool_format import CALL_KEYS, format_call


class TaskRunnerSummaries(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tools = bind_tools(TaskRunnerAgent(Path(self._tmp.name).resolve()), agent_name="gp")
        self.params = {t.name: set(t.args) for t in tools}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_every_entry_is_a_real_tool(self) -> None:
        unknown = sorted(n for n in CALL_KEYS if n.startswith("tr_") and n not in self.params)
        self.assertEqual(unknown, [])

    def test_every_entry_names_one_of_its_tools_parameters(self) -> None:
        wrong = {
            name: keys
            for name, keys in CALL_KEYS.items()
            if name in self.params and not set(keys) & self.params[name]
        }
        self.assertEqual(wrong, {})

    def test_a_script_run_is_summarised_by_its_script(self) -> None:
        line = format_call("tr_run_python", {"script_path": "tidy.py", "reason": "tidy up"})
        self.assertIn("tidy.py", line)


if __name__ == "__main__":
    unittest.main()
