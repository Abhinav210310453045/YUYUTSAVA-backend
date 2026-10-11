"""A Vertex gcloud login is looked up once per process, not once per model built.

``google.auth.default()`` runs ``gcloud config get-value project`` as a
subprocess each time (~0.25 s); a chat's start built three models, so three.
A new login (the file changes) is still picked up.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from yuyutsava.llm.config.loader import load_text
from yuyutsava.llm.providers.vertex import google_credentials
from yuyutsava.llm.target import access_for

CONFIG = load_text(
    'version = 1\n[defaults]\nmodel = "v/gemini-3.5-flash"\n'
    '[providers.v]\ntype = "vertex"\nproject = "p"\nlocation = "global"\n'
    'auth = { method = "adc" }\n'
)


class AGcloudLogin(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.adc = Path(self._tmp.name, "application_default_credentials.json")
        self.adc.write_text("{}")
        patch = mock.patch.dict(os.environ, {"CLOUDSDK_CONFIG": self._tmp.name}, clear=False)
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
        self.access = access_for(CONFIG, "v", secrets={}, unavailable={})

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_is_looked_up_once_for_every_model(self) -> None:
        login = object()
        with mock.patch("google.auth.default", return_value=(login, None)) as lookup:
            first = google_credentials(self.access)
            second = google_credentials(self.access)
        self.assertIs(first, login)
        self.assertIs(second, login)
        self.assertEqual(lookup.call_count, 1)

    def test_a_new_login_is_picked_up(self) -> None:
        with mock.patch("google.auth.default", side_effect=[(object(), None), (object(), None)]):
            before = google_credentials(self.access)
            later = time.time() + 5
            os.utime(self.adc, (later, later))  # `gcloud auth application-default login` again
            after = google_credentials(self.access)
        self.assertIsNot(before, after)


if __name__ == "__main__":
    unittest.main()
