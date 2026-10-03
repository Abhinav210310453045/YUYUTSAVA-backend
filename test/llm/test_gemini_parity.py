"""Every Gemini fix applies to Gemini on Vertex and on AI Studio alike.

Both routes build ``ChatGoogleGenerativeAI`` through one builder
(``llm/providers/gemini.py``). Gemini on Vertex used to be ``ChatVertexAI``, and
each fix was written against that class; the owner's rule for the move was that
none is dropped, since the failures they fix can come back through the new
class too (design §2 rule 14). These pin it: the built model of each route is
every fix's class, in the order they wrap, and the tool-schema fix still keeps a
nullable setting nullable with genai's converter.
"""

from __future__ import annotations

import unittest

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_google_genai._function_utils import convert_to_genai_function_declarations

from test.support.targets import gemini_targets
from yuyutsava.llm.factory import chat_model
from yuyutsava.llm.quirks.first_chunk_retry import first_chunk_retry
from yuyutsava.llm.quirks.gemini_parts import parts_safe
from yuyutsava.llm.quirks.gemini_tool_schema import bindable_tools, tool_schema_safe
from yuyutsava.llm.quirks.loop_affinity import loop_pinned


def layers() -> list[tuple[str, type]]:
    """Each fix's class, innermost first, as the builder composes them."""
    schema = tool_schema_safe(ChatGoogleGenerativeAI, convert_to_genai_function_declarations)
    parts = parts_safe(schema)
    retry = first_chunk_retry(parts)
    return [
        ("tool schema", schema),
        ("empty turns", parts),
        ("first-chunk retry", retry),
        ("loop", loop_pinned(retry)),
    ]


class EveryRouteHasEveryFix(unittest.TestCase):
    def test_each_layer_wraps_each_route(self) -> None:
        for target in gemini_targets():
            model = chat_model(target)
            for name, layer in layers():
                with self.subTest(route=target.type, fix=name):
                    self.assertIsInstance(model, layer)

    def test_both_routes_are_one_class(self) -> None:
        classes = {type(chat_model(target)) for target in gemini_targets()}
        self.assertEqual(len(classes), 1)


class AToolSchemaKeepsItsNullability(unittest.TestCase):
    def test_a_type_list_with_null_stays_nullable(self) -> None:
        tool = {
            "type": "function",
            "function": {
                "name": "manage_accounts",
                "description": "d",
                "parameters": {
                    "type": "object",
                    "properties": {"account_id": {"type": ["string", "null"]}},
                },
            },
        }
        [fixed] = bindable_tools([tool], check=convert_to_genai_function_declarations)
        declaration = convert_to_genai_function_declarations([fixed])[0].function_declarations[0]
        prop = declaration.parameters.properties["account_id"]
        self.assertEqual((prop.type, prop.nullable), ("STRING", True))


class TheCheckFails(unittest.TestCase):
    """Negative control: without the rewrite, genai drops the nullability."""

    def test_the_raw_schema_loses_null(self) -> None:
        schema = {"type": "object", "properties": {"account_id": {"type": ["string", "null"]}}}
        tool = {
            "type": "function",
            "function": {"name": "m", "description": "d", "parameters": schema},
        }
        declaration = convert_to_genai_function_declarations([tool])[0].function_declarations[0]
        self.assertIsNone(declaration.parameters.properties["account_id"].nullable)


if __name__ == "__main__":
    unittest.main()
