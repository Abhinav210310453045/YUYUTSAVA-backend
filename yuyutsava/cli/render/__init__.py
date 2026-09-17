"""Terminal rendering for the CLI: the ``chat`` REPL and the one-shot run.

``chat_repl`` picks one of three display modes with ``dashboard.display_mode``: the
two-pane dashboard, the rich inline transcript, or plain ANSI lines for pipes and
dumb terminals. The one-shot ``yuyutsava <task>`` run picks nothing — it streams
straight to stderr through :mod:`~yuyutsava.cli.render.terminal_stream`.

- :mod:`~yuyutsava.cli.render.plain` — ``ChatRenderer``, the plain renderer and the
  base class of the rich one
- :mod:`~yuyutsava.cli.render.renderer` — ``RichChatRenderer``: live spinner, tool-call
  lines with result summaries, streamed markdown answers
- :mod:`~yuyutsava.cli.render.dashboard` — ``ChatDashboard``, the transcript beside a
  live context column, and ``display_mode``
- :mod:`~yuyutsava.cli.render.console` — the shared rich console, and ``rich_capable``
- :mod:`~yuyutsava.cli.render.terminal_stream` — ``astream_agent``, the one-shot run's
  stderr stream, and ``prompt_permission``, the interrupt prompt it reads on stdin
- helpers: ``panels`` (permission and question cards), ``markdown_stream``,
  ``tool_format`` (one-line tool summaries), ``context_panel``, ``artifact_view``,
  ``ansi_wrap``

This package re-exports nothing: import a name from the module that owns it.
"""
