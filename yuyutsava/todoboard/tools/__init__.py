"""The ``todo_*`` tool family, bound to one board.

Two scopes, because two kinds of agent use the board differently. **capture** is
what the orchestrator and the chat master get: record a card, find one again,
and nothing that edits. **full** is the TinkerAgent's, which works a card rather
than filing it.

The tools are grouped by what they touch — :mod:`capture`, :mod:`editing`,
:mod:`notes` and :mod:`artifacts` — so a change to one family is one file rather
than a stretch of one long factory (`R-RDB-02`). :mod:`assemble` orders the
families, which fixes the order of the bound list — the model sees the tools in
that order.
"""

from yuyutsava.todoboard.tools.assemble import make_todo_tools

__all__ = ["make_todo_tools"]
