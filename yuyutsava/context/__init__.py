"""Context controller: bounded, self-compacting agent context.

Three cooperating layers, wired in :mod:`yuyutsava.core.engine`. Since Phase 4
the first is a plain :class:`~yuyutsava.policy.base.Policy` behind
``LangChainPolicyAdapter`` rather than an ``AgentMiddleware`` subclass; the other
two are still middleware.

1. :class:`~yuyutsava.context.offload_policy.ToolResultOffloadPolicy` — large
   tool results never enter
   graph state (and therefore never reach the checkpointer); the full
   content goes to the :class:`ArtifactStore` and a small structured digest
   takes its place. The agent reads more on demand via ``ctx_fetch_artifact``
   / ``ctx_grep_artifact``.
2. :class:`YuyutsavaCompactionMiddleware` — when the conversation nears the
   model's input budget, older turns are summarized (cheap model), the
   original task message stays pinned, and the rewritten state — summary +
   recent tail — is what the checkpointer persists. Each summary is also
   stored in ``thread_summaries`` (and embedded into semantic memory when
   enabled) so continuity survives sweeps and restarts.
3. :class:`yuyutsava.daemon.budget_policy.BudgetPolicy` (existing) remains the
   absolute cumulative-spend ceiling above both.
"""

from yuyutsava.context.config import ContextSettings
from yuyutsava.context.offload_policy import ToolResultOffloadPolicy
from yuyutsava.context.compaction import YuyutsavaCompactionMiddleware
from yuyutsava.context.tools import make_context_tools

__all__ = [
    "ContextSettings",
    "ToolResultOffloadPolicy",
    "YuyutsavaCompactionMiddleware",
    "make_context_tools",
]
