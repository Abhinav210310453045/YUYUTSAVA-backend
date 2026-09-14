"""Policies: cross-cutting agent behaviour written in this project's own types.

- :mod:`~yuyutsava.policy.types` — what a policy sees (``ToolCall``) and may answer
- :mod:`~yuyutsava.policy.base` — the ``Policy`` contract, with no-op defaults
- :mod:`~yuyutsava.policy.adapter` — ``LangChainPolicyAdapter``, which runs policies as
  LangChain agent middleware
- :mod:`~yuyutsava.policy.ask` — how a policy's question reaches the user

``types`` and ``base`` import no agent framework; ``adapter`` and ``ask`` are where the
framework comes in. This package re-exports nothing: import a name from the module
that owns it.
"""
