"""Reading ``models.toml``: text in, a validated config out, or every problem explained.

A file that does not load raises :class:`ModelsConfigError` listing *all* of its
problems at once. Each one is a :class:`ConfigIssue`:

* the setting's path, as it would be written (``providers.aws.auth.method``);
* the line it is on, when it can be found;
* what is wrong, in the file's own terms;
* the fix — usually the exact line to write.

Two passes. The first is :mod:`.schema` — structure, types, which login each
provider type accepts — with pydantic's errors translated here into those terms.
The second, run only on a file that passes the first, checks references: that the
default and each task name a model that exists, and that each model's provider
does.
"""

from __future__ import annotations

import difflib
import re
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from yuyutsava.llm.config.messages import (
    LOGIN_EXAMPLES,
    REFERENCE,
    SETTING_EXAMPLES,
    login_example,
    type_label,
)
from yuyutsava.llm.config.schema import (
    CONNECTIONS,
    ModelEntry,
    ModelsConfig,
    connection_type,
    connection_types,
    login_methods,
    parse_model_ref,
)
from yuyutsava.llm.tasks import SUBAGENT_PREFIX, is_task, task_names


@dataclass(frozen=True)
class ConfigIssue:
    """One problem in ``models.toml``, and how to fix it."""

    #: The setting, as written in the file; ``""`` for the file as a whole.
    path: str
    problem: str
    fix: str
    line: int | None = None


class ModelsConfigError(ValueError):
    """``models.toml`` does not load; :attr:`issues` says why, all at once."""

    def __init__(self, source: str, issues: Sequence[ConfigIssue]) -> None:
        self.source = source
        self.issues = tuple(issues)
        super().__init__(self.__str__())

    def __str__(self) -> str:
        count = len(self.issues)
        lines = [f"{self.source} has {count} problem{'s' if count != 1 else ''}:"]
        for issue in self.issues:
            where = " · ".join(p for p in (_line_label(issue.line), issue.path) if p)
            fix = issue.fix.replace("\n", "\n          ")
            lines += ["", f"  {where or 'the file'}", f"    {issue.problem}", f"    Fix:  {fix}"]
        return "\n".join(lines)


def load_text(text: str, *, source: str = "models.toml") -> ModelsConfig:
    """Validate *text* as ``models.toml``; *source* names it in messages."""
    data = _parse_toml(text, source)
    try:
        config = ModelsConfig.model_validate(data)
    except ValidationError as exc:
        raise ModelsConfigError(source, _structural_issues(exc, text)) from exc
    issues = reference_issues(config)
    if issues:
        raise ModelsConfigError(source, [_with_line(i, text) for i in issues])
    return config


def load_file(path: Path) -> ModelsConfig:
    """Read and validate the ``models.toml`` at *path*."""
    return load_text(path.read_text(encoding="utf-8"), source=str(path))


# ─── pass 0: TOML ─────────────────────────────────────────────────────────

_TOML_LINE = re.compile(r"line (\d+)")


def _parse_toml(text: str, source: str) -> dict[str, Any]:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        found = _TOML_LINE.search(str(exc))
        issue = ConfigIssue(
            path="",
            problem=f"this is not valid TOML: {exc}",
            fix=(
                'check that line: text values need quotes ("…"), tables are written [like.this], '
                "and every [ or { is closed"
            ),
            line=int(found.group(1)) if found else None,
        )
        raise ModelsConfigError(source, [issue]) from exc


# ─── pass 1: structure ────────────────────────────────────────────────────

_METHODS = frozenset(LOGIN_EXAMPLES)
_TYPES = frozenset(connection_types())
_CLASS_BY_TYPE = {connection_type(cls): cls for cls in CONNECTIONS}


def _known_settings() -> list[str]:
    names = set(ModelsConfig.model_fields) | set(ModelEntry.model_fields)
    for cls in CONNECTIONS:
        names |= set(cls.model_fields)
        annotation = cls.model_fields["auth"].annotation
        for login in getattr(annotation, "__args__", None) or (annotation,):
            names |= set(getattr(login, "model_fields", {}))
    return sorted(names)


_SETTINGS = _known_settings()


@dataclass(frozen=True)
class _Located:
    """A pydantic error with its location in the file's own terms."""

    error: dict[str, Any]
    #: Path as written in the file, discriminator tags removed.
    parts: tuple[str, ...]
    #: The provider ``type`` the error sits under, when it does.
    provider_type: str | None


