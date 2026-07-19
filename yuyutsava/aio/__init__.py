"""Asyncio infrastructure shared across subsystems.

Deliberately dependency-free (stdlib only) so leaf modules — storage, memory,
llm quirks — can import it without dragging in ``yuyutsava.core`` (whose
``__init__`` eagerly imports the engine and therefore langgraph).
"""

from yuyutsava.aio.loop_local import LoopLocal

__all__ = ["LoopLocal"]
