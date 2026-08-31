"""TinkerAgent — the TODO board's dedicated thinking partner.

A purpose-built deepagent (see ``docs/design/todo-board.md`` §4) that lives on
one TODO card at a time: it sharpens rough ideas instead of answering them,
decomposes goals into small objectives, asks clarifying questions before
committing to a direction, and persists everything worth keeping onto the
card via the ``todo_*`` tools.

``prompts.py`` renders its system prompt; ``agent.py`` builds the full daemon
stack around :func:`yuyutsava.core.engine.build_tinker_agent`.
"""
