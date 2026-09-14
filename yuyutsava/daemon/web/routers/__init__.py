"""The daemon's HTTP, SSE and WebSocket endpoints, one module per resource.

Each module defines a FastAPI ``router``, and
:func:`yuyutsava.daemon.web.app.create_app` includes them. Most request and
response models live in :mod:`yuyutsava.daemon.web.schemas`; logic shared by
several routers lives in :mod:`yuyutsava.daemon.web.services`.

This package re-exports nothing: import a name from the module that owns it.
"""
