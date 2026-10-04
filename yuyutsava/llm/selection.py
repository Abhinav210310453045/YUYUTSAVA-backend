"""Which model each task uses, and the built models to run it on.

:class:`TaskModels` is the one place that answers "which model does this task
use?" (tasks: :mod:`yuyutsava.llm.tasks`):

* **from ``models.toml``** when it exists — the task's own entry under
  ``[tasks]``; for a named subagent, ``subagent.<name>``, then ``subagent``; and
  ``defaults.model`` for any task without one. It is read through a
  :class:`~yuyutsava.llm.config.watch.ConfigSource`, so an edit is in use from the
  next call.
* **from the environment** (``LLM_PROVIDER`` and its role prefixes) for a process
  that has no ``models.toml`` — until the environment cutover (design §7, M7).

A ``models.toml`` that exists but has never loaded is an error, not a reason to
fall back to the environment: falling back would run a model its owner did not
choose, and hide why.

Built models are pooled per event loop — a model's async client binds to the
loop that first drives it (``quirks/loop_affinity``) — and keyed by the target,
the build options and the config generation, so an edit rebuilds what changed
and a call that asks again gets the same instance.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from yuyutsava.aio import LoopLocal
from yuyutsava.llm.config.schema import ModelsConfig
from yuyutsava.llm.config.watch import ConfigSource, ConfigState
from yuyutsava.llm.factory import model_handle
from yuyutsava.llm.handle import ModelHandle
from yuyutsava.llm.settings import llm_settings_from_env
from yuyutsava.llm.target import ModelTarget, target_for
from yuyutsava.llm.tasks import SUBAGENT_PREFIX, is_task

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

logger = logging.getLogger(__name__)

#: The temperature a model is built at only to read its window — any value would do;
#: this is the chat default, so the instance is the one a chat would get.
_WINDOW_TEMPERATURE = 0.1

#: The environment role each task reads on the route without ``models.toml``.
_ENV_ROLES: dict[str, str | None] = {"chat": None}


def env_role(task: str) -> str | None:
    """The ``<ROLE>_`` prefix *task* reads its model from in the environment."""
    if task.startswith(SUBAGENT_PREFIX):
        return "subagent"
    return _ENV_ROLES.get(task, task)


def reference_for(config: ModelsConfig, task: str) -> str:
    """The model reference *task* resolves to in *config*."""
    if task in config.tasks:
        return config.tasks[task]
    if task.startswith(SUBAGENT_PREFIX) and "subagent" in config.tasks:
        return config.tasks["subagent"]
    return config.defaults.model


@dataclass(frozen=True)
class _BuildKey:
    generation: int
    target: ModelTarget
    temperature: float
    disable_reasoning: bool


class TaskModels:
    """The model each task uses, from ``models.toml`` or the environment, built once per loop."""

    def __init__(self, source: ConfigSource | None = None) -> None:
        self._source = source
        self._pool: LoopLocal[dict[_BuildKey, ModelHandle]] = LoopLocal(dict)
        self._lock = threading.Lock()
        if source is not None:
            source.subscribe(self._forget_built_models)

    @property
    def origin(self) -> str:
        """Where the models come from now: ``models.toml`` or ``the environment``."""
        return "models.toml" if self._state().configured else "the environment"

    def target(self, task: str) -> ModelTarget:
        """The model *task* uses. No I/O on the ``models.toml`` route."""
        if not is_task(task):
            raise ValueError(f"{task!r} is not a task a model can be assigned to")
        state = self._state()
        if state.snapshot is not None:
            snapshot = state.snapshot
            return target_for(
                snapshot.config,
                reference_for(snapshot.config, task),
                secrets=snapshot.secrets,
                unavailable=snapshot.unavailable,
            )
        if state.problem is not None:
            raise state.problem
        return llm_settings_from_env(env_role(task))

    def handle(
        self, task: str, *, temperature: float, disable_reasoning: bool = False
    ) -> ModelHandle:
        """*task*'s model for the running event loop, built on first use. Needs a running loop."""
        return self._pooled(self.target(task), temperature, disable_reasoning)

    def window(self, target: ModelTarget) -> int:
        """The input window for *target*'s model: its alias, its profile, its family.

        Read off a built model's profile — the pooled one on a running loop, a
        new one where no loop runs yet.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return model_handle(target).context_window
        return self._pooled(target, _WINDOW_TEMPERATURE, False).context_window

    def _pooled(
        self, target: ModelTarget, temperature: float, disable_reasoning: bool
    ) -> ModelHandle:
        key = _BuildKey(self._generation(), target, temperature, disable_reasoning)
        built = self._pool.get()
        handle = built.get(key)
        if handle is None:
            handle = model_handle(
                target, temperature=temperature, disable_reasoning=disable_reasoning
            )
            built[key] = handle
        return handle

    def model(
        self, task: str, *, temperature: float, disable_reasoning: bool = False
    ) -> BaseChatModel:
        """:meth:`handle`'s model."""
        return self.handle(task, temperature=temperature, disable_reasoning=disable_reasoning).model

    def build(
        self, task: str, *, temperature: float, disable_reasoning: bool = False
    ) -> BaseChatModel:
        """A new model for *task*, for code that builds before any loop runs it."""
        target = self.target(task)
        return model_handle(
            target, temperature=temperature, disable_reasoning=disable_reasoning
        ).model

    def _state(self) -> ConfigState:
        return self._source.current() if self._source is not None else ConfigState()

    def _generation(self) -> int:
        snapshot = self._state().snapshot
        return snapshot.generation if snapshot is not None else 0

    def _forget_built_models(self, state: ConfigState) -> None:
        """A new config makes every built model stale; drop them so the next call rebuilds."""
        with self._lock:
            for built in self._pool.instances():
                built.clear()
        logger.info("models config changed (now from %s); models rebuild on next use", self.origin)


__all__ = ["TaskModels", "env_role", "reference_for"]
