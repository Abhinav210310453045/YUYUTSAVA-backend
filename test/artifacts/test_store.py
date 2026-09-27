"""The general artifacts store: one directory per artifact, its sidecar the record (R-TST-06).

Chat and voice artifacts land here and the Artifacts gallery lists them. What
must hold: each content kind lands as the block the app renders it with, the
``meta.json`` sidecar round-trips, an attached file keeps its bytes and gets a
kind from its MIME type, an id that could walk out of the store is refused, a
corrupt sidecar is skipped rather than breaking the gallery, and the list is
newest first. Everything runs under a scratch ``YUYUTSAVA_HOME``.

Run:  .venv/bin/python -m pytest test/artifacts/test_store.py
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from yuyutsava.artifacts import store
from yuyutsava.artifacts.store import (
    ArtifactError,
    artifacts_root,
    attach_file,
    create_from_content,
    generate,
    list_records,
    load_record,
)


class _ScratchHome(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(os.environ, {"YUYUTSAVA_HOME": self._tmp.name})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()


class Content(_ScratchHome):
    def test_each_kind_lands_as_the_block_the_app_renders(self) -> None:
        expected = {
            "html": ("artifact", "text/html", ".html"),
            "jsx": ("artifact", "text/jsx", ".jsx"),
            "markdown": ("file", "text/markdown", ".md"),
            "csv": ("file", "text/csv", ".csv"),
            "json": ("file", "application/json", ".json"),
        }
        for content_kind, (kind, mime, ext) in expected.items():
            with self.subTest(content_kind=content_kind):
                rec = create_from_content(content_kind, "x", title="t", thread_id="th")
                self.assertEqual((rec.kind, rec.mime), (kind, mime))
                self.assertTrue(rec.path.endswith(ext))
                self.assertEqual(Path(rec.path).read_text(), "x")
                self.assertEqual(rec.url, f"/artifacts/{rec.artifact_id}")

    def test_the_sidecar_round_trips(self) -> None:
        rec = create_from_content("markdown", "# hi", title="Notes", thread_id="t1")
        again = load_record(rec.artifact_id)
        self.assertEqual(again, rec)
        self.assertEqual(again.meta["content_kind"], "markdown")
        self.assertEqual(again.meta["size"], 4)

    def test_an_unknown_kind_or_empty_content_is_refused(self) -> None:
        with self.assertRaisesRegex(ArtifactError, "unknown content kind 'pdf'"):
            create_from_content("pdf", "x")
        with self.assertRaisesRegex(ArtifactError, "non-empty"):
            create_from_content("html", "")


class Attach(_ScratchHome):
    def test_a_file_is_copied_and_typed_by_its_mime(self) -> None:
        src = Path(self._tmp.name) / "chart.png"
        src.write_bytes(b"\x89PNG fake")
        rec = attach_file(src, meta={"from": "subagent"})
        self.assertEqual((rec.kind, rec.mime), ("image", "image/png"))
        self.assertEqual(Path(rec.path).read_bytes(), b"\x89PNG fake")
        self.assertNotEqual(Path(rec.path).parent, src.parent, "the file was not copied in")
        self.assertEqual(rec.meta["from"], "subagent")

    def test_a_missing_source_is_an_error(self) -> None:
        with self.assertRaisesRegex(ArtifactError, "not found"):
            attach_file(Path(self._tmp.name) / "nope.png")


class Loading(_ScratchHome):
    def test_ids_that_could_leave_the_store_are_refused(self) -> None:
        # A valid sidecar one level above the store root: bait a traversal would load.
        real = create_from_content("text", "x")
        sidecar = artifacts_root() / real.artifact_id / "meta.json"
        (artifacts_root().parent / "meta.json").write_text(sidecar.read_text())
        for bad in ("", "../x", "..", "a/b", "a\\b"):
            with self.subTest(bad=bad):
                self.assertIsNone(load_record(bad))

    def test_a_corrupt_sidecar_is_skipped_not_fatal(self) -> None:
        good = create_from_content("text", "ok")
        broken = artifacts_root() / "01BROKEN"
        broken.mkdir()
        (broken / "meta.json").write_text("{not json")
        self.assertIsNone(load_record("01BROKEN"))
        self.assertEqual([r.artifact_id for r in list_records()], [good.artifact_id])

    def test_the_list_is_newest_first_and_capped(self) -> None:
        ids = []
        for i in range(3):
            ids.append(create_from_content("text", str(i)).artifact_id)
            time.sleep(0.01)
        self.assertEqual([r.artifact_id for r in list_records()], ids[::-1])
        self.assertEqual(len(list_records(limit=2)), 2)

    def test_an_empty_store_lists_nothing(self) -> None:
        self.assertEqual(list_records(), [])


class Generate(_ScratchHome):
    def test_an_unknown_block_names_the_ones_that_exist(self) -> None:
        with self.assertRaisesRegex(ArtifactError, "no generative block named 'nope'"):
            generate("nope", {})

    def test_the_store_lives_under_the_home(self) -> None:
        self.assertTrue(str(store.artifacts_root()).startswith(self._tmp.name))


if __name__ == "__main__":
    unittest.main()
