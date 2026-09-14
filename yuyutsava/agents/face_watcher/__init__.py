"""The face-watcher subagent.

- :mod:`~yuyutsava.agents.face_watcher.agent` — ``FaceWatcherAgent``: takes one
  ``face.frame`` event, identifies the face with a tool from the MCP server scoped
  to ``"face-watcher"``, and returns a one-line summary
- :mod:`~yuyutsava.agents.face_watcher.prompts` — ``FACE_WATCHER_PROMPT``

No face-recognition tools are bundled. This package re-exports nothing: import a
name from the module that owns it.
"""
