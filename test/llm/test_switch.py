"""A conversation's turn runs on the model chosen when it started, and never changes mid-turn.

Real agent graphs (``create_agent``) with scripted models; nothing is sent. The
"models" are fakes keyed by target, so each answer names the model that wrote it.
"""

from __future__ import annotations

import asyncio
import logging
import unittest
from pathlib import Path
from typing import Any

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command, interrupt
from pydantic import Field

from yuyutsava.core.model_choice import ModelChooser
from yuyutsava.llm.config.loader import load_text
from yuyutsava.llm.handle import ModelHandle
from yuyutsava.llm.quirks.history_portable import STAMP, writer_of
from yuyutsava.llm.switch import ModelSwitchMiddleware
from yuyutsava.llm.target import target_for
from yuyutsava.storage.models import ALL_SUBAGENTS, ModelChoice, Session
from yuyutsava.storage.sessions.store import SessionNotFound

CONFIG = load_text(
    "version = 1\n"
    '[defaults]\nmodel = "local/a"\n'
    '[providers.local]\ntype = "ollama"\n'
    '[providers.other]\ntype = "ollama"\n'
)


def _target(reference: str):
    return target_for(CONFIG, reference, secrets={}, unavailable={})


class _Scripted(FakeMessagesListChatModel):
    """Says its own name (or calls a tool first, when told to), and records what it was sent."""

    seen: list[list[BaseMessage]] = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        return super()._generate(messages, stop, run_manager, **kwargs)


def _says(name: str, *, calls: str | None = None) -> _Scripted:
    first = (
        AIMessage(
            content=[{"type": "thinking", "thinking": "t", "signature": f"{name}-SIG"}],
            tool_calls=[{"name": calls, "args": {}, "id": f"call-{name}", "type": "tool_call"}],
        )
        if calls
        else AIMessage(content=name)
    )
    responses = [first, AIMessage(content=name)] if calls else [first]
    return _Scripted(responses=responses)


class _Models:
    """A TaskModelSource double: each task's reference, and one scripted model per target."""

    def __init__(self, **models: _Scripted) -> None:
        self.tasks: dict[str, str] = {"chat": "local/a", "subagent": "local/a"}
        self.models = models
        self.generation = 1
        self.built: list[str] = []

    def target(self, task: str):
        key = "subagent" if task.startswith("subagent.") else task
        return _target(self.tasks[key])

    def resolve(self, reference: str):
        return _target(reference)

    def pooled(self, target, *, temperature: float, disable_reasoning: bool = False):
        self.built.append(target.reference)
        return ModelHandle(model=self.models[target.model], name=target.model, provider=target.type)

    def edit(self, task: str, reference: str) -> None:
        """What a models.toml edit does: a new generation with a new task model."""
        self.tasks[task] = reference
        self.generation += 1


class _Sessions:
    """A session store that knows one conversation's model choice."""

    def __init__(self, choices: dict[str, ModelChoice]) -> None:
        self.choices = choices

    async def get(self, session_id: str) -> Session:
        if session_id not in self.choices:
            raise SessionNotFound(session_id)
        return Session(
            id=session_id,
            thread_id=session_id,
            workspace=Path("/w"),
            status="idle",
            created_at=0.0,
            updated_at=0.0,
            message_count=0,
            memory_files_count=0,
            db_row_bytes=0,
            task_preview="",
            models=self.choices[session_id],
        )


def _agent(built_model, models: _Models, *, task="chat", sessions=None, tools=(), saver=None):
    switch = ModelSwitchMiddleware(
        ModelChooser(models, sessions),
        task=task,
        built=_target("local/a"),
        temperature=0.1,
    )
    agent = create_agent(
        built_model, tools=list(tools), middleware=[switch], checkpointer=saver or InMemorySaver()
    )
    return agent, switch


def _ask(agent, text: str, thread: str = "t1") -> list[BaseMessage]:
    config = {"configurable": {"thread_id": thread}}
    return asyncio.run(agent.ainvoke({"messages": [HumanMessage(text)]}, config))["messages"]


class NothingChanged(unittest.TestCase):
    def test_the_graphs_own_model_answers_and_nothing_is_built(self) -> None:
        models = _Models()
        agent, switch = _agent(_says("built"), models)
        self.assertEqual(_ask(agent, "hi")[-1].content, "built")
        self.assertEqual(models.built, [])
        self.assertIsNone(switch.active("t1"))

    def test_the_answer_names_the_model_that_wrote_it(self) -> None:
        agent, _ = _agent(_says("built"), _Models())
        self.assertEqual(writer_of(_ask(agent, "hi")[-1]), "local/a")


