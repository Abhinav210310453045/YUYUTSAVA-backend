"""The ``ctx_*`` tools: what the agent can do about its own context window.

Four surfaces, not one — the set depends on what is wired behind it, and a tool
that is offered but cannot work is worse than one that is absent. Reading back
offloaded results and compacting are always available; the history tools appear
once a transcript store is wired; semantic recall appears only when the store
maintains an index for it.

Grouped by what each family touches — :mod:`artifacts`, :mod:`compaction`,
:mod:`history` and :mod:`recall` — because they used to be seven closures and
nine helpers around one 242-line function (`R-RDB-02`). They need no shared
context object: each family closes over a different thing, or over nothing.
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool

from yuyutsava.context.artifacts import MAX_SLICE_CHARS, ArtifactStore
from yuyutsava.context.tools.artifacts import make_artifacts_tools
from yuyutsava.context.tools.compaction import make_compaction_tools
from yuyutsava.context.tools.history import make_history_tools
from yuyutsava.context.tools.recall import make_recall_tools

__all__ = ["MAX_SLICE_CHARS", "make_context_tools"]


def make_context_tools(
    store: ArtifactStore,
    transcript_store: Any | None = None,
) -> list[BaseTool]:
    """Build the ``ctx_*`` family for one conversation's stores.

    The order is fixed rather than incidental: it is what the model has always
    been shown, and the agent fingerprint sorts names before hashing, so nothing
    else would catch a change to it.
    """
    tools: list[BaseTool] = [*make_artifacts_tools(store), *make_compaction_tools()]
    # The read-back path for turns compaction has evicted. Offered whenever a
    # transcript store exists (both backends have one; only the semantic
    # *index* is Postgres-only, and these tools need just the rows).
    if transcript_store is not None:
        tools.extend(make_history_tools(transcript_store))
    # Semantic recall needs an index the SQLite backend does not maintain.
    if getattr(store, "supports_recall", False):
        tools.extend(make_recall_tools(store))
    return tools
