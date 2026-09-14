"""Agent assembly: the builders that turn settings, stores and policies into a runnable graph.

Import a name from the module that owns it — ``yuyutsava.core.engine``,
``yuyutsava.core.streaming``, ``yuyutsava.core.config`` — never from this package.

This file deliberately imports nothing. Python runs a package's ``__init__.py``
before any module inside it, so every import placed here is paid by every
``yuyutsava.core.*`` import, however small: re-exporting the engine made importing
the settings module load the entire agent stack, and closed import cycles through
every package that needed a setting (docs/code-review/v0.1.0/03-imports-and-packages.md).
"""
