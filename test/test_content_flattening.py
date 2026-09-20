"""One corpus of message-content shapes, and what each flattener does with it.

`R-DRY-02` found message content flattened five ways and asked for one
flattener. Three of the five are gone —
:func:`yuyutsava.text.flatten_content` is now the one used for model prose.
The fourth is kept, because measuring the two showed they answer different
questions:

* ``text.flatten_content`` is "what prose did the model emit". It joins with
  ``""`` because a provider splits one sentence across blocks, and it takes
  only ``{"type": "text"}`` blocks, because picking up text out of a
  ``tool_use`` block would put tool arguments into the assistant's reply.
* ``watcher.parsing.content_text`` is "render this message readably for a
  summary line". It joins with ``"\\n"`` because the pieces are separate steps,
  and it takes any block carrying text, because a background subagent's last
  message is often a tool block and a summary of "" helps nobody.

Seven of the twelve shapes below produce different output. That is the
finding's real consequence to defend against: a new content-block type has to
be handled in both. So both are asserted against the same table — add a shape
here and whichever flattener has not thought about it fails.
"""

from __future__ import annotations

import unittest

from yuyutsava.async_subagents.watcher.parsing import content_text
from yuyutsava.text import flatten_content

#: (shape, prose_result, readable_result)
CORPUS: tuple[tuple[object, str, str], ...] = (
    ("plain string", "plain string", "plain string"),
    ("", "", ""),
    (None, "", ""),
    ([], "", ""),
    (["a", "b"], "ab", "a\nb"),
    ([{"type": "text", "text": "x"}, {"type": "text", "text": "y"}], "xy", "x\ny"),
    (
        ["str block", {"type": "text", "text": "dict block"}],
        "str blockdict block",
        "str block\ndict block",
    ),
    # A provider that omits "type" on a text block.
    ([{"text": "no type"}], "", "no type"),
    # Tool arguments must not become the assistant's prose.
    ([{"type": "tool_use", "text": "inside a tool block"}], "", "inside a tool block"),
    # Nothing readable in either reading.
    ([{"type": "image", "url": "u"}], "", ""),
    # Mixed: merge_content appends raw str chunks to a list-content message.
    ([{"type": "text", "text": "a"}, "b", {"text": "c"}], "ab", "a\nb\nc"),
    # Scalars: prose says nothing, a summary says what it was handed.
    (42, "", "42"),
)


class BothFlattenersFaceTheSameShapes(unittest.TestCase):
    def test_prose_flattening(self) -> None:
        for shape, prose, _readable in CORPUS:
            with self.subTest(shape=repr(shape)[:60]):
                self.assertEqual(flatten_content(shape), prose)

    def test_readable_flattening(self) -> None:
        for shape, _prose, readable in CORPUS:
            with self.subTest(shape=repr(shape)[:60]):
                self.assertEqual(content_text(shape), readable)

    def test_a_string_block_is_never_dropped(self) -> None:
        """The bug that made this file's subject matter: dropping the plain-str
        blocks truncated a `final` event to the first chunk of the reply."""
        mixed = [{"type": "text", "text": "first"}, " and then the rest"]
        self.assertEqual(flatten_content(mixed), "first and then the rest")
        self.assertIn("and then the rest", content_text(mixed))

    def test_they_disagree_on_exactly_the_shapes_recorded_here(self) -> None:
        """If a change makes them agree everywhere, they can finally be one
        function — and this test is where that shows up."""
        differ = [s for s, prose, readable in CORPUS if prose != readable]
        self.assertEqual(len(differ), 7, "the recorded divergence has changed")


if __name__ == "__main__":
    unittest.main()
