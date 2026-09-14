"""Terminal rendering for the ``yuyutsava chat`` REPL.

``chat_repl`` picks one of three display modes with ``dashboard.display_mode``: the
two-pane dashboard, the rich inline transcript, or plain ANSI lines for pipes and
dumb terminals.

- :mod:`~yuyutsava.cli.render.plain` — ``ChatRenderer``, the plain renderer and the
  base class of the rich one
- :mod:`~yuyutsava.cli.render.renderer` — ``RichChatRenderer``: live spinner, tool-call
  lines with result summaries, streamed markdown answers
- :mod:`~yuyutsava.cli.render.dashboard` — ``ChatDashboard``, the transcript beside a
  live context column, and ``display_mode``
- :mod:`~yuyutsava.cli.render.console` — the shared rich console, and ``rich_capable``
- helpers: ``panels`` (permission and question cards), ``markdown_stream``,
  ``tool_format`` (one-line tool summaries), ``context_panel``, ``artifact_view``,
  ``ansi_wrap``

This package re-exports nothing: import a name from the module that owns it.
"""
