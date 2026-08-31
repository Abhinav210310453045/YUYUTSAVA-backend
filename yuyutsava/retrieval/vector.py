"""pgvector text-literal helper.

Vectors travel to Postgres as pgvector's text literal (``'[0.1,0.2,…]'::vector``)
so no per-connection type registration is needed through the pool. Shared by
every semantic store (memory, skills, …) built on this package.
"""

from __future__ import annotations


def vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{v:.7g}" for v in vec) + "]"
