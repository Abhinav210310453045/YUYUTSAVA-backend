"""Sub-agents, and the filesystem and shell gateway they all act through.

- :class:`~yuyutsava.agents.task_runner.agent.TaskRunnerAgent` is the permission
  gateway for every file and shell operation; create one per workspace.
- :class:`~yuyutsava.agents.base_sub_agent.BaseSubAgent` is the base class for the
  LLM sub-agents; each sub-package holds one agent and its prompts.
- :func:`~yuyutsava.agents.task_runner.tools.bind_tools` builds the ``tr_*`` tools
  bound to a workspace.

This package re-exports nothing: import a name from the module that owns it.
"""
