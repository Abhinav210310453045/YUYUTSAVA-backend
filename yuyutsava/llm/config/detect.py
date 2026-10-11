"""What this machine is already set up for, so a setup can start from it.

The setup wizards (CLI and app) open with "here is what I found — use it?", so
someone who already has a working login does not retype it:

* **your current setup** — ``LLM_PROVIDER`` and its variables, from the
  environment or a ``.env`` file: the model this machine runs on today;
* **a Google login** — gcloud's application-default credentials, or a
  service-account key named by ``GOOGLE_APPLICATION_CREDENTIALS``;
* **AWS profiles** — ``~/.aws/config`` and ``~/.aws/credentials``;
* **a running Ollama** with a chat model downloaded;
* **API keys** — the ``*_API_KEY`` variables each provider reads.

Each :class:`Finding` is a ``[providers.<name>]`` table ready to write. Keys stay
references (``env:NAME``); copying a key into ``secrets.toml`` is the writer's
choice (:mod:`yuyutsava.llm.config.edits`), never this module's. Nothing here
writes, and only the Ollama check goes over the network — to this machine, with
a short timeout.
"""

from __future__ import annotations

import configparser
import json
import logging
import os
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from yuyutsava.llm.config.leftovers import env_files
from yuyutsava.llm.settings import env_setup
from yuyutsava.storage.paths import models_config_path

logger = logging.getLogger("yuyutsava.llm.config.detect")

#: The variable each API-key provider reads, and the provider type it belongs to.
API_KEY_VARIABLES: tuple[tuple[str, str], ...] = (
    ("ANTHROPIC_API_KEY", "anthropic"),
    ("OPENAI_API_KEY", "openai"),
    ("OPENROUTER_API_KEY", "openrouter"),
    ("GROQ_API_KEY", "groq"),
    ("GOOGLE_API_KEY", "google"),
    ("GEMINI_API_KEY", "google"),
    ("MISTRAL_API_KEY", "mistral"),
    ("COHERE_API_KEY", "cohere"),
)

_OLLAMA_DEFAULT = "http://localhost:11434"
_OLLAMA_TIMEOUT_SECONDS = 0.5


@dataclass(frozen=True)
class Finding:
    """A provider this machine is already set up for."""

    #: The name to give it under ``[providers]``.
    name: str
    #: Its ``type``.
    type: str
    #: Where it was found, said for someone new to all of it, with the file it is
    #: in: "what YUYUTSAVA uses today: gemini-3.5-flash, set in ~/project/.env".
    origin: str
    #: The ``[providers.<name>]`` table. Keys are ``env:`` references.
    connection: Mapping[str, Any]
    #: A model to suggest, when the finding names one.
    model: str | None = None
    #: Settings for that model's alias (a thinking budget), when there are any.
    model_settings: Mapping[str, Any] = field(default_factory=dict)


Probe = Callable[[str], Any]
"""GET a URL and return its parsed JSON; raise when nothing answers."""


def detect(
    *,
    home: Path | None = None,
    probe: Probe | None = None,
    dotenv_files: Sequence[Path] | None = None,
) -> list[Finding]:
    """Everything found, the current setup first; one finding per provider and login.

    Reads this process's environment — after ``.env`` is loaded, which is how a
    ``.env`` setup is found. *dotenv_files* are the ``.env`` files YUYUTSAVA
    loads (default: :func:`~yuyutsava.llm.config.leftovers.env_files`), read
    only to say which one a setting is in.
    """
    environ = os.environ
    home = Path.home() if home is None else home
    if dotenv_files is None:
        dotenv_files = env_files(models_config_path().parent)
    found: list[Finding] = []
    for finding in (
        *current_setup(environ, dotenv_files),
        *google_login(environ),
        *aws_profiles(environ, home),
        *ollama(environ, probe or _get_json),
        *api_keys(environ),
    ):
        if all(_same_login(finding) != _same_login(f) for f in found):
            found.append(finding)
    return found


def _same_login(finding: Finding) -> tuple[str, str]:
    """What makes two findings one provider: its type and how it logs in."""
    return finding.type, json.dumps(finding.connection.get("auth"), sort_keys=True)


def current_setup(environ: Mapping[str, str], dotenv_files: Sequence[Path] = ()) -> list[Finding]:
    """What ``LLM_PROVIDER`` describes, when it is set and complete."""
    chosen = environ.get("LLM_PROVIDER", "").strip()
    if not chosen:
        return []
    try:
        setup = env_setup()  # reads os.environ, which *environ* is outside tests
    except RuntimeError:
        return []
    return [
        Finding(
            name=setup.type,
            type=setup.type,
            origin=f"what YUYUTSAVA uses today: {setup.model}, "
            f"{where_set('LLM_PROVIDER', dotenv_files)} (LLM_PROVIDER={chosen})",
            connection=setup.connection,
            model=setup.model,
            model_settings=setup.model_settings or {},
        )
    ]


def google_login(environ: Mapping[str, str]) -> list[Finding]:
    """Vertex through gcloud's login or a service-account key, when either exists.

    The project comes from ``VERTEX_PROJECT`` / ``GOOGLE_CLOUD_PROJECT``, else
    gcloud's own setting; without one there is nothing to write yet.
    """
    key_file = environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if key_file and _credential_type(Path(key_file).expanduser()) == "service_account":
        auth: dict[str, Any] = {"method": "service_account", "key_file": key_file}
        origin = f"a service-account key file, {key_file}"
    elif key_file and _credential_type(Path(key_file).expanduser()) is not None:
        # A user login saved to a file (``authorized_user``): Google's own lookup
        # reads it through this variable, which is what the adc method uses.
        auth = {"method": "adc"}
        origin = f"your Google account, signed in through {key_file}"
    elif (adc := _adc_path()) is not None and adc.is_file():
        auth = {"method": "adc"}
        origin = "your Google account, signed in with gcloud on this computer"
    else:
        return []
    project = (
        environ.get("VERTEX_PROJECT") or environ.get("GOOGLE_CLOUD_PROJECT") or _gcloud_project()
    )
    if not project:
        return []
    location = environ.get("VERTEX_LOCATION") or "global"
    connection = {"type": "vertex", "project": project, "location": location, "auth": auth}
    origin = f"{origin} — project {project}"
    return [Finding("vertex", "vertex", origin, connection, model="gemini-3.5-flash")]


