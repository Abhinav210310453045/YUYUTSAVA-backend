"""DeepFace MCP server — face detection, identification, enrollment.

Runs as a separate process; the daemon talks to it over MCP stdio. State
(identity → embeddings) lives under ``~/.yuyutsava/deepface/``.

Per PHASE_2 §2.
"""
