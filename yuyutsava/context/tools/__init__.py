"""The ``ctx_*`` tools: what the agent can do about its own context window.

Four surfaces, not one — the set depends on what is wired behind it, and a tool
that is offered but cannot work is worse than one that is absent. Reading back
offloaded results and compacting are always available; the history tools appear
once a transcript store is wired; semantic recall appears only when the store
maintains an index for it.

Grouped by what each family touches — :mod:`artifacts`, :mod:`compaction`,
:mod:`history` and :mod:`recall` (`R-RDB-02`). They need no shared context
object: each family closes over a different thing, or over nothing.
"""

from yuyutsava.context.tools.assemble import make_context_tools

__all__ = ["make_context_tools"]
