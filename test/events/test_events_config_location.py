"""Which sources run is the user's state; the file in the package is only the default.

R-BUG-16. Toggling the wake word or a watched folder rewrote
``yuyutsava/events/events_config.json`` — a tracked file inside the package —
so every toggle dirtied the repository, and every daemon on the machine shared
it whatever its home. Now the live copy lives under the state dir: read from
there once it exists, falling back to the shipped default until the first save
creates it.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yuyutsava.events.config import EventsConfig
from yuyutsava.storage.paths import events_config_path, shipped_events_config_path


class TheLiveCopyIsPerUser(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {"YUYUTSAVA_HOME": str(self.home)})
        self._env.start()
        os.environ.pop("YUYUTSAVA_EVENTS_CONFIG", None)

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def test_it_lives_under_the_state_dir(self) -> None:
        self.assertEqual(events_config_path(), self.home / "events_config.json")

    def test_an_override_wins(self) -> None:
        with mock.patch.dict(os.environ, {"YUYUTSAVA_EVENTS_CONFIG": str(self.home / "x.json")}):
            self.assertEqual(events_config_path(), self.home / "x.json")

    def test_until_the_first_save_the_shipped_default_is_read(self) -> None:
        shipped = json.loads(shipped_events_config_path().read_text())
        self.assertEqual(
            EventsConfig.from_file().to_dict()["sources"].keys(), shipped["sources"].keys()
        )

    def test_a_save_goes_to_the_user_copy_and_leaves_the_package_alone(self) -> None:
        before = shipped_events_config_path().read_bytes()
        cfg = EventsConfig.from_file()
        voice = cfg.sources["voice"]
        toggled = EventsConfig(
            sources={
                **cfg.sources,
                "voice": type(voice)(voice.name, not voice.enabled, voice.params),
            }
        )
        written = toggled.to_file()
        self.assertEqual(written, self.home / "events_config.json")
        self.assertEqual(shipped_events_config_path().read_bytes(), before)
        self.assertEqual(EventsConfig.from_file().sources["voice"].enabled, not voice.enabled)


if __name__ == "__main__":
    unittest.main()
