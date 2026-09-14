"""Rendering library for charts, diagrams, styled tables, code images, math and timelines.

:func:`~yuyutsava.visuals.render.render` has no dependency on LangChain, FastAPI or the
agent runtime, so the same renderers serve the agent tools
(:mod:`yuyutsava.visuals.tools`) and the daemon's routes
(:mod:`yuyutsava.daemon.web.routers.visuals`). Results persist through
:class:`~yuyutsava.visuals.store.VisualStore`.

This package re-exports nothing: import a name from the module that owns it.
"""