def _locate(error: dict[str, Any]) -> _Located:
    raw = [str(p) for p in error["loc"]]
    provider_type = None
    if len(raw) > 2 and raw[0] == "providers" and raw[2] in _TYPES:
        provider_type = raw.pop(2)
    if "auth" in raw:
        after = raw.index("auth") + 1
        if after < len(raw) and raw[after] in _METHODS and after + 1 < len(raw):
            raw.pop(after)
    if raw and raw[-1] == "[key]":
        raw.pop()
    return _Located(error, tuple(raw), provider_type)


def _structural_issues(exc: ValidationError, text: str) -> list[ConfigIssue]:
    located = [_locate(e) for e in exc.errors()]
    issues = [_translate(item) for item in located]
    return [_with_line(i, text) for i in _drop_noise(issues, located)]


def _drop_noise(issues: list[ConfigIssue], located: list[_Located]) -> list[ConfigIssue]:
    """Once a login's method is wrong, its other errors only describe the wrong form."""
    wrong_method = {
        ".".join(item.parts[: item.parts.index("auth") + 1])
        for item in located
        if "auth" in item.parts and _is_method_error(item)
    }
    kept = []
    for issue, item in zip(issues, located, strict=True):
        under = any(issue.path.startswith(p + ".") for p in wrong_method)
        if under and not _is_method_error(item):
            continue
        kept.append(issue)
    return kept


def _is_method_error(item: _Located) -> bool:
    kind = item.error["type"]
    if kind in ("union_tag_invalid", "union_tag_not_found"):
        return _discriminator(item) == "method"
    return kind == "literal_error" and item.parts[-2:] == ("auth", "method")


def _discriminator(item: _Located) -> str:
    return str((item.error.get("ctx") or {}).get("discriminator", "")).strip("'")


def _translate(item: _Located) -> ConfigIssue:
    kind = item.error["type"]
    handler = _HANDLERS.get(kind)
    if handler is None:
        return ConfigIssue(_dotted(item.parts), item.error["msg"], f"see {REFERENCE}")
    return handler(item)


def _on_bad_tag(item: _Located) -> ConfigIssue:
    ctx = item.error.get("ctx") or {}
    tag = str(ctx.get("tag", ""))
    if _discriminator(item) == "method":
        return _wrong_method(item.parts + ("method",), item.provider_type, tag)
    return ConfigIssue(
        _dotted(item.parts + ("type",)),
        f"{tag!r} is not a type of provider",
        _suggest(tag, sorted(_TYPES), "type = ", f"the types are: {', '.join(connection_types())}"),
    )


def _on_missing_tag(item: _Located) -> ConfigIssue:
    if _discriminator(item) == "method":
        allowed = _allowed_methods(item.provider_type)
        return ConfigIssue(
            _dotted(item.parts + ("method",)),
            "this login does not say which method it uses",
            "\n".join(login_example(m, item.provider_type) for m in allowed),
        )
    return ConfigIssue(
        _dotted(item.parts + ("type",)),
        "this provider does not say which type it is",
        f"{SETTING_EXAMPLES['type']}   # one of: {', '.join(connection_types())}",
    )


def _on_literal(item: _Located) -> ConfigIssue:
    if item.parts[-2:] == ("auth", "method"):
        return _wrong_method(item.parts, item.provider_type, None)
    expected = str((item.error.get("ctx") or {}).get("expected", ""))
    name = item.parts[-1] if item.parts else ""
    example = SETTING_EXAMPLES.get(name, f"{name} = {expected}")
    return ConfigIssue(_dotted(item.parts), f"{name} must be {expected}", example)


def _wrong_method(
    parts: tuple[str, ...], provider_type: str | None, tag: str | None
) -> ConfigIssue:
    allowed = _allowed_methods(provider_type)
    who = type_label(provider_type) if provider_type else "this provider"
    given = f" — not {tag!r}" if tag else ""
    return ConfigIssue(
        _dotted(parts),
        f"{who} logs in with {_either(allowed)}{given}",
        "\n".join(login_example(m, provider_type) for m in allowed),
    )


def _on_missing(item: _Located) -> ConfigIssue:
    name = item.parts[-1]
    who = f"a {type_label(item.provider_type)} provider" if item.provider_type else "this table"
    return ConfigIssue(
        _dotted(item.parts),
        f"{who} needs {name}",
        SETTING_EXAMPLES.get(name, f'{name} = "…"   # see {REFERENCE}'),
    )


