"""Google Vertex AI — Gemini, Claude and open models in one Google Cloud project.

Which family serves a model is the alias's ``family``, or inferred from its id:

* ``claude-…`` — **Claude**, through ``ChatAnthropicVertex``;
* ``<publisher>/<name>-maas`` — an **open model** (Model as a Service), through
  ``ChatOpenAI`` at Vertex's OpenAI-compatible endpoint;
* anything else — **Gemini**, through the shared Gemini builder.

Open models do not go through LangChain's own MaaS classes: those accept a fixed
list of models, build an invalid host for ``global`` and keep a token fetched at
construction until it expires. Here ``api_key`` is a token function
``ChatOpenAI`` calls on every request — in an executor thread for async calls, so
a token refresh never blocks the event loop
(``test/framework_contract/test_vertex_model_garden_contract.py``).

Each family logs in with a ``google.auth`` credentials object built from the
provider's login (ADC, a service-account key file, or an access token), never
through the environment.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

from langchain_core.language_models import BaseChatModel

from yuyutsava.llm.base import ANSWER_TOKENS, BuildOptions, Provider, require
from yuyutsava.llm.handle import Capability
from yuyutsava.llm.providers.gemini import build_gemini
from yuyutsava.llm.target import ModelTarget

_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
_INSTALL = "'yuyutsava[vertex]'"


def family_of(target: ModelTarget) -> str:
    """``gemini``, ``anthropic`` or ``maas``: which family serves *target* on Vertex."""
    if target.entry is not None and target.entry.family:
        return target.entry.family
    if target.model.startswith("claude"):
        return "anthropic"
    if "/" in target.model and target.model.endswith("-maas"):
        return "maas"
    return "gemini"


def location_of(target: ModelTarget) -> str:
    """The model's own location when its alias sets one, else the provider's."""
    if target.entry is not None and target.entry.location:
        return target.entry.location
    return target.connection.location


def google_credentials(target: ModelTarget) -> Any:
    """A ``google.auth`` credentials object for the provider's login."""
    auth = target.connection.auth
    if auth.method == "adc":
        google_auth = require("google.auth", provider="Vertex AI", install=_INSTALL)
        credentials, _project = google_auth.default(scopes=_SCOPES)
        return credentials
    if auth.method == "service_account":
        accounts = require("google.oauth2.service_account", provider="Vertex AI", install=_INSTALL)
        path = Path(auth.key_file).expanduser()
        return accounts.Credentials.from_service_account_file(str(path), scopes=_SCOPES)
    oauth = require("google.oauth2.credentials", provider="Vertex AI", install=_INSTALL)
    return oauth.Credentials(token=target.secret(auth.token))


def token_source(credentials: Any) -> Callable[[], str]:
    """A function returning a valid access token, refreshing it only when needed."""
    transport = require("google.auth.transport.requests", provider="Vertex AI", install=_INSTALL)
    lock = threading.Lock()

    def token() -> str:
        with lock:
            if not credentials.valid:
                credentials.refresh(transport.Request())
            return credentials.token

    return token


def maas_endpoint(project: str, location: str) -> str:
    """Vertex's OpenAI-compatible endpoint for *project* in *location*."""
    host = (
        "aiplatform.googleapis.com"
        if location == "global"
        else f"{location}-aiplatform.googleapis.com"
    )
    return f"https://{host}/v1/projects/{project}/locations/{location}/endpoints/openapi"


class VertexProvider(Provider):
    types: ClassVar[tuple[str, ...]] = ("vertex",)
    # Gemini's client binds to the loop that first drives it; every family is kept
    # one-instance-per-loop for one rule — see quirks/loop_affinity.
    capabilities = frozenset({Capability.LOOP_AFFINE})

    def build(self, target: ModelTarget, options: BuildOptions) -> BaseChatModel:
        family = family_of(target)
        if family == "anthropic":
            return self._claude(target, options)
        if family == "maas":
            return self._open_model(target, options)
        return build_gemini(
            target,
            options,
            vertexai=True,
            project=target.connection.project,
            location=location_of(target),
            credentials=google_credentials(target),
        )

    def _claude(self, target: ModelTarget, options: BuildOptions) -> BaseChatModel:
        garden = require(
            "langchain_google_vertexai.model_garden", provider="Claude on Vertex", install=_INSTALL
        )
        return garden.ChatAnthropicVertex(
            model_name=target.model,
            project=target.connection.project,
            location=location_of(target),
            credentials=google_credentials(target),
            temperature=options.temperature,
            # Explicit: the SDK's default comes from a profile lookup that misses
            # dated ids (claude-…@2025…) and falls back to 4096.
            max_output_tokens=ANSWER_TOKENS,
        )

    def _open_model(self, target: ModelTarget, options: BuildOptions) -> BaseChatModel:
        openai = require(
            "langchain_openai", provider="open models on Vertex", install="langchain-openai"
        )
        return openai.ChatOpenAI(
            model=target.model,
            base_url=maas_endpoint(target.connection.project, location_of(target)),
            api_key=token_source(google_credentials(target)),
            temperature=options.temperature,
            max_completion_tokens=ANSWER_TOKENS,
        )


__all__ = ["VertexProvider", "family_of", "google_credentials", "location_of", "maas_endpoint"]
