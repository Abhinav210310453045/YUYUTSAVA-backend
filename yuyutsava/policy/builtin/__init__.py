"""Built-in policies that an agent build switches on from its profile.

- :mod:`~yuyutsava.policy.builtin.budget` — ``BudgetPolicy``, the cumulative-spend
  ceiling for one task
- :mod:`~yuyutsava.policy.builtin.usage` — ``UsagePolicy``, one ``llm_usage`` row per
  model call
- :mod:`~yuyutsava.policy.builtin.permission` — ``PermissionPolicy``, which asks
  before a risky shell command; the rules it applies are
  :mod:`~yuyutsava.policy.builtin.command_safety`
- :mod:`~yuyutsava.policy.builtin.tool_permissions` — ``PermissionsPolicy`` from
  ``~/.yuyutsava/permissions.json`` (auto-approve, propose, daily caps) and the
  ``ws_*`` cap enforcer
- :mod:`~yuyutsava.policy.builtin.filesystem_prompt` — ``FilesystemPromptPolicy``,
  which strips deepagents' filesystem block from the system prompt
- :mod:`~yuyutsava.policy.builtin.tool_filter` — ``ToolFilterPolicy``, which hides
  lazily-discovered tool families from the model
- :mod:`~yuyutsava.policy.builtin.subagent_gate` — ``SubagentGatePolicy``, which
  enforces the user's subagent switches
- :mod:`~yuyutsava.policy.builtin.retrieval_injection` — retrieved memory and skills
  injected before a model call
- :mod:`~yuyutsava.policy.builtin.voice_style` — ``VoiceStylePolicy``, a spoken-style
  addendum on voice turns

This package re-exports nothing: import a name from the module that owns it.
"""