class AnEdit(unittest.TestCase):
    def test_applies_from_the_next_turn(self) -> None:
        models = _Models(b=_says("b"))
        agent, switch = _agent(_says("built"), models)
        self.assertEqual(_ask(agent, "one")[-1].content, "built")
        models.edit("chat", "local/b")
        messages = _ask(agent, "two")
        self.assertEqual(messages[-1].content, "b")
        self.assertEqual(writer_of(messages[-1]), "local/b")
        self.assertEqual(switch.active("t1").name, "b")

    def test_a_rotated_key_rebuilds_the_same_model(self) -> None:
        models = _Models(a=_says("rebuilt a"))
        agent, _ = _agent(_says("built"), models)
        models.generation += 1  # same target, new load (a new secret, say)
        self.assertEqual(_ask(agent, "hi")[-1].content, "rebuilt a")

    def test_never_applies_inside_a_tool_loop(self) -> None:
        models = _Models(b=_says("b"))

        @tool
        def look() -> str:
            """Look around."""
            models.edit("chat", "local/b")  # the edit lands mid-turn
            return "seen"

        agent, _ = _agent(_says("built", calls="look"), models, tools=[look])
        self.assertEqual(_ask(agent, "go")[-1].content, "built")
        self.assertEqual(_ask(agent, "again")[-1].content, "b")

    def test_a_switched_turn_keeps_its_model_through_a_reload(self) -> None:
        models = _Models(b=_says("b", calls="look"))

        @tool
        def look() -> str:
            """Look around."""
            models.generation += 1  # models.toml reloads mid-turn
            return "seen"

        agent, _ = _agent(_says("built"), models, tools=[look])
        models.edit("chat", "local/b")
        self.assertEqual(_ask(agent, "go")[-1].content, "b")
        self.assertEqual(models.built, ["local/b"])

    def test_a_paused_turn_resumes_on_its_own_model(self) -> None:
        models = _Models(b=_says("b"))

        @tool
        def approve() -> str:
            """Ask first."""
            return str(interrupt("ok?"))

        agent, _ = _agent(_says("built", calls="approve"), models, tools=[approve])
        config = {"configurable": {"thread_id": "t1"}}
        asyncio.run(agent.ainvoke({"messages": [HumanMessage("go")]}, config))
        models.edit("chat", "local/b")  # changed while the turn waits for approval
        result = asyncio.run(agent.ainvoke(Command(resume="yes"), config))
        self.assertEqual(result["messages"][-1].content, "built")


class ASwitchedConversation(unittest.TestCase):
    def test_sends_the_previous_models_turns_as_text_and_tool_calls(self) -> None:
        b = _says("b")
        models = _Models(b=b)
        agent, _ = _agent(_says("built", calls="look"), models, tools=[_look])
        _ask(agent, "go")
        models.edit("chat", "local/b")
        _ask(agent, "and now?")

        earlier = [m for m in b.seen[-1] if isinstance(m, AIMessage)]
        self.assertEqual(earlier[0].content, "")  # the thinking block stayed behind
        self.assertEqual([c["id"] for c in earlier[0].tool_calls], ["call-built"])
        self.assertNotIn(STAMP, earlier[0].response_metadata)


@tool
def _look() -> str:
    """Look around."""
    return "seen"


class ASessionsChoice(unittest.TestCase):
    def test_wins_over_the_task(self) -> None:
        models = _Models(b=_says("b"))
        sessions = _Sessions({"t1": ModelChoice(model="other/b")})
        agent, _ = _agent(_says("built"), models, sessions=sessions)
        messages = _ask(agent, "hi")
        self.assertEqual(messages[-1].content, "b")
        self.assertEqual(writer_of(messages[-1]), "other/b")

    def test_a_thread_without_a_session_follows_the_task(self) -> None:
        agent, _ = _agent(_says("built"), _Models(), sessions=_Sessions({}))
        self.assertEqual(_ask(agent, "hi", thread="no-session")[-1].content, "built")

    def test_a_model_models_toml_no_longer_has_falls_back_and_says_so(self) -> None:
        sessions = _Sessions({"t1": ModelChoice(model="gone")})
        agent, _ = _agent(_says("built"), _Models(), sessions=sessions)
        with self.assertLogs("yuyutsava.core.model_choice", logging.WARNING) as logs:
            self.assertEqual(_ask(agent, "hi")[-1].content, "built")
        self.assertIn("'gone'", logs.output[0])

    def test_a_subagent_reads_its_own_entry(self) -> None:
        models = _Models(b=_says("b"))
        sessions = _Sessions(
            {"t1": ModelChoice(model="local/a", subagents=((ALL_SUBAGENTS, "other/b"),))}
        )
        agent, _ = _agent(_says("built"), models, task="subagent.researcher", sessions=sessions)
        self.assertEqual(_ask(agent, "hi")[-1].content, "b")


class Stamping(unittest.TestCase):
    def test_reaches_an_extended_response(self) -> None:
        from langchain.agents.middleware.types import ExtendedModelResponse, ModelResponse

        from yuyutsava.llm import switch as model_switch

        response = ExtendedModelResponse(model_response=ModelResponse(result=[AIMessage("x")]))
        stamped: Any = model_switch._stamp(response, "local/a")
        self.assertEqual(writer_of(stamped.model_response.result[0]), "local/a")


if __name__ == "__main__":
    unittest.main()
