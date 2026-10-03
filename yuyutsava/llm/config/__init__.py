"""yuyutsava.llm.config — the models config file: its shape, how it is read, written and watched.

``~/.yuyutsava/models.toml`` is the single place model configuration lives: every
provider and how to log in to it, the models, and which model each task uses
(docs/architecture/decisions/ADR-005-models-config.md).

    schema         the file's shape, as pydantic models
    loader         text → config, with every problem reported as path, line, problem and fix
    messages       the examples and names those problems are explained with
    secrets        secret references (secret:/env:/file:) and how their values are found
    store          the one writer: locked, atomic, comment-preserving, never invalid
    watch          a watcher thread that publishes each valid edit as an immutable snapshot
    schema_export  the JSON Schema editors use to complete and check the file

This file imports nothing: import a name from the module that owns it.
"""
