"""Vertex rejects a JSON Schema ``type`` list — one tool schema fails every turn.

## The failure

JSON Schema lets a property name several types: ``"type": ["string", "null"]``.
MCP servers publish their tool schemas as they are, and version 2.9.0 of
``@piotr-agier/google-drive-mcp`` uses that form for ``manage_accounts.account_id``.
``langchain_google_vertexai`` (3.2.4, ``functions_utils._format_json_schema_to_gapic``)
converts ``type`` with ``str(value).upper()``, so the list becomes the string
``"['STRING', 'NULL']"`` and ``gapic.Schema.from_json`` raises::

    ParseError: ... Invalid enum value ['STRING', 'NULL'] for enum type
    google.cloud.aiplatform.v1beta1.Type at Schema.properties[account_id].type

The conversion runs on every request that binds the tool, so a single schema from a
single server fails every turn, a plain "hi" included. ``langchain_google_genai``
converts the same schema without complaint: this is the Vertex SDK's converter, not
Gemini's wire format, so only ``providers/vertex.py`` applies this quirk.

## The repair, at ``bind_tools``

1. Every ``type`` list is rewritten as the equivalent ``anyOf``: ``["string", "null"]``
   becomes ``[{"type": "string"}, {"type": "null"}]``, which the converter already
   understands (the null branch makes the property optional). A list containing
   ``"null"`` also gets ``nullable: true``, the keyword Vertex's schema uses, so the
   model can still send ``null`` where the tool accepts it.
2. When the provider passes the SDK's converter as ``check``, each tool is converted
   once more. A tool that still fails is dropped, and named in a warning once per
   process, so a schema Vertex cannot express costs that one tool, not the
   conversation.

``bind_tools`` is the entry every tool passes through: the agent loop calls it on
each model request, and ``with_structured_output`` calls it for tool-calling output.
A tool that is not a function (a ``gapic.Tool``, Google Search) passes through as it
came.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, Callable, Sequence

from langchain_core.utils.function_calling import convert_to_openai_tool

logger = logging.getLogger("yuyutsava.llm.quirks.gemini_tool_schema")

#: Converts a list of OpenAI-format tools, raising when Vertex cannot express one.
ToolCheck = Callable[[list[dict[str, Any]]], Any]

_warned: set[str] = set()


def anyof_for_type_lists(schema: Any) -> Any:
    """A copy of *schema* with every ``"type": [...]`` rewritten as ``anyOf``.

    The input is not mutated. A one-element list becomes that type; a list with
    ``"null"`` also marks the node ``nullable``. A node that already has ``anyOf``
    keeps its list, because no single rewrite is equivalent; the ``check`` in
    :func:`bindable_tools` then decides whether its tool survives.
    """
    if isinstance(schema, list):
        return [anyof_for_type_lists(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    out = {key: anyof_for_type_lists(value) for key, value in schema.items()}
    types = out.get("type")
    if isinstance(types, list) and "anyOf" not in out:
        unique = list(dict.fromkeys(types))
        del out["type"]
        if len(unique) == 1:
            out["type"] = unique[0]
        elif unique:
            out["anyOf"] = [{"type": name} for name in unique]
            if "null" in unique:
                out["nullable"] = True
    return out


def bindable_tools(tools: Sequence[Any], check: ToolCheck | None = None) -> list[Any]:
    """*tools* as Vertex can bind them: type lists rewritten, unconvertible tools dropped."""
    out: list[Any] = []
    for tool in tools:
        try:
            spec = convert_to_openai_tool(tool)
        except (ValueError, TypeError):
            out.append(tool)  # not a function tool; the SDK knows what to do with it
            continue
        function = spec.get("function", {})
        if "parameters" in function:
            function = {**function, "parameters": anyof_for_type_lists(function["parameters"])}
            spec = {**spec, "function": function}
        if check is not None:
            try:
                check([spec])
            except Exception as exc:  # noqa: BLE001 — any converter failure means Vertex rejects the tool
                name = str(function.get("name", "?"))
                if name not in _warned:
                    _warned.add(name)
                    logger.warning(
                        "not binding tool %r: Vertex cannot express its schema (%s: %s)",
                        name,
                        type(exc).__name__,
                        exc,
                    )
                continue
        out.append(spec)
    return out


class _ToolSchemaSafeMixin:
    """Routes ``bind_tools`` through :func:`bindable_tools`."""

    _tool_check: ToolCheck | None = None

    def bind_tools(self, tools, *args, **kwargs):  # type: ignore[no-untyped-def]
        return super().bind_tools(  # type: ignore[misc]
            bindable_tools(tools, check=type(self)._tool_check), *args, **kwargs
        )


@lru_cache(maxsize=None)
def tool_schema_safe(base: type, check: ToolCheck | None = None) -> type:
    """``base`` re-based so every bound tool schema is one Gemini can convert.

    Cached for the same reason as ``gemini_parts.parts_safe``: one class per base
    (and check) keeps ``isinstance``, pickling and identity stable. *check* is passed
    in by the provider, which already holds the optional SDK, so this module imports
    no provider SDK.
    """
    namespace = {"_tool_check": staticmethod(check) if check is not None else None}
    return type(f"ToolSchemaSafe{base.__name__}", (_ToolSchemaSafeMixin, base), namespace)


__all__ = ["ToolCheck", "anyof_for_type_lists", "bindable_tools", "tool_schema_safe"]