def _on_unknown(item: _Located) -> ConfigIssue:
    name = item.parts[-1]
    return ConfigIssue(
        _dotted(item.parts),
        f"{name!r} is not a setting here",
        _suggest(name, _SETTINGS, "", f"remove it, or see {REFERENCE} for the settings"),
    )


def _on_secret(item: _Located) -> ConfigIssue:
    return ConfigIssue(
        _dotted(item.parts), item.error["msg"], str((item.error.get("ctx") or {}).get("fix", ""))
    )


def _on_not_a_table(item: _Located) -> ConfigIssue:
    if item.parts and item.parts[-1] == "auth":
        allowed = _allowed_methods(item.provider_type)
        fix = "\n".join(login_example(m, item.provider_type) for m in allowed)
        return ConfigIssue(_dotted(item.parts), "auth must be a table with a method", fix)
    return ConfigIssue(_dotted(item.parts), "this must be a table", f"see {REFERENCE}")


def _on_bad_name(item: _Located) -> ConfigIssue:
    name = item.parts[-1] if item.parts else ""
    return ConfigIssue(
        _dotted(item.parts),
        f"{name!r} cannot be used as a name",
        'a name uses letters, digits, "_", "-" and ".", and starts with a letter or digit '
        '— e.g. "google-work"',
    )


def _on_empty(item: _Located) -> ConfigIssue:
    if item.parts == ("providers",):
        return ConfigIssue("providers", "no providers are set up", SETTING_EXAMPLES["providers"])
    name = item.parts[-1] if item.parts else ""
    return ConfigIssue(
        _dotted(item.parts), f"{name} must not be empty", SETTING_EXAMPLES.get(name, "")
    )


def _on_bad_value(description: str) -> Callable[[_Located], ConfigIssue]:
    def handle(item: _Located) -> ConfigIssue:
        name = item.parts[-1] if item.parts else ""
        return ConfigIssue(
            _dotted(item.parts),
            f"{name} must be {description}",
            SETTING_EXAMPLES.get(name, f"see {REFERENCE}"),
        )

    return handle


_HANDLERS: dict[str, Callable[[_Located], ConfigIssue]] = {
    "union_tag_invalid": _on_bad_tag,
    "union_tag_not_found": _on_missing_tag,
    "literal_error": _on_literal,
    "missing": _on_missing,
    "extra_forbidden": _on_unknown,
    "secret_reference": _on_secret,
    "model_attributes_type": _on_not_a_table,
    "model_type": _on_not_a_table,
    "dict_type": _on_not_a_table,
    "string_pattern_mismatch": _on_bad_name,
    "too_short": _on_empty,
    "string_too_short": _on_empty,
    "greater_than": _on_bad_value("more than zero"),
    "greater_than_equal": _on_bad_value("zero or more"),
    "int_parsing": _on_bad_value("a whole number"),
    "int_type": _on_bad_value("a whole number"),
    "int_from_float": _on_bad_value("a whole number"),
    "float_parsing": _on_bad_value("a number"),
    "float_type": _on_bad_value("a number"),
    "bool_type": _on_bad_value("true or false"),
    "bool_parsing": _on_bad_value("true or false"),
    "string_type": _on_bad_value('text, in quotes ("…")'),
}


def _allowed_methods(provider_type: str | None) -> tuple[str, ...]:
    cls = _CLASS_BY_TYPE.get(provider_type or "")
    return login_methods(cls) if cls else tuple(LOGIN_EXAMPLES)


# ─── pass 2: references ───────────────────────────────────────────────────

#: Settings of a model that only mean something on one provider type.
_ONLY_ON: tuple[tuple[str, str], ...] = (
    ("family", "vertex"),
    ("location", "vertex"),
    ("region", "bedrock"),
    ("base_model", "bedrock"),
)


def reference_issues(config: ModelsConfig) -> list[ConfigIssue]:
    """Every name in *config* that points at nothing, or at the wrong kind of thing."""
    issues = _model_ref_issues("defaults.model", config.defaults.model, config)
    for task, ref in config.tasks.items():
        path = f"tasks.{_quoted(task)}"
        if not is_task(task):
            issues.append(_unknown_task(path, task))
        issues += _model_ref_issues(path, ref, config)
    for alias, entry in config.models.items():
        issues += _model_entry_issues(alias, entry, config)
    return issues


def _unknown_task(path: str, task: str) -> ConfigIssue:
    return ConfigIssue(
        path,
        f"{task!r} is not a task",
        _suggest(
            task,
            list(task_names()),
            "",
            f"the tasks are: {', '.join(task_names())}, and {SUBAGENT_PREFIX}<name> for one subagent",
        ),
    )


