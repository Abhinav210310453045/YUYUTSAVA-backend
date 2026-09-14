"""Artifacts produced inline in chat and voice replies, outside any TODO card.

They use the same block types the TODO board pins to cards (JSX sandbox, audio,
HTML, diagram, text), through the one block registry in
:mod:`yuyutsava.todoboard.artifacts`. Each artifact is a directory under
``blobs/artifacts/<id>/`` holding the produced file and a ``meta.json`` record; the
daemon serves it at ``/artifacts/{id}``.

This package re-exports nothing: import a name from the module that owns it.
"""
