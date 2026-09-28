"""The Gemini chat classes still take the kwargs that bound a reply.

`providers/gemini_budget` passes ``max_output_tokens`` and ``thinking_budget``
to ``ChatVertexAI`` / ``ChatGoogleGenerativeAI``. If an SDK release renamed
either, the kwarg would be dropped without an error, and thinking would once
more eat the answer's share — empty replies again (`R-BUG-23`). A provider
whose optional extra is not installed is skipped.
"""

from __future__ import annotations

import importlib
import unittest

import pytest

_CLASSES = (
    ("langchain_google_vertexai", "ChatVertexAI"),
    ("langchain_google_genai", "ChatGoogleGenerativeAI"),
)
_KWARGS = ("max_output_tokens", "thinking_budget")


def missing_output_kwargs(cls: type) -> list[str]:
    """The reply-bounding kwargs *cls* no longer declares as fields."""
    fields = getattr(cls, "model_fields", {})
    return [k for k in _KWARGS if k not in fields]


def test_gemini_chat_classes_accept_the_output_kwargs() -> None:
    checked = 0
    for module, name in _CLASSES:
        try:
            cls = getattr(importlib.import_module(module), name)
        except ImportError:
            continue
        checked += 1
        missing = missing_output_kwargs(cls)
        assert not missing, f"{module}.{name} no longer declares {missing}"
    if not checked:
        pytest.skip("no Gemini SDK installed")


class TheCheckFails(unittest.TestCase):
    """Negative control: a class without the fields is reported."""

    def test_a_renamed_kwarg_is_named(self) -> None:
        class Renamed:
            model_fields = {"max_output_tokens": None, "thinking_tokens": None}

        self.assertEqual(missing_output_kwargs(Renamed), ["thinking_budget"])


if __name__ == "__main__":
    unittest.main()
