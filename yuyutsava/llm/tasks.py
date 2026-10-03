"""The jobs a model can be assigned to — one closed list.

A *task* is a job YUYUTSAVA gives a model: answering in a chat, triaging an
event, summarising a long conversation. ``models.toml`` names a model per task
under ``[tasks]``, and a task without an entry uses ``defaults.model``
(docs/architecture/decisions/ADR-005-models-config.md §1).

Subagents are a family: ``subagent`` sets the model of every subagent, and
``subagent.<name>`` the model of one of them, by the name it is registered under.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Prefix of a task that names one subagent: ``subagent.researcher``.
SUBAGENT_PREFIX = "subagent."


@dataclass(frozen=True)
class Task:
    """One job a model can be assigned to."""

    #: The key under ``[tasks]`` in ``models.toml``.
    name: str
    #: What a person sees for it in the wizard, the app and ``/model``.
    label: str


TASKS: tuple[Task, ...] = (
    Task("chat", "Chat — the CLI, the app's chat and voice"),
    Task("orchestrator", "Background tasks and Tinker"),
    Task("triage", "Triage of incoming events"),
    Task("subagent", "Subagents"),
    Task("compaction", "Summaries of long conversations"),
    Task("tier_light", "Smart routing — light tasks"),
    Task("tier_standard", "Smart routing — standard tasks"),
    Task("tier_heavy", "Smart routing — heavy tasks"),
)

_NAMES = frozenset(task.name for task in TASKS)


def is_task(name: str) -> bool:
    """Whether *name* is a task ``models.toml`` may assign a model to."""
    if name.startswith(SUBAGENT_PREFIX):
        return len(name) > len(SUBAGENT_PREFIX)
    return name in _NAMES


def task_names() -> tuple[str, ...]:
    """Every task name, in the order people are shown them."""
    return tuple(task.name for task in TASKS)


__all__ = ["SUBAGENT_PREFIX", "TASKS", "Task", "is_task", "task_names"]
