"""Unit tests for tr_fetch_url's download verification helpers.

Covers the exact failure that produced corrupt sample files: a server returning
a 200 OK HTML interstitial (Cloudflare "Just a moment…") or a 0-byte body that
curl happily saves under a binary extension.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from yuyutsava.agents.task_runner.tools import _looks_like_html, _verify_download


def _write(tmp: str, name: str, data: bytes) -> str:
    path = os.path.join(tmp, name)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


_CLOUDFLARE = (
    b"<!DOCTYPE html><html><head><title>Just a moment...</title></head>"
    b"<body>Enable JavaScript and cookies to continue</body></html>"
)


class LooksLikeHtmlTests(unittest.TestCase):
    def test_doctype(self):
        self.assertTrue(_looks_like_html(b"   <!doctype html><html>"))

    def test_cloudflare_marker(self):
        self.assertTrue(_looks_like_html(_CLOUDFLARE))

    def test_binary_not_html(self):
        self.assertFalse(_looks_like_html(b"PK\x03\x04\x14\x00"))

    def test_plain_text_not_html(self):
        self.assertFalse(_looks_like_html(b"name,age\nalice,30\n"))


class VerifyDownloadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def test_empty_file_rejected(self):
        p = _write(self.tmp, "sample.zip", b"")
        ok, why, detected = _verify_download(p, "auto")
        self.assertFalse(ok)
        self.assertIn("empty", why)

    def test_html_interstitial_saved_as_zip_rejected(self):
        p = _write(self.tmp, "sample.zip", _CLOUDFLARE)
        ok, why, detected = _verify_download(p, "auto")
        self.assertFalse(ok)
        self.assertEqual(detected, "html")

    def test_real_pdf_accepted(self):
        p = _write(self.tmp, "doc.pdf", b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n...")
        ok, why, detected = _verify_download(p, "auto")
        self.assertTrue(ok, why)
        self.assertEqual(detected, "pdf")

    def test_real_zip_accepted(self):
        p = _write(self.tmp, "a.zip", b"PK\x03\x04" + b"\x00" * 64)
        ok, _why, detected = _verify_download(p, "auto")
        self.assertTrue(ok)
        self.assertEqual(detected, "zip")

    def test_real_jpg_accepted(self):
        p = _write(self.tmp, "img.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 32)
        ok, _why, _d = _verify_download(p, "auto")
        self.assertTrue(ok)

    def test_real_mp3_id3_accepted(self):
        p = _write(self.tmp, "song.mp3", b"ID3\x03\x00" + b"\x00" * 64)
        ok, _why, _d = _verify_download(p, "auto")
        self.assertTrue(ok)

    def test_text_saved_as_pdf_rejected_by_magic(self):
        p = _write(self.tmp, "notreally.pdf", b"this is just text, not a pdf at all\n")
        ok, why, _d = _verify_download(p, "auto")
        self.assertFalse(ok)
        self.assertIn("magic-byte", why)

    def test_csv_text_accepted(self):
        p = _write(self.tmp, "data.csv", b"name,age\nalice,30\nbob,25\n")
        ok, _why, detected = _verify_download(p, "auto")
        self.assertTrue(ok)
        self.assertEqual(detected, "csv")

    def test_html_saved_as_csv_rejected(self):
        p = _write(self.tmp, "data.csv", _CLOUDFLARE)
        ok, _why, detected = _verify_download(p, "auto")
        self.assertFalse(ok)
        self.assertEqual(detected, "html")

    def test_explicit_expected_type_overrides_extension(self):
        # No extension on disk, but caller declares it's a zip.
        p = _write(self.tmp, "blob", b"PK\x03\x04" + b"\x00" * 8)
        ok, _why, detected = _verify_download(p, "zip")
        self.assertTrue(ok)
        self.assertEqual(detected, "zip")


if __name__ == "__main__":
    unittest.main(verbosity=2)
