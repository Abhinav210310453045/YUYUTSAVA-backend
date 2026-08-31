"""Reusable per-provider wire-format fixes.

A quirk lives here rather than inside one provider module when more than one
provider shares it — ``gemini_parts`` is needed by BOTH ``providers/vertex.py``
and ``providers/google.py``, because it is a property of the Gemini wire format
rather than of either SDK.

Each module is self-contained and states, in its docstring, the upstream
behaviour that forces it to exist — so it can be deleted without archaeology once
that behaviour changes.
"""