def aws_profiles(environ: Mapping[str, str], home: Path) -> list[Finding]:
    """One Bedrock finding per AWS profile in ``~/.aws`` (or where ``AWS_*_FILE`` point)."""
    profiles: dict[str, str | None] = {}
    config = Path(environ.get("AWS_CONFIG_FILE") or home / ".aws" / "config")
    credentials = Path(environ.get("AWS_SHARED_CREDENTIALS_FILE") or home / ".aws" / "credentials")
    for path, prefix in ((config, "profile "), (credentials, "")):
        parser = configparser.ConfigParser()
        try:
            parser.read(path, encoding="utf-8")
        except (configparser.Error, UnicodeDecodeError):
            continue
        for section in parser.sections():
            name = section.removeprefix(prefix) if section != "default" else "default"
            region = parser[section].get("region")
            profiles[name] = profiles.get(name) or region
    return [
        Finding(
            name="bedrock" if name == "default" else f"bedrock-{name}",
            type="bedrock",
            origin=f"the AWS profile '{name}' in ~/.aws, region "
            f"{region or environ.get('AWS_REGION') or 'us-east-1'}",
            connection={
                "type": "bedrock",
                "region": region or environ.get("AWS_REGION") or "us-east-1",
                "auth": {"method": "aws_profile", "profile": name},
            },
        )
        for name, region in sorted(profiles.items())
    ]


def ollama(environ: Mapping[str, str], probe: Probe) -> list[Finding]:
    """A running Ollama, and the first model it has pulled."""
    host = environ.get("OLLAMA_HOST", "").strip() or _OLLAMA_DEFAULT
    if "://" not in host:
        host = f"http://{host}"
    try:
        tags = probe(f"{host.rstrip('/')}/api/tags")
    except Exception:  # noqa: BLE001 — nothing answering is the usual, unremarkable case
        logger.debug("no Ollama at %s", host, exc_info=True)
        return []
    # Embedding models are listed beside chat models and cannot hold a conversation.
    models = [
        name
        for m in (tags or {}).get("models", [])
        if (name := m.get("name")) and "embed" not in name
    ]
    if not models:  # running, but with nothing it could chat with
        logger.debug("Ollama at %s has no chat model", host)
        return []
    connection: dict[str, Any] = {"type": "ollama"}
    if host != _OLLAMA_DEFAULT:
        connection["base_url"] = host
    count = f"{len(models)} models" if len(models) > 1 else "1 model"
    return [
        Finding(
            "ollama",
            "ollama",
            f"running on this computer, with {count} downloaded ({', '.join(models[:3])})",
            connection,
            model=models[0],
        )
    ]


def api_keys(environ: Mapping[str, str]) -> list[Finding]:
    """A provider for each ``*_API_KEY`` variable that is set."""
    return [
        Finding(
            name=provider_type,
            type=provider_type,
            origin=f"the key in your {variable} setting",
            connection={
                "type": provider_type,
                "auth": {"method": "api_key", "key": f"env:{variable}"},
            },
        )
        for variable, provider_type in API_KEY_VARIABLES
        if environ.get(variable, "").strip()
    ]


def where_set(name: str, dotenv_files: Sequence[Path]) -> str:
    """Where the setting *name* comes from, for a person: "set in ~/project/.env"."""
    for path in reversed(dotenv_files):  # the one loaded last wins
        try:
            if name in dotenv_values(path):
                return f"set in {_short(path)}"
        except OSError:
            continue
    return "set in your terminal's environment"


def _short(path: Path) -> str:
    """*path* with the home folder as ``~``."""
    home = str(Path.home())
    text = str(path)
    return "~" + text[len(home) :] if text.startswith(home + os.sep) else text


def _credential_type(path: Path) -> str | None:
    """The ``type`` a Google credentials file declares; ``None`` when it is not one."""
    try:
        declared = json.loads(path.read_text(encoding="utf-8")).get("type")
    except (OSError, ValueError, AttributeError):
        return None
    return declared if isinstance(declared, str) else None


def _adc_path() -> Path | None:
    """Where gcloud keeps application-default credentials on this OS, per google-auth."""
    try:
        from google.auth import _cloud_sdk  # noqa: PLC0415 — optional; knows each OS's path
    except ImportError:
        return None
    return Path(_cloud_sdk.get_application_default_credentials_path())


def _gcloud_project() -> str | None:
    """gcloud's configured project; ``None`` when gcloud is absent or has none."""
    try:
        from google.auth import _cloud_sdk  # noqa: PLC0415 — optional; runs gcloud itself
    except ImportError:
        return None
    try:
        return _cloud_sdk.get_project_id()
    except Exception:  # noqa: BLE001 — no gcloud, or no project set: nothing to suggest
        logger.debug("no gcloud project to suggest", exc_info=True)
        return None


def _get_json(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=_OLLAMA_TIMEOUT_SECONDS) as response:  # noqa: S310 — a URL this module builds
        return json.loads(response.read())


__all__ = ["API_KEY_VARIABLES", "Finding", "detect", "where_set"]
