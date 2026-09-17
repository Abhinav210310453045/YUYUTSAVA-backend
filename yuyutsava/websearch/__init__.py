"""Web search for agents: the ``ws_*`` tool family over Tavily and Exa.

:func:`~yuyutsava.websearch.tools.make_search_tools` returns only the tools whose
provider key is configured, so it is safe to call unconditionally — no keys means
no tools rather than a failure at build time.

The name says *web* deliberately. :mod:`yuyutsava.discovery` also searches, but
it searches the agent's own catalog of tools and skills; this package queries the
internet through third-party APIs. They were never the same concern, which is why
this did not fold into ``discovery/`` (R-PLC-05).

This package re-exports nothing: import a name from the module that owns it.
"""
