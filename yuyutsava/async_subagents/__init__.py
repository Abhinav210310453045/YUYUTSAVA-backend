"""Async (background) subagent infrastructure.

See `docs/` and the original plan for design context. This package owns:

  * ``host.AsyncSubagentHost``     — embedded LangGraph API server
  * ``mirror.AsyncTaskMirror``      — daemon-scoped task-state mirror
  * ``watcher.AsyncTaskHealthWatcher`` — polling HITL bridge
  * ``cap_policy``                  — concurrency cap on ``start_async_task``
  * ``remote.RemoteAsyncSubagentSpec`` — first-class remote subagent spec
  * ``_lg_graphs``                  — module that LangGraph API imports to locate
                                      compiled graphs by name (populated by host)
"""
