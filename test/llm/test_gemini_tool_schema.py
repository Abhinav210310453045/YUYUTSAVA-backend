"""``gemini_tool_schema``: a JSON Schema type list does not fail a Gemini turn.

``@piotr-agier/google-drive-mcp`` 2.9.0 publishes ``manage_accounts`` with
``account_id: {"type": ["string", "null"]}``. The Vertex SDK turned that list into
``"['STRING', 'NULL']"``, and every request that bound the tool raised, so typing
"hi" in the chat failed. These tests run the SDK's real converter on that schema.
No request is sent: no network, no API calls.

Run:  .venv/bin/python -m pytest test/llm/test_gemini_tool_schema.py
"""

from __future__ import annotations

import copy
import unittest

from google.protobuf.json_format import ParseError
from langchain_core.tools import StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_google_vertexai import ChatVertexAI
from langchain_google_vertexai.functions_utils import _format_to_gapic_tool

from yuyutsava.llm.quirks import gemini_tool_schema
from yuyutsava.llm.quirks.gemini_tool_schema import (
    anyof_for_type_lists,
    bindable_tools,
    tool_schema_safe,
)

#: google-drive-mcp 2.9.0's manage_accounts schema, descriptions shortened.
MANAGE_ACCOUNTS = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["list", "add", "remove", "set_default"],
            "description": "Which account-lifecycle operation to perform.",
        },
        "account_id": {
            "type": ["string", "null"],
            "description": "Account alias. Pass JSON null with set_default to clear it.",
        },
    },
    "required": ["action"],
}

BROKEN = {"type": "object", "properties": {"x": {"type": "bogus"}}}


def _mcp_tool(name: str = "google-drive__manage_accounts", schema: dict = MANAGE_ACCOUNTS):
    """A tool built the way ``mcp/tool_adapter.adapt`` builds one: a dict args_schema."""

    async def _call(**kwargs):
        return ""

    return StructuredTool.from_function(
        name=name,
        description="Manage connected Google accounts.",
        args_schema=copy.deepcopy(schema),
        coroutine=_call,
    )


def _declaration(spec):
    return _format_to_gapic_tool([spec]).function_declarations[0]


class UpstreamTripwireTests(unittest.TestCase):
    """If these start passing, the SDK was fixed and the quirk can be deleted."""

    def test_the_sdk_converter_rejects_a_type_list(self):
        with self.assertRaises(ParseError):
            _format_to_gapic_tool([convert_to_openai_tool(_mcp_tool())])

    def test_a_plain_chat_vertexai_fails_at_request_time(self):
        model = ChatVertexAI(model="gemini-2.5-flash", project="probe", location="us-central1")
        bound = model.bind_tools([_mcp_tool()])
        with self.assertRaises(ParseError):
            model._tools_gemini(tools=bound.kwargs["tools"])


class RewriteTests(unittest.TestCase):
    def test_a_nullable_type_becomes_anyof(self):
        self.assertEqual(
            anyof_for_type_lists({"type": ["string", "null"], "description": "d"}),
            {"description": "d", "anyOf": [{"type": "string"}, {"type": "null"}], "nullable": True},
        )

    def test_a_one_element_list_becomes_that_type(self):
        self.assertEqual(anyof_for_type_lists({"type": ["integer"]}), {"type": "integer"})

    def test_several_types_become_anyof_in_order_without_duplicates(self):
        out = anyof_for_type_lists({"type": ["string", "number", "string"]})
        self.assertEqual(out, {"anyOf": [{"type": "string"}, {"type": "number"}]})

    def test_nested_schemas_are_rewritten(self):
        schema = {
            "type": "object",
            "properties": {
                "tags": {"type": "array", "items": {"type": ["string", "null"]}},
                "choice": {
                    "anyOf": [{"type": "object", "properties": {"x": {"type": ["number"]}}}]
                },
            },
        }
        out = anyof_for_type_lists(schema)
        self.assertEqual(
            out["properties"]["tags"]["items"],
            {"anyOf": [{"type": "string"}, {"type": "null"}], "nullable": True},
        )
        self.assertEqual(
            out["properties"]["choice"]["anyOf"][0]["properties"]["x"], {"type": "number"}
        )

    def test_the_input_is_not_mutated(self):
        schema = copy.deepcopy(MANAGE_ACCOUNTS)
        anyof_for_type_lists(schema)
        self.assertEqual(schema, MANAGE_ACCOUNTS)

    def test_a_property_named_type_is_not_the_type_keyword(self):
        schema = {"type": "object", "properties": {"type": {"type": "string"}}}
        self.assertEqual(anyof_for_type_lists(schema), schema)

    def test_a_node_that_already_has_anyof_keeps_its_list(self):
        node = {"type": ["string", "null"], "anyOf": [{"type": "string"}]}
        self.assertEqual(anyof_for_type_lists(node), node)


