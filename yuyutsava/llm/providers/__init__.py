"""One module per LLM provider, and the registry that picks one.

:func:`~yuyutsava.llm.providers.registry.provider_for` maps a settings object to the
provider that builds its chat model. This package re-exports nothing: import a name
from the module that owns it.
"""
