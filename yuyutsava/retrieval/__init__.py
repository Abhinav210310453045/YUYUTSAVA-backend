"""Semantic retrieval shared by memory and skills recall.

- :mod:`~yuyutsava.retrieval.hit` — ``Hit``, a search result
- :mod:`~yuyutsava.retrieval.store` — ``VectorStore``, the search contract consumers depend on
- :mod:`~yuyutsava.retrieval.pg` — ``PgVectorTable`` and ``PgVectorSearch``, one pgvector
  engine parametrised by table and column names
- :mod:`~yuyutsava.retrieval.injector` — ``RetrievalInjector``, a capped prompt block built
  from hits, which never raises
- :mod:`~yuyutsava.retrieval.vector` and :mod:`~yuyutsava.retrieval.keyword` — shared helpers

Embeddings come from :class:`~yuyutsava.memory.embedder.Embedder`. This package
re-exports nothing: import a name from the module that owns it.
"""
