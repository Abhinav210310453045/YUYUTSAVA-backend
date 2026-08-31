"""Rich terminal rendering for the ``yuyutsava chat`` REPL.

Claude-Code-style inline transcript: live spinner, tool-call lines with
✓/✗ result summaries, block-streamed markdown answers, humanized
permission/question cards. The plain ANSI ``ChatRenderer`` in
``chat_repl.py`` remains the fallback for pipes / dumb terminals — this
package is only imported when :func:`console.rich_capable` says yes.
"""
