"""TODO board — the user's global planning/thinking surface.

Every producer/consumer (REST, `todo_*` tools, master, TinkerAgent) goes
through :class:`~yuyutsava.todoboard.exchange.TodoExchange` and its versioned
schemas — never the store tables directly. See ``docs/design/todo-board.md``.
"""
