"""The JSON Schema of ``models.toml``, for editors that complete and check it as it is typed.

TOML editors (Even Better TOML / Taplo, IntelliJ) read a JSON Schema named in
the file's first line (``#:schema ./models.schema.json``) and then offer each
setting, each provider ``type`` and each login ``method`` as they are typed. The
schema is generated from :mod:`.schema`, so it cannot drift from what the loader
accepts; the loader still has the last word, since cross-references (a task's
model, a model's provider) are beyond what a schema can say.
"""

from __future__ import annotations

from typing import Any

from yuyutsava.llm.config.schema import ModelsConfig

_DIALECT = "https://json-schema.org/draft/2020-12/schema"


def models_config_json_schema() -> dict[str, Any]:
    """The JSON Schema of ``models.toml``."""
    schema = ModelsConfig.model_json_schema(mode="validation")
    schema["$schema"] = _DIALECT
    schema["title"] = "YUYUTSAVA models.toml"
    schema["description"] = (
        "Every model provider YUYUTSAVA may call, how it logs in, the models, and which "
        "model each task uses. Secrets are references (secret:, env:, file:), never values."
    )
    return schema


__all__ = ["models_config_json_schema"]
