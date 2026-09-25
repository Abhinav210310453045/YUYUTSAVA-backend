"""LangChain ``@tool`` wrappers exposing the TaskRunner as callable tools.

``bind_tools(agent)`` is the only public entry point: the tools are closures over
a :class:`~yuyutsava.agents.task_runner.agent.TaskRunnerAgent` — the gateway
whose zones, policy and consent decide every call — so importing one directly
would give you a tool with nothing to act on.

The twelve tools are grouped by what they touch — :mod:`files`, :mod:`search`,
:mod:`shell`, :mod:`web`, :mod:`host` and :mod:`ask` (`R-RDB-02`), so finding
one never means scrolling past the other eleven.

Every tool returns a JSON string (``OperationResponse.model_dump_json()``) so
the calling model sees a structured, parseable result in its ToolMessage.
"""

from yuyutsava.agents.task_runner.tools.assemble import bind_tools

__all__ = ["bind_tools"]
