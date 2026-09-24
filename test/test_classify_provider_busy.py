"""What status a user is shown when a provider pushes back.

W4 named `classify_provider_busy`. Three surfaces labelled a retried provider
error — the rich renderer's spinner, the dashboard's status line and the REPL's
"retries gave up" message — each with its own guess from the class name:
``"429" if ResourceExhausted else "503"``.

That guess was wrong twice. The retry predicate also retries `DeadlineExceeded`
and `Aborted`, which the SDK reports as 504 and 409, so a timeout was announced
as "provider busy (503)". And the guesses drifted — two matched the name as a
substring, one exactly — so a subclass could read 429 on one surface and 503 on
another. The SDK already carries the right code; this reads it.
"""

from __future__ import annotations

import pathlib
import re
import unittest

from google.api_core import exceptions as g

from yuyutsava.llm.quirks.first_chunk_retry import _is_transient, classify_provider_busy

ROOT = pathlib.Path(__file__).resolve().parent.parent / "yuyutsava"


class _GrpcStatus:
    def __init__(self, name: str) -> None:
        self.name = name


class _RawGrpcError(Exception):
    """A grpc.aio error: `code` is a method returning a status, not an int."""

    def __init__(self, status: str) -> None:
        super().__init__(status)
        self._status = _GrpcStatus(status)

    def code(self):
        return self._status


class EveryRetriedKindIsLabelledCorrectly(unittest.TestCase):
    CASES = (
        (g.ResourceExhausted, "429"),
        (g.ServiceUnavailable, "503"),
        (g.DeadlineExceeded, "504"),
        (g.Aborted, "409"),
    )

    def test_the_sdk_exceptions(self) -> None:
        for cls, label in self.CASES:
            with self.subTest(kind=cls.__name__):
                self.assertEqual(classify_provider_busy(cls("x")), label)

    def test_a_timeout_is_not_called_unavailable(self) -> None:
        """The concrete bug: a retried deadline read as 'provider busy (503)'."""
        self.assertNotEqual(classify_provider_busy(g.DeadlineExceeded("x")), "503")

    def test_everything_labelled_is_something_that_is_retried(self) -> None:
        """The label and the retry decision describe the same set."""
        for cls, _label in self.CASES:
            with self.subTest(kind=cls.__name__):
                self.assertTrue(_is_transient(cls("x")))


class SubclassesAreNotGuessedAt(unittest.TestCase):
    def test_a_subclass_keeps_its_parent_s_status(self) -> None:
        """A name test calls this 429 or 503 depending on how it is written."""

        class ResourceExhaustedError(g.ResourceExhausted):
            pass

        self.assertEqual(classify_provider_busy(ResourceExhaustedError("x")), "429")


class RawGrpcErrors(unittest.TestCase):
    def test_their_status_is_mapped(self) -> None:
        for status, label in (
            ("RESOURCE_EXHAUSTED", "429"),
            ("UNAVAILABLE", "503"),
            ("DEADLINE_EXCEEDED", "504"),
            ("ABORTED", "409"),
        ):
            with self.subTest(status=status):
                self.assertEqual(classify_provider_busy(_RawGrpcError(status)), label)

    def test_a_code_method_that_raises_does_not_raise_here(self) -> None:
        """A label must never become a second error on top of the first."""

        class Broken(Exception):
            def code(self):
                raise RuntimeError("no")

        self.assertEqual(classify_provider_busy(Broken()), "503")


class AnErrorWithNoStatusAtAll(unittest.TestCase):
    """A stand-in with the right name and none of the real exception's
    attributes — how a CLI test keeps the Google SDK out of its imports."""

    def test_the_exact_name_is_recognised(self) -> None:
        class ResourceExhausted(Exception):
            pass

        class ServiceUnavailable(Exception):
            pass

        self.assertEqual(classify_provider_busy(ResourceExhausted("x")), "429")
        self.assertEqual(classify_provider_busy(ServiceUnavailable("x")), "503")

    def test_a_near_miss_name_is_not(self) -> None:
        """Exact, not a substring — the substring rule is how two surfaces
        came to disagree."""

        class ResourceExhaustedWrapper(Exception):
            pass

        self.assertEqual(classify_provider_busy(ResourceExhaustedWrapper("x")), "503")

    def test_a_real_status_beats_the_name(self) -> None:
        """A name says less than the status the SDK attached."""

        class ResourceExhausted(g.DeadlineExceeded):
            pass

        self.assertEqual(classify_provider_busy(ResourceExhausted("x")), "504")


class TheFallbackIsUnchanged(unittest.TestCase):
    def test_an_unrecognised_error_reads_503(self) -> None:
        self.assertEqual(classify_provider_busy(ValueError("x")), "503")


class NoSurfaceGuessesFromTheName(unittest.TestCase):
    def test_the_three_surfaces_ask_the_classifier(self) -> None:
        for rel in (
            "cli/render/renderer.py",
            "cli/render/dashboard.py",
            "cli/commands/chat_repl.py",
        ):
            with self.subTest(surface=rel):
                source = (ROOT / rel).read_text()
                self.assertIn("classify_provider_busy(", source)
                self.assertIsNone(
                    re.search(r'"429" if .*ResourceExhausted', source),
                    f"{rel} guesses the status from the class name",
                )


if __name__ == "__main__":
    unittest.main()