class BindableToolsTests(unittest.TestCase):
    def setUp(self):
        gemini_tool_schema._warned.clear()

    def test_the_drive_tool_converts_and_keeps_its_meaning(self):
        (spec,) = bindable_tools([_mcp_tool()], check=_format_to_gapic_tool)
        parameters = _declaration(spec).parameters
        account_id = parameters.properties["account_id"]
        action = parameters.properties["action"]
        self.assertEqual(account_id.type_.name, "STRING")
        self.assertTrue(account_id.nullable)
        self.assertIn("Account alias", account_id.description)
        self.assertEqual(action.type_.name, "STRING")
        self.assertEqual(list(action.enum), ["list", "add", "remove", "set_default"])
        self.assertEqual(list(parameters.required), ["action"])

    def test_an_unconvertible_tool_is_dropped_and_named_once(self):
        tools = [_mcp_tool("srv__broken", BROKEN), _mcp_tool()]
        with self.assertLogs("yuyutsava.llm.quirks.gemini_tool_schema", "WARNING") as logs:
            first = bindable_tools(tools, check=_format_to_gapic_tool)
            second = bindable_tools(tools, check=_format_to_gapic_tool)
        self.assertEqual([s["function"]["name"] for s in first], ["google-drive__manage_accounts"])
        self.assertEqual(first, second)
        self.assertEqual(len(logs.records), 1)
        self.assertIn("srv__broken", logs.output[0])

    def test_without_a_check_every_function_tool_is_kept(self):
        self.assertEqual(len(bindable_tools([_mcp_tool("srv__broken", BROKEN)])), 1)

    def test_a_non_function_tool_passes_through_unchanged(self):
        marker = object()
        self.assertIs(bindable_tools([marker], check=_format_to_gapic_tool)[0], marker)


class MixinTests(unittest.TestCase):
    def setUp(self):
        gemini_tool_schema._warned.clear()

    def test_the_class_is_cached_and_named(self):
        class _Base:
            def bind_tools(self, tools, **kwargs):
                return tools

        cls = tool_schema_safe(_Base, _format_to_gapic_tool)
        self.assertIs(tool_schema_safe(_Base, _format_to_gapic_tool), cls)
        self.assertEqual(cls.__name__, "ToolSchemaSafe_Base")

    def test_bind_tools_passes_safe_tools_and_keeps_its_arguments(self):
        class _Base:
            def bind_tools(self, tools, *, tool_choice=None):
                return tools, tool_choice

        cls = tool_schema_safe(_Base, _format_to_gapic_tool)
        tools, choice = cls().bind_tools(
            [_mcp_tool(), _mcp_tool("srv__broken", BROKEN)], tool_choice="auto"
        )
        self.assertEqual(choice, "auto")
        self.assertEqual(len(tools), 1)
        _declaration(tools[0])  # converts; the unpatched SDK raises ParseError here

    def test_the_real_chat_vertexai_request_converts(self):
        model = tool_schema_safe(ChatVertexAI, _format_to_gapic_tool)(
            model="gemini-2.5-flash", project="probe", location="us-central1"
        )
        bound = model.bind_tools([_mcp_tool()])
        (gapic_tool,) = model._tools_gemini(tools=bound.kwargs["tools"])
        (declaration,) = gapic_tool.function_declarations
        self.assertEqual(declaration.name, "google-drive__manage_accounts")
        self.assertTrue(declaration.parameters.properties["account_id"].nullable)


if __name__ == "__main__":
    unittest.main()
