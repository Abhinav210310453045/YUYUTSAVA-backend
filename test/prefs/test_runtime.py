"""Runtime toggles: what a stored value means, and how a write reaches a reader (R-TST-06).

Voice mode and the subagent deny-list are flipped from the app, the CLI and the
overlay, and read synchronously mid-turn. The rules that must hold: garbage in
the row reads as the defaults, ``general-purpose`` can never be disabled, a
write elsewhere is picked up once the snapshot is stale, and a failed read
never blocks boot.

Run:  .venv/bin/python -m pytest test/prefs/test_runtime.py
"""

from __future__ import annotations

import unittest
from unittest import mock

from yuyutsava.prefs import runtime
from yuyutsava.prefs.runtime import (
    SUBAGENTS_KEY,
    VOICE_KEY,
    RuntimeSettings,
    SubagentSettings,
    VoiceSettings,
)


class _Prefs:
    """An in-memory preferences store; ``fail`` makes every read raise."""

    def __init__(self, rows: dict | None = None, *, fail: bool = False) -> None:
        self.rows = dict(rows or {})
        self.fail = fail

    async def get(self, key, default=None):
        if self.fail:
            raise RuntimeError("store unavailable")
        return self.rows.get(key, default)

    async def set(self, key, value) -> None:
        self.rows[key] = value


class StoredValues(unittest.TestCase):
    def test_voice_defaults_on_and_survives_garbage(self) -> None:
        for raw in (None, "x", 3, [], {"wake_enabled": object()}):
            with self.subTest(raw=raw):
                self.assertEqual(VoiceSettings.from_stored(raw), VoiceSettings())

    def test_voice_reads_loose_booleans(self) -> None:
        v = VoiceSettings.from_stored({"wake_enabled": "off", "tts_enabled": "yes"})
        self.assertEqual((v.wake_enabled, v.tts_enabled), (False, True))
        self.assertTrue(v.enabled)
        self.assertFalse(VoiceSettings(wake_enabled=False, tts_enabled=False).enabled)

    def test_general_purpose_can_never_be_disabled(self) -> None:
        s = SubagentSettings.from_stored({"disabled": ["general-purpose", " web ", "", 7]})
        self.assertEqual(s.disabled, {"web"})
        self.assertTrue(s.is_enabled("general-purpose"))
        self.assertFalse(s.is_enabled("web"))


class TheSnapshot(unittest.IsolatedAsyncioTestCase):
    async def test_a_failed_read_leaves_the_defaults(self) -> None:
        with self.assertLogs("yuyutsava.prefs.runtime", "WARNING"):
            settings = await RuntimeSettings(_Prefs(fail=True)).load()
        self.assertEqual(settings.voice(), VoiceSettings())

    async def test_a_write_elsewhere_arrives_once_the_snapshot_is_stale(self) -> None:
        prefs = _Prefs()
        settings = await RuntimeSettings(prefs).load()
        prefs.rows[VOICE_KEY] = {"wake_enabled": False, "tts_enabled": True}  # another process
        await settings.refresh()
        self.assertTrue(settings.voice().wake_enabled, "re-read before the snapshot went stale")
        with mock.patch.object(runtime, "_TTL_SEC", 0.0):
            await settings.refresh()
        self.assertFalse(settings.voice().wake_enabled)

    async def test_set_voice_patches_only_what_it_is_given(self) -> None:
        prefs = _Prefs({VOICE_KEY: {"wake_enabled": False, "tts_enabled": False}})
        settings = await RuntimeSettings(prefs).load()
        updated = await settings.set_voice(tts_enabled=True)
        self.assertEqual((updated.wake_enabled, updated.tts_enabled), (False, True))
        self.assertEqual(prefs.rows[VOICE_KEY], {"wake_enabled": False, "tts_enabled": True})

    async def test_disabling_is_stored_and_general_purpose_is_refused(self) -> None:
        prefs = _Prefs()
        settings = await RuntimeSettings(prefs).load()
        await settings.set_subagent_enabled("web", False)
        await settings.set_subagent_enabled("general-purpose", False)
        self.assertEqual(prefs.rows[SUBAGENTS_KEY], {"disabled": ["web"]})
        await settings.set_subagent_enabled("web", True)
        self.assertEqual(prefs.rows[SUBAGENTS_KEY], {"disabled": []})

    async def test_the_wire_snapshot(self) -> None:
        settings = await RuntimeSettings(_Prefs()).load()
        self.assertEqual(
            settings.snapshot(),
            {"voice": {"wake_enabled": True, "tts_enabled": True}, "subagents": {"disabled": []}},
        )


if __name__ == "__main__":
    unittest.main()