def _model_ref_issues(path: str, text: str, config: ModelsConfig) -> list[ConfigIssue]:
    ref = parse_model_ref(text)
    if ref.provider is None:
        if ref.name in config.models:
            return []
        first = next(iter(config.providers))
        return [
            ConfigIssue(
                path,
                f"no model is called {ref.name!r}",
                _suggest(
                    ref.name,
                    sorted(config.models),
                    "",
                    f"add it under [models], or name a provider's model directly: {first}/<model-id>",
                ),
            )
        ]
    if ref.provider not in config.providers:
        return [
            ConfigIssue(
                path,
                f"no provider is called {ref.provider!r}",
                _suggest(
                    ref.provider,
                    sorted(config.providers),
                    "",
                    "the part before the first / must be a name from [providers]",
                ),
            )
        ]
    if not ref.name:
        return [
            ConfigIssue(path, "the model id after the / is missing", f"{ref.provider}/<model-id>")
        ]
    return []


def _model_entry_issues(alias: str, entry: ModelEntry, config: ModelsConfig) -> list[ConfigIssue]:
    connection = config.providers.get(entry.provider)
    if connection is None:
        return [
            ConfigIssue(
                f"models.{_quoted(alias)}.provider",
                f"no provider is called {entry.provider!r}",
                _suggest(
                    entry.provider,
                    sorted(config.providers),
                    "provider = ",
                    "use a name from [providers]",
                ),
            )
        ]
    kind = connection_type(type(connection))
    return [
        ConfigIssue(
            f"models.{_quoted(alias)}.{setting}",
            f"{setting} applies only to {type_label(only)} models, and {alias!r} is on "
            f"{type_label(kind)}",
            f"remove {setting} from [models.{_quoted(alias)}]",
        )
        for setting, only in _ONLY_ON
        if getattr(entry, setting) is not None and kind != only
    ]


# ─── wording and lines ────────────────────────────────────────────────────


def _suggest(given: str, choices: list[str], prefix: str, rest: str) -> str:
    """*rest*, led by "did you mean …?" when *given* is close to one of *choices*."""
    match = difflib.get_close_matches(given, choices, n=1, cutoff=0.6)
    return (
        f"did you mean {prefix}{match[0]!r}? Otherwise {rest}"
        if match
        else rest[:1].upper() + rest[1:]
    )


def _either(items: Sequence[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + f" or {items[-1]}"


def _quoted(part: str) -> str:
    return f'"{part}"' if not re.fullmatch(r"[A-Za-z0-9_-]+", part) else part


def _dotted(parts: Sequence[str]) -> str:
    return ".".join(_quoted(p) for p in parts)


def _line_label(line: int | None) -> str:
    return f"line {line}" if line else ""


def _with_line(issue: ConfigIssue, text: str) -> ConfigIssue:
    if issue.line is not None or not issue.path:
        return issue
    line = line_of(text, _split_path(issue.path))
    return ConfigIssue(issue.path, issue.problem, issue.fix, line)


_HEADER = re.compile(r"^\s*\[(?!\[)\s*(.+?)\s*\]\s*(#.*)?$")
_KEY = re.compile(r'^\s*(?:"([^"]+)"|([A-Za-z0-9_-]+))\s*=')
_PATH_PART = re.compile(r'"([^"]*)"|([^.]+)')


def _split_path(text: str) -> tuple[str, ...]:
    return tuple(a or b for a, b in _PATH_PART.findall(text))


def line_of(text: str, parts: Sequence[str]) -> int | None:
    """The 1-based line of the setting at *parts*, or of the table holding it."""
    lines = text.splitlines()
    header_at, depth = None, 0
    for number, line in enumerate(lines):
        match = _HEADER.match(line)
        if match:
            header = _split_path(match.group(1))
            if tuple(parts[: len(header)]) == header and len(header) >= depth:
                header_at, depth = number, len(header)
    if depth == len(parts) and header_at is not None:
        return header_at + 1
    key = parts[depth] if depth < len(parts) else None
    for number in range((header_at + 1) if header_at is not None else 0, len(lines)):
        if _HEADER.match(lines[number]):
            break
        match = _KEY.match(lines[number])
        if match and (match.group(1) or match.group(2)) == key:
            return number + 1
    return header_at + 1 if header_at is not None else None


__all__ = [
    "ConfigIssue",
    "ModelsConfigError",
    "line_of",
    "load_file",
    "load_text",
    "reference_issues",
]
