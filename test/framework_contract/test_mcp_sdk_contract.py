"""The installed MCP SDK still names its fields one of the two ways the adapter reads.

`R-BUG-25`: mcp 1.x models carry the wire protocol's camelCase as attribute
names (``Tool.inputSchema``, ``CallToolResult.isError``); mcp 2.x renamed them to
snake_case. ``mcp/tool_adapter.sdk_field`` reads either. A third spelling in a
future release would fail two ways at once, and one of them silently: every
server stops loading, and ``is_error`` reads as absent, so failed calls reach
the model as successes. This names the field that moved.
"""

from __future__ import annotations

import unittest

#: (model, the 1.x attribute, the 2.x attribute) — what the adapter reads.
FIELDS = (
    ("Tool", "inputSchema", "input_schema"),
    ("CallToolResult", "isError", "is_error"),
)


def unreadable_fields(types_module) -> list[str]:
    """``Model.field`` for each field *types_module* declares under neither spelling."""
    missing = []
    for model, camel, snake in FIELDS:
        declared = getattr(getattr(types_module, model), "model_fields", {})
        if camel not in declared and snake not in declared:
            missing.append(f"{model}.{snake}")
    return missing


def test_the_mcp_models_declare_a_spelling_the_adapter_reads() -> None:
    import mcp.types  # noqa: PLC0415 — the SDK is what is under test, imported where it is checked

    missing = unreadable_fields(mcp.types)
    assert not missing, f"mcp.types no longer declares {missing} under either spelling"


class TheCheckFails(unittest.TestCase):
    """Negative control: a model with a third spelling is named."""

    def test_a_renamed_field_is_named(self) -> None:
        class Tool:
            model_fields = {"schema": None}

        class CallToolResult:
            model_fields = {"is_error": None}

        types = type("types", (), {"Tool": Tool, "CallToolResult": CallToolResult})
        self.assertEqual(unreadable_fields(types), ["Tool.input_schema"])


if __name__ == "__main__":
    unittest.main()
