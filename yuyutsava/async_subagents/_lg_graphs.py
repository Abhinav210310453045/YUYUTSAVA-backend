"""Module exposing compiled subagent graphs by Python-identifier name.

``langgraph_api.cli.run_server(graphs={...})`` requires each graph value to be
a ``"module:variable"`` string (see ``langgraph_api/graph.py:collect_graphs_from_env``).
The graph loader imports the module and reads the attribute via
``module.__dict__[variable]`` (graph.py:751) — note this bypasses any PEP 562
``__getattr__`` hook, so we must write graphs straight into ``globals()``.

``AsyncSubagentHost`` calls :func:`register` with each compiled graph before
invoking ``run_server``; that injects an entry into this module's namespace
which the loader then finds via the dict lookup.

Graph IDs containing characters that aren't valid Python identifiers
(e.g. ``"file-organizer"``) are mapped to attribute names by replacing
``-`` and ``.`` with ``_``.
"""

from __future__ import annotations


def attr_name_for_graph_id(graph_id: str) -> str:
    """Map a public graph_id (kebab-case allowed) to a Python attr name."""
    return graph_id.replace("-", "_").replace(".", "_")


def register(graph_id: str, graph: object) -> str:
    """Register a compiled graph and return the module attribute name used.

    The returned attribute name is what gets embedded in the
    ``"module:variable"`` path passed to ``run_server(graphs=...)``.
    """
    attr = attr_name_for_graph_id(graph_id)
    globals()[attr] = graph
    return attr


def unregister(graph_id: str) -> None:
    globals().pop(attr_name_for_graph_id(graph_id), None)
