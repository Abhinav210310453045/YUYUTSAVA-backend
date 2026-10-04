"""Each turn runs on the model its conversation uses now — chosen once, kept for the turn.

A conversation's model can change while the agent graph that serves it lives
on: an edit to ``models.toml``, or ``/model`` and the app's model picker, which
record a choice on the session. The graph is never rebuilt for it.
:class:`ModelSwitchMiddleware` sets the model on each call instead
(``ModelRequest.override(model=…)``; the agent factory binds the tools to
whatever model the request carries — pinned by
``test/framework_contract/test_model_switch_contract.py``).

## Pinned per turn

The model is chosen when a turn starts — when new input arrives — and every call
of that turn uses it, including after an approval pause. A switch applies from
the next turn, never inside a tool loop: Anthropic requires the thinking block
of the turn in progress to go back to the model that wrote it, and Gemini 3 its
thought signature.

## How the model is chosen

By a :class:`Chooser` — in the running system
:class:`yuyutsava.core.model_choice.ModelChooser`: the conversation's own
choice, else the task's model in ``models.toml``.

## Who wrote each turn

Every answer is stamped with the reference of the model that wrote it, and
the history each call sends is shaped for its reader
(:mod:`yuyutsava.llm.quirks.history_portable`): the reader's own turns go as
written, every other model's as text and tool calls.

## Why middleware and not a policy

A :class:`~yuyutsava.policy.base.Policy` revises a call through
:class:`~yuyutsava.policy.types.ModelCall`, which carries prompt and tool edits
and deliberately no framework types (ADR-004 keeps the policy package to one
``AgentMiddleware``, the adapter). Choosing the model object and reshaping
assistant messages are the framework's own currency — model plumbing, so it
lives with the models. It is not a policy adapter, so
``collapse_policy_adapters`` leaves it where it is put: first among this
system's middleware, so every later hook sees the chosen model.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage

from yuyutsava.llm.handle import ModelHandle
from yuyutsava.llm.quirks.history_portable import for_reader, stamped
from yuyutsava.ports.models import TaskModelSource
from yuyutsava.run_context import thread_id_from_runtime


class Chooser(Protocol):
    """What the switch asks when a turn starts."""

    #: Where the chosen model is built (``TaskModels``).
    models: TaskModelSource

    async def choose(self, thread_id: str, task: str) -> Any:
        """The target *task*'s turn on *thread_id* runs on (a ``ModelTarget``)."""
        ...


@dataclass(frozen=True)
class _Pin:
    """One turn's model, decided when the turn starts."""

    target: Any  # yuyutsava.llm.target.ModelTarget
    #: The model serving the turn, or ``None`` for the graph's own — the target it
    #: was built from, with ``models.toml`` unchanged since. Settled when the turn
    #: starts, so a reload mid-turn cannot swap even an identical model.
    handle: ModelHandle | None


#: Conversations whose turn pins one agent keeps. A pin outlives its turn so an
#: approval pause resumes on the same model; past this many, the least recent
#: goes, and a turn resumed after that is chosen afresh.
_PINS_KEPT = 512


class ModelSwitchMiddleware(AgentMiddleware):  # type: ignore[misc]
    """Run each model call on the model pinned for its turn, with history shaped for it.

    *built* is the target the graph's model was built from. While a turn's pin
    is that target and ``models.toml`` has not been reloaded since, the call
    keeps the graph's own model; otherwise it runs on the pinned target's model,
    built once per event loop by :class:`~yuyutsava.llm.selection.TaskModels`.
    """

    name = "ModelSwitch"

    def __init__(
        self,
        chooser: Chooser,
        *,
        task: str,
        built: Any,
        temperature: float,
        disable_reasoning: bool = False,
    ) -> None:
        super().__init__()
        self._chooser = chooser
        self._task = task
        self._built = built
        self._built_generation = chooser.models.generation
        self._temperature = temperature
        self._disable_reasoning = disable_reasoning
        self._pins: OrderedDict[str, _Pin] = OrderedDict()

    def active(self, thread_id: str) -> ModelHandle | None:
        """The model *thread_id*'s current turn runs on, when it is not the graph's own.

        ``None`` means the graph's model: no turn has started, or nothing changed.
        What the context and usage hooks ask, so they measure the model in use.
        """
        pin = self._pins.get(thread_id)
        return pin.handle if pin is not None else None

    async def abefore_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """A turn starts: choose its model. Not run when a paused turn resumes."""
        await self._begin(thread_id_from_runtime(default=""))
        return None

    async def awrap_model_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        thread_id = thread_id_from_runtime(default="")
        # No pin: the turn started before this process did (an approval resumed
        # after a restart). Choose now; the turn keeps it from here.
        pin = self._pins.get(thread_id) or await self._begin(thread_id)
        model = request.model if pin.handle is None else pin.handle.model
        reference = pin.target.reference
        response = await handler(
            request.override(model=model, messages=for_reader(request.messages, reference))
        )
        return _stamp(response, reference)

    async def _begin(self, thread_id: str) -> _Pin:
        target = await self._chooser.choose(thread_id, self._task)
        models = self._chooser.models
        built = target == self._built and models.generation == self._built_generation
        handle = (
            None
            if built
            else models.pooled(
                target, temperature=self._temperature, disable_reasoning=self._disable_reasoning
            )
        )
        pin = self._pins[thread_id] = _Pin(target, handle)
        self._pins.move_to_end(thread_id)
        while len(self._pins) > _PINS_KEPT:
            self._pins.popitem(last=False)
        return pin


def _stamp(response: Any, reference: str) -> Any:
    """*response* with each answer naming the model that wrote it.

    A handler hands back a ``ModelResponse``, an ``ExtendedModelResponse``
    wrapping one, or — from a middleware that answers itself — an ``AIMessage``.
    """
    if isinstance(response, AIMessage):
        return stamped(response, reference)
    inner = getattr(response, "model_response", None)
    if inner is not None:
        response.model_response = _stamp(inner, reference)
        return response
    result = getattr(response, "result", None)
    if result is not None:
        response.result = [
            stamped(message, reference) if isinstance(message, AIMessage) else message
            for message in result
        ]
    return response


__all__ = ["Chooser", "ModelSwitchMiddleware"]
