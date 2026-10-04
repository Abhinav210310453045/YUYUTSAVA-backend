"""``TaskModelSource`` — the models each task uses, as the agent stacks ask for them.

The source of truth is :class:`yuyutsava.llm.selection.TaskModels`; this port is
what code above it depends on (DEP-2), so a stack can be built against a test
double. Targets and models are named structurally (``Any``) because this package
imports nothing from ``yuyutsava``: a target is a ``yuyutsava.llm.target.ModelTarget``,
a model a LangChain ``BaseChatModel``.
"""

from __future__ import annotations

from typing import Any, Protocol


class TaskModelSource(Protocol):
    """Which model each task uses, and the built models to run it on."""

    def target(self, task: str) -> Any:
        """The model *task* uses (a ``ModelTarget``)."""
        ...

    def window(self, target: Any) -> int:
        """The input window, in tokens, the context budget steers under for *target*."""
        ...

    def model(self, task: str, *, temperature: float, disable_reasoning: bool = False) -> Any:
        """*task*'s model for the running event loop, rebuilt when the config changes."""
        ...

    def build(self, task: str, *, temperature: float, disable_reasoning: bool = False) -> Any:
        """A new model for *task*, for code that builds before any loop runs it."""
        ...

    def resolve(self, reference: str) -> Any:
        """The target a ``models.toml`` reference names; ``KeyError`` when there is none."""
        ...

    def pooled(self, target: Any, *, temperature: float, disable_reasoning: bool = False) -> Any:
        """*target*'s model for the running event loop (a ``ModelHandle``)."""
        ...

    @property
    def generation(self) -> int:
        """Which load of ``models.toml`` is in use; ``0`` on the environment route."""
        ...
