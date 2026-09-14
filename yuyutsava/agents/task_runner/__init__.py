"""The task runner: the permission gateway every file and shell operation passes through.

- :class:`~yuyutsava.agents.task_runner.agent.TaskRunnerAgent` decides, by zone and
  permission rules, whether an operation may run, and runs it; create one per
  workspace.
- :func:`~yuyutsava.agents.task_runner.tools.bind_tools` builds the ``tr_*`` tools
  bound to a workspace.
- Request and response models live in :mod:`yuyutsava.models.operations`.

This package re-exports nothing: import a name from the module that owns it.
"""
