"""Google Vertex AI — Gemini, Claude and open models in one Google Cloud project.

Which family serves a model is :attr:`ModelTarget.family` — the alias's
``family``, or read from the id:

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
import warnings
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, ClassVar

from langchain_core.language_models import BaseChatModel

from yuyutsava.llm.base import ANSWER_TOKENS, BuildOptions, Provider, require
from yuyutsava.llm.discovery import Availability, DiscoveredModel, Discovery
from yuyutsava.llm.handle import Capability
from yuyutsava.llm.providers.gemini import build_gemini
from yuyutsava.llm.providers.listing import Listing, get_json
from yuyutsava.llm.target import ModelTarget, ProviderAccess

_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
#: The start of google-auth's warning for a user login with no quota project.
_NO_QUOTA_PROJECT = "Your application has authenticated using end user credentials"

#: Publishers whose open models Vertex serves as a service (ids ending ``-maas``).
#: Listing checks each; a model from a publisher not here can still be added by id.
MAAS_PUBLISHERS = (
    "openai",
    "meta",
    "deepseek-ai",
    "qwen",
    "mistralai",
    "moonshotai",
    "zai-org",
    "minimaxai",
)
#: Words in a Gemini model's name that mark it as something other than a chat model.
_NOT_CHAT = (
    "embedding",
    "tts",
    "image",
    "live",
    "transcribe",
    "translate",
    "computer-use",
    "native-audio",
)
_GARDEN_URL = "https://us-central1-aiplatform.googleapis.com/v1beta1/publishers/{publisher}/models"
_QUOTA_URL = (
    "https://cloudquotas.googleapis.com/v1/projects/{project}/locations/global/services/"
    "aiplatform.googleapis.com/quotaInfos/GlobalOnlinePredictionRequestsPerMinutePerProjectPerBaseModel"
)
_LOGIN_FIX = "run `gcloud auth application-default login`, or check the service-account key file"
#: Model Garden is listed one publisher at a time; these run at once.
_LISTING_WORKERS = 6
_INSTALL = "'yuyutsava[vertex]'"


def location_of(target: ModelTarget) -> str:
    """The model's own location when its alias sets one, else the provider's."""
    if target.entry is not None and target.entry.location:
        return target.entry.location
    return target.connection.location


def google_credentials(target: ProviderAccess) -> Any:
    """A ``google.auth`` credentials object for the provider's login."""
    auth = target.connection.auth
    if auth.method == "adc":
        google_auth = require("google.auth", provider="Vertex AI", install=_INSTALL)
        with warnings.catch_warnings():
            # A gcloud user login carries no quota project, and google-auth warns
            # about it on every lookup — once per model built. Vertex bills the
            # project in the request URL either way; setting a quota project
            # would add a serviceusage permission not every Vertex user has.
            warnings.filterwarnings("ignore", message=_NO_QUOTA_PROJECT, category=UserWarning)
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
        family = target.family
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

    def discover(self, access: ProviderAccess) -> Discovery:
        """Gemini, Claude and open models in Model Garden, with Claude's quota checked.

        Every request names the provider's project as the quota project: a gcloud
        user login has none, and listing endpoints carry no project of their own.
        """
        google_exceptions = require(
            "google.auth.exceptions", provider="Vertex AI", install=_INSTALL
        )
        try:
            token = token_source(google_credentials(access))()
        except google_exceptions.DefaultCredentialsError:
            return Discovery(access.provider, problem="no Google login was found", fix=_LOGIN_FIX)
        except (google_exceptions.RefreshError, OSError) as exc:
            return Discovery(
                access.provider, problem=f"Google refused the login: {exc}", fix=_LOGIN_FIX
            )
        project = access.connection.project
        headers = {"Authorization": f"Bearer {token}", "x-goog-user-project": project}
        publishers = ("google", "anthropic", *MAAS_PUBLISHERS)
        with ThreadPoolExecutor(max_workers=_LISTING_WORKERS) as pool:
            quotas = pool.submit(_base_model_quotas, headers, project)
            listings = dict(
                zip(publishers, pool.map(lambda p: _garden(headers, p), publishers), strict=True)
            )
        if listings["google"].problem:
            return Discovery(
                access.provider, problem=listings["google"].problem, fix=listings["google"].fix
            )
        models = [*vertex_gemini_models(listings["google"].body)]
        if not listings["anthropic"].problem:
            models += vertex_claude_models(listings["anthropic"].body, quotas.result())
        for publisher in MAAS_PUBLISHERS:
            if not listings[publisher].problem:
                models += vertex_open_models(publisher, listings[publisher].body)
        return Discovery(access.provider, models=tuple(models))


