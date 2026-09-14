"""The daemon orchestrator's definitions; ``core.engine.build_orchestrator`` builds it.

- :mod:`~yuyutsava.agents.orchestrator.agent` — ``OrchestratorDeps``, the
  orchestrator's dependencies, and its ask-the-user tool
- :mod:`~yuyutsava.agents.orchestrator.capabilities` — the "available subagents"
  prompt block, generated from the registered subagents
- :mod:`~yuyutsava.agents.orchestrator.prompts` — the system prompt
- :mod:`~yuyutsava.agents.orchestrator.spawn` — a ``spawn_subagent`` tool; the
  orchestrator deliberately does not register it

This package re-exports nothing: import a name from the module that owns it.
"""
