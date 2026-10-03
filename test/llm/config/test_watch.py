"""An edit to ``models.toml`` is in use from the next read, and a bad edit never replaces a good config."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from yuyutsava.llm.config.store import ConfigStore
from yuyutsava.llm.config.watch import ConfigSource, ConfigState, ConfigWatcher

GOOD = """\
version = 1
[defaults]
model = "fast"
[providers.groq]
type = "groq"
auth = { method = "api_key", key = "secret:groq" }
[providers.local]
type = "ollama"
auth = { method = "bearer_token", token = "env:YT_WATCH_TEST_TOKEN" }
[models.fast]
provider = "groq"
id = "llama-3.3-70b-versatile"
"""


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class SourceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.models = self.dir / "models.toml"
        self.secrets = self.dir / "secrets.toml"
        self.source = ConfigSource(self.models, self.secrets)
        self._env = mock.patch.dict("os.environ", {}, clear=False)
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()


class NothingConfigured(SourceTest):
    def test_no_file_means_not_configured(self) -> None:
        state = self.source.reload()
        self.assertFalse(state.configured)
        self.assertIsNone(state.problem)

    def test_removing_the_file_unconfigures(self) -> None:
        self.models.write_text(GOOD, encoding="utf-8")
        self.assertTrue(self.source.reload().configured)
        self.models.unlink()
        self.assertFalse(self.source.reload().configured)


class AValidFile(SourceTest):
    def test_it_becomes_the_first_snapshot_with_its_secrets_resolved(self) -> None:
        self.models.write_text(GOOD, encoding="utf-8")
        self.secrets.write_text('groq = "gsk-1"\n', encoding="utf-8")
        snapshot = self.source.reload().snapshot
        self.assertEqual(snapshot.generation, 1)
        self.assertEqual(snapshot.secrets["secret:groq"].reveal(), "gsk-1")

    def test_a_missing_secret_leaves_the_config_usable_and_says_why(self) -> None:
        self.models.write_text(GOOD, encoding="utf-8")
        state = self.source.reload()
        self.assertTrue(state.configured)
        self.assertIsNone(state.problem)
        issue = state.snapshot.unavailable["env:YT_WATCH_TEST_TOKEN"]
        self.assertEqual(issue.path, "providers.local.auth.token")
        self.assertIn("is not set", issue.problem)

    def test_reading_unchanged_files_publishes_nothing_new(self) -> None:
        self.models.write_text(GOOD, encoding="utf-8")
        first = self.source.reload().snapshot
        heard: list[ConfigState] = []
        self.source.subscribe(heard.append)
        self.assertIs(self.source.reload().snapshot, first)
        self.assertEqual(heard, [])

    def test_a_secret_change_alone_is_a_new_snapshot(self) -> None:
        self.models.write_text(GOOD, encoding="utf-8")
        self.source.reload()
        self.secrets.write_text('groq = "gsk-2"\n', encoding="utf-8")
        snapshot = self.source.reload().snapshot
        self.assertEqual(
            (snapshot.generation, snapshot.secrets["secret:groq"].reveal()), (2, "gsk-2")
        )


class ABadEdit(SourceTest):
    def test_keeps_the_last_good_config_and_says_what_is_wrong(self) -> None:
        self.models.write_text(GOOD, encoding="utf-8")
        good = self.source.reload().snapshot
        self.models.write_text(GOOD.replace('model = "fast"', 'model = "fats"'), encoding="utf-8")
        state = self.source.reload()
        self.assertIs(state.snapshot, good)
        self.assertEqual(state.problem.issues[0].path, "defaults.model")

    def test_fixing_it_clears_the_problem(self) -> None:
        self.models.write_text(GOOD, encoding="utf-8")
        self.source.reload()
        self.models.write_text("version = [", encoding="utf-8")
        self.assertIsNotNone(self.source.reload().problem)
        self.models.write_text(
            GOOD.replace("llama-3.3-70b-versatile", "llama-3.1-8b-instant"), encoding="utf-8"
        )
        state = self.source.reload()
        self.assertIsNone(state.problem)
        self.assertEqual(state.snapshot.generation, 2)

    def test_a_broken_secrets_file_is_a_problem_not_a_silent_loss_of_every_secret(self) -> None:
        self.models.write_text(GOOD, encoding="utf-8")
        self.secrets.write_text("groq = ", encoding="utf-8")
        state = self.source.reload()
        self.assertFalse(state.configured)
        self.assertEqual(state.problem.source, str(self.secrets))


class Listeners(SourceTest):
    def test_each_change_is_announced_and_a_failing_listener_does_not_stop_the_rest(self) -> None:
        heard: list[int] = []

        def broken(state: ConfigState) -> None:
            raise RuntimeError("listener bug")

        self.source.subscribe(broken)
        stop = self.source.subscribe(lambda state: heard.append(state.snapshot.generation))
        self.models.write_text(GOOD, encoding="utf-8")
        with self.assertLogs("yuyutsava.llm.config.watch", level="ERROR"):
            self.source.reload()
        stop()
        self.models.write_text(GOOD + "\n", encoding="utf-8")
        with self.assertLogs("yuyutsava.llm.config.watch", level="ERROR"):
            self.source.reload()
        self.assertEqual(heard, [1])


class TheWatcherFollowsTheFiles(SourceTest):
    def test_a_write_through_the_store_is_picked_up_without_a_reload_call(self) -> None:
        self.models.write_text(GOOD, encoding="utf-8")
        watcher = ConfigWatcher(self.source)
        watcher.start()
        try:
            self.assertEqual(self.source.current().snapshot.generation, 1)
            store = ConfigStore(self.models, self.secrets)

            def to_groq_small(document) -> None:
                document["defaults"]["model"] = "groq/llama-3.1-8b-instant"

            store.apply(to_groq_small, expected=store.read().digest)
            store.store_secret("groq", "gsk-1")
            self.assertTrue(
                _wait_until(
                    lambda: self.source.current().snapshot.config.defaults.model.endswith(
                        "8b-instant"
                    )
                ),
                "the watcher did not pick up the store's write",
            )
            self.assertTrue(
                _wait_until(lambda: "secret:groq" in self.source.current().snapshot.secrets)
            )
        finally:
            watcher.stop()


class TheCheckFails(SourceTest):
    """Negative control: without a reload, a change on disk is not seen."""

    def test_current_does_not_read_the_disk(self) -> None:
        self.models.write_text(GOOD, encoding="utf-8")
        self.source.reload()
        self.models.write_text(GOOD.replace("llama-3.3", "llama-9"), encoding="utf-8")
        self.assertEqual(
            self.source.current().snapshot.config.models["fast"].id, "llama-3.3-70b-versatile"
        )


if __name__ == "__main__":
    unittest.main()
