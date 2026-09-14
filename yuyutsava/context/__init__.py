"""Context control: keeps an agent's context bounded without losing what was said.

Two mechanisms, both wired by :mod:`yuyutsava.core.engine`:

- :class:`~yuyutsava.context.offload_policy.ToolResultOffloadPolicy` keeps large
  tool results out of graph state. The full content goes to the artifact store and
  a small digest takes its place; the agent reads more with the ``ctx_*`` tools.
- :class:`~yuyutsava.context.compaction.YuyutsavaCompactionMiddleware` summarises
  older turns when the conversation nears the model's input budget, keeps the
  original task pinned, and persists each summary so continuity survives restarts.

:class:`~yuyutsava.daemon.budget_policy.BudgetPolicy` caps cumulative spend above
both. This package re-exports nothing: import a name from the module that owns it.
"""