def _garden(headers: dict[str, str], publisher: str) -> Listing:
    """Every model *publisher* has in Model Garden, all pages."""
    models: list[dict[str, Any]] = []
    params: dict[str, Any] = {"pageSize": 200, "view": "PUBLISHER_MODEL_VIEW_BASIC"}
    while True:
        page = get_json(
            _GARDEN_URL.format(publisher=publisher),
            label="Vertex AI",
            headers=headers,
            params=params,
        )
        if page.problem:
            return page
        models += page.body.get("publisherModels", [])
        token = page.body.get("nextPageToken")
        if not token:
            return Listing(body={"publisherModels": models})
        params["pageToken"] = token


def _base_model_quotas(headers: dict[str, str], project: str) -> dict[str, int] | None:
    """Requests-per-minute quota per base model at ``global``; ``None`` if it cannot be read."""
    listing = get_json(_QUOTA_URL.format(project=project), label="Cloud Quotas", headers=headers)
    if listing.problem:
        return None
    return quota_values(listing.body)


def quota_values(body: dict[str, Any]) -> dict[str, int]:
    """``base_model → quota`` from a Cloud Quotas ``quotaInfo``.

    A value left out is zero — the API drops a zero, as protobuf JSON does — and
    ``-1`` is unlimited (measured on the owner's project, design §2 rule 15).
    """
    values = {}
    for info in body.get("dimensionsInfos", []):
        base_model = (info.get("dimensions") or {}).get("base_model")
        if base_model:
            values[base_model] = int((info.get("details") or {}).get("value", 0))
    return values


def _model_names(body: dict[str, Any]) -> list[tuple[str, str]]:
    return [
        (entry["name"].rsplit("/", 1)[-1], entry.get("versionId") or "")
        for entry in body.get("publisherModels", [])
    ]


def vertex_gemini_models(body: dict[str, Any]) -> list[DiscoveredModel]:
    """The Gemini chat models in Model Garden's ``google`` listing."""
    return [
        DiscoveredModel(id=name, availability=Availability.AVAILABLE)
        for name, _version in _model_names(body)
        if name.startswith("gemini-") and not any(word in name for word in _NOT_CHAT)
    ]


def vertex_claude_models(
    body: dict[str, Any], quotas: dict[str, int] | None
) -> list[DiscoveredModel]:
    """Claude models on Vertex, each judged by this project's quota for it."""
    models = []
    for name, version in _model_names(body):
        model_id = f"{name}@{version}" if version and version != "default" else name
        state, note = _claude_availability(name, quotas)
        models.append(DiscoveredModel(id=model_id, tools=True, availability=state, note=note))
    return models


def _claude_availability(name: str, quotas: dict[str, int] | None) -> tuple[Availability, str]:
    if quotas is None:
        return (
            Availability.UNKNOWN,
            "enable the Cloud Quotas API to see whether your quota allows it",
        )
    quota = quotas.get(f"anthropic-{name}")
    if quota is None:
        return Availability.UNKNOWN, ""
    if quota == 0:
        return Availability.NEEDS_QUOTA, (
            f"your quota for it is 0 — request an increase under IAM & Admin → Quotas, "
            f"filter base_model:anthropic-{name}, location global"
        )
    return Availability.AVAILABLE, "if a call is refused, enable it in Model Garden"


def vertex_open_models(publisher: str, body: dict[str, Any]) -> list[DiscoveredModel]:
    """*publisher*'s models served as a service on Vertex, as ``publisher/name-maas`` ids."""
    return [
        DiscoveredModel(
            id=f"{publisher}/{name}",
            availability=Availability.UNKNOWN,
            note="available once enabled in Model Garden",
        )
        for name, _version in _model_names(body)
        if name.endswith("-maas")
    ]


__all__ = [
    "MAAS_PUBLISHERS",
    "VertexProvider",
    "google_credentials",
    "location_of",
    "maas_endpoint",
    "quota_values",
    "vertex_claude_models",
    "vertex_gemini_models",
    "vertex_open_models",
]
