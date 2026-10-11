"""The JSON Schema of ``models.toml``, for editors that complete and check it as it is typed.

TOML editors (Even Better TOML / Taplo, IntelliJ) read a JSON Schema named in
the file's first line (``#:schema ./models.schema.json``) and then offer each
setting, each provider ``type`` and each login ``method`` as they are typed. The
schema is generated from :mod:`.schema`, so it cannot drift from what the loader
accepts; the loader still has the last word, since cross-references (a task's
model, a model's provider) are beyond what a schema can say.
"""

from __future__ import annotations

import re
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


# ─── the reference page ───────────────────────────────────────────────────

_REFERENCE_HEADER = """\
# `models.toml` reference

<!-- Generated from the schema by `yuyutsava models schema --markdown`. Do not edit:
     change the schema (yuyutsava/llm/config/schema.py) and regenerate. -->

Every setting `~/.yuyutsava/models.toml` accepts. For what to write and why — a
copy-paste block per provider and login — read the
[models guide](../guides/models.md). The loader has the last word: it also
checks what a schema cannot, such as a task naming a model the file defines.
`yuyutsava models doctor` checks a file without calling anyone.
"""


def models_config_reference() -> str:
    """``models.toml``'s reference page in Markdown, from its JSON Schema.

    The page at ``docs/reference/models-config.md``; a test keeps that file equal
    to this, so the page cannot drift from what the loader accepts.
    """
    schema = models_config_json_schema()
    defs = schema["$defs"]
    top = schema["properties"]
    lines = [_REFERENCE_HEADER]
    lines += ["## The file", "", *_rows(top, schema.get("required", []), defs)]
    lines += ["## `[defaults]`", "", *_table_of(defs["Defaults"], defs)]
    lines += ["## `[tasks]`", "", _text(top["tasks"]["description"]), ""]
    lines += ["## `[providers.<name>]`", "", _text(top["providers"]["description"]), ""]
    union = next(iter(top["providers"]["patternProperties"].values()))
    for ref in union["oneOf"]:
        lines += _provider(_def(ref, defs), defs)
    lines += ["## `[models.<alias>]`", "", *_table_of(defs["ModelEntry"], defs)]
    lines += ["### `thinking`", "", *_table_of(defs["Thinking"], defs)]
    lines += ["### `price`", "", *_table_of(defs["Price"], defs)]
    lines += ["## `[security]`", "", *_table_of(defs["Security"], defs)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).rstrip() + "\n"


def _def(ref: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    return defs[ref["$ref"].rsplit("/", 1)[-1]]


def _table_of(
    table: dict[str, Any], defs: dict[str, Any], *, skip: tuple[str, ...] = ()
) -> list[str]:
    head = [_text(table["description"]), ""] if table.get("description") else []
    props = {k: v for k, v in table.get("properties", {}).items() if k not in skip}
    return head + _rows(props, table.get("required", []), defs)


def _rows(props: dict[str, Any], required: list[str], defs: dict[str, Any]) -> list[str]:
    if not props:
        return []
    rows = ["| Setting | Value | | Meaning |", "|---|---|---|---|"]
    for name, prop in props.items():
        if "$ref" in prop and not prop.get("description"):
            prop = {**prop, "description": _def(prop, defs).get("description", "")}
        need = "required" if name in required else _default(prop)
        meaning = _text(prop.get("description", "")).replace("|", "\\|")
        rows.append(f"| `{name}` | {_type(prop, defs)} | {need} | {meaning} |")
    return [*rows, ""]


def _provider(connection: dict[str, Any], defs: dict[str, Any]) -> list[str]:
    kind = connection["properties"]["type"]["const"]
    lines = [f'### `type = "{kind}"`', ""]
    lines += _table_of(connection, defs, skip=("type", "auth"))
    auth = connection["properties"]["auth"]
    logins = [_def(ref, defs) for ref in auth.get("oneOf", [auth])]
    lines += ["Logins — `auth = { method = … }`:", ""]
    for login in logins:
        method = login["properties"]["method"]["const"]
        fields = [
            f'`{name} = "secret:…"`' if _is_reference(prop) else f"`{name} = …`"
            for name, prop in login["properties"].items()
            if name != "method"
        ]
        extra = f" — with {', '.join(fields)}" if fields else ""
        lines.append(f"- `{method}`{extra}: {_text(login.get('description', ''))}")
    if "default" in auth:
        lines.append(f"- left out: `{_toml(auth['default'])}`")
    return [*lines, ""]


def _text(text: str) -> str:
    """*text* as Markdown shows it: ``<name>`` is not an HTML tag."""
    return text.replace("<", "&lt;").replace(">", "&gt;")


def _is_reference(prop: dict[str, Any]) -> bool:
    """Whether *prop* holds a secret reference (``secret:``, ``env:``, ``file:``)."""
    kinds = prop.get("anyOf", [prop])
    return any(k.get("description", "").startswith("A reference") for k in [prop, *kinds])


def _type(prop: dict[str, Any], defs: dict[str, Any]) -> str:
    if "const" in prop:
        return f"`{_toml(prop['const'])}`"
    if "enum" in prop:
        return " · ".join(f"`{_toml(v)}`" for v in prop["enum"])
    if "anyOf" in prop:
        kinds = [k for k in prop["anyOf"] if k.get("type") != "null"]
        return " or ".join(_type(k, defs) for k in kinds)
    if "$ref" in prop or prop.get("type") == "object":
        return "table"
    return {"string": "text", "integer": "whole number", "number": "number"}.get(
        prop.get("type", ""), prop.get("type", "")
    ).replace("boolean", "true · false")


def _default(prop: dict[str, Any]) -> str:
    if "default" not in prop or prop["default"] in (None, {}, []):
        return "optional"
    return f"default `{_toml(prop['default'])}`"


def _toml(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k} = {_toml(v)}" for k, v in value.items()) + "}"
    if isinstance(value, str):
        return f'"{value}"'
    return str(value)


__all__ = ["models_config_json_schema", "models_config_reference"]
