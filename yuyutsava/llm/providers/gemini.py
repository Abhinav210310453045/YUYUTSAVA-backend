"""Gemini, on Vertex AI or AI Studio — one class, every Gemini quirk.

Both routes build ``ChatGoogleGenerativeAI``: AI Studio with an API key, Vertex
with ``vertexai=True`` and Google credentials. ``ChatVertexAI`` is deprecated and
removed in langchain-google-vertexai 4.0; the genai class retries a busy stream
open and signs Gemini 3 function calls itself (design §2 rule 14,
``test/framework_contract/test_genai_vertex_contract.py``).

Every Gemini fix applies on both routes, kept even where the SDK now covers the
case, because the same failures can come back through it:

* ``gemini_tool_schema`` — a JSON Schema type list stays expressible, nullability
  included, checked with genai's own converter;
* ``gemini_parts`` — an assistant turn with nothing in it never reaches Gemini;
* ``first_chunk_retry`` — owns the retry policy and the "provider busy" notices;
  the SDK keeps a token retry so the two ladders do not multiply;
* ``loop_affinity`` — one instance per event loop;
* ``gemini_budget`` — thinking gets a budget on top of the answer's share.
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel

from yuyutsava.llm.base import BuildOptions, require
from yuyutsava.llm.config.schema import DEFAULT_THINKING_BUDGET
from yuyutsava.llm.providers.gemini_budget import gemini_output_kwargs
from yuyutsava.llm.quirks.first_chunk_retry import first_chunk_retry
from yuyutsava.llm.quirks.gemini_parts import parts_safe
from yuyutsava.llm.quirks.gemini_tool_schema import tool_schema_safe
from yuyutsava.llm.quirks.loop_affinity import loop_pinned
from yuyutsava.llm.target import ModelTarget

#: Attempts the SDK makes itself. ``first_chunk_retry`` owns the real ladder; at
#: six each, the two nested into minutes of silence per call.
SDK_RETRIES = 2


def thinking_budget(target: ModelTarget) -> int:
    """The thinking budget for *target*: its alias's, or the default."""
    entry = target.entry
    if entry is not None and entry.thinking is not None:
        return entry.thinking.budget
    return DEFAULT_THINKING_BUDGET


def build_gemini(target: ModelTarget, options: BuildOptions, **route: Any) -> BaseChatModel:
    """``ChatGoogleGenerativeAI`` for *target*; *route* says how to reach it.

    *route* is AI Studio's ``api_key``, or Vertex's ``vertexai``, ``project``,
    ``location`` and ``credentials``.
    """
    install = "'yuyutsava[google]'"
    mod = require("langchain_google_genai", provider="Gemini", install=install)
    converter = require(
        "langchain_google_genai._function_utils", provider="Gemini", install=install
    )
    cls = loop_pinned(
        first_chunk_retry(
            parts_safe(
                tool_schema_safe(
                    mod.ChatGoogleGenerativeAI, converter.convert_to_genai_function_declarations
                )
            )
        )
    )
    return cls(
        model=target.model,
        temperature=options.temperature,
        max_retries=SDK_RETRIES,
        **gemini_output_kwargs(target.model, thinking_budget(target)),
        **route,
    )


__all__ = ["SDK_RETRIES", "build_gemini", "thinking_budget"]
