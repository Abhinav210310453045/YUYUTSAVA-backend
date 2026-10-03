"""AWS Bedrock — ``ChatBedrockConverse``, with the boto3 clients built here.

The clients are built here and handed in (``client=`` / ``bedrock_client=``), one
way per login method:

* ``aws_default_chain`` — boto3's own session: environment, ``~/.aws``, an
  instance role, as the AWS tools find them;
* ``aws_profile`` — that named profile from ``~/.aws/config``;
* ``aws_keys`` and ``bedrock_api_key`` — a session that reads nothing from
  ``~/.aws``, the environment or the instance metadata endpoint, so the login is
  exactly the one in the models config.

Given ``api_key=`` directly, ``ChatBedrockConverse`` still builds its session from
the machine's default AWS profile, so a Bedrock API key worked or not depending on
unrelated ``~/.aws`` contents (``test/framework_contract/test_bedrock_contract.py``).

An application inference profile ARN needs the model behind it as ``base_model``
on the alias, so nothing is looked up at construction.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, ClassVar

from langchain_core.language_models import BaseChatModel

from yuyutsava.llm.base import ANSWER_TOKENS, BuildOptions, Provider, require
from yuyutsava.llm.discovery import Availability, DiscoveredModel, Discovery
from yuyutsava.llm.target import ModelTarget, ProviderAccess

_INSTALL = "'yuyutsava[bedrock]'"

#: Availability lookups made at once while listing: one per model, so they run in parallel.
_AVAILABILITY_WORKERS = 8


def region_of(target: ProviderAccess) -> str:
    """The model's own region when its alias sets one, else the provider's."""
    entry = getattr(target, "entry", None)
    if entry is not None and entry.region:
        return entry.region
    return target.connection.region


class _StaticToken:
    """A Bedrock API key as a botocore token provider."""

    METHOD = "yuyutsava-bedrock-api-key"

    def __init__(self, token: str) -> None:
        self._token = token

    def load_token(self, **_: object) -> _StaticToken:
        return self

    def get_frozen_token(self) -> Any:
        tokens = require("botocore.tokens", provider="Bedrock", install=_INSTALL)
        return tokens.FrozenAuthToken(self._token)


def _isolated_session() -> Any:
    """A botocore session with no credentials and no config of its own."""
    botocore_session = require("botocore.session", provider="Bedrock", install=_INSTALL)
    credentials = require("botocore.credentials", provider="Bedrock", install=_INSTALL)
    session = botocore_session.Session()
    for variable in ("config_file", "credentials_file"):
        session.set_config_variable(variable, os.devnull)
    session.register_component("credential_provider", credentials.CredentialResolver(providers=[]))
    return session


def aws_clients(target: ProviderAccess) -> tuple[Any, Any]:
    """``(bedrock-runtime, bedrock)`` clients logged in as the provider's login says."""
    boto3 = require("boto3", provider="Bedrock", install=_INSTALL)
    auth = target.connection.auth
    region = region_of(target)
    config = None
    if auth.method == "aws_default_chain":
        session = boto3.Session(region_name=region)
    elif auth.method == "aws_profile":
        session = boto3.Session(profile_name=auth.profile, region_name=region)
    elif auth.method == "aws_keys":
        session = boto3.Session(
            botocore_session=_isolated_session(),
            aws_access_key_id=target.secret(auth.access_key_id),
            aws_secret_access_key=target.secret(auth.secret_access_key),
            aws_session_token=target.secret(auth.session_token) if auth.session_token else None,
            region_name=region,
        )
    else:  # bedrock_api_key
        tokens = require("botocore.tokens", provider="Bedrock", install=_INSTALL)
        botocore_config = require("botocore.config", provider="Bedrock", install=_INSTALL)
        isolated = _isolated_session()
        isolated.register_component(
            "token_provider",
            tokens.TokenProviderChain(providers=[_StaticToken(target.secret(auth.key))]),
        )
        session = boto3.Session(botocore_session=isolated, region_name=region)
        config = botocore_config.Config(auth_scheme_preference="httpBearerAuth")
    runtime = session.client("bedrock-runtime", region_name=region, config=config)
    control = session.client("bedrock", region_name=region, config=config)
    return runtime, control


class BedrockProvider(Provider):
    types: ClassVar[tuple[str, ...]] = ("bedrock",)

    def build(self, target: ModelTarget, options: BuildOptions) -> BaseChatModel:
        mod = require("langchain_aws", provider="Bedrock", install=_INSTALL)
        runtime, control = aws_clients(target)
        base_model = target.entry.base_model if target.entry is not None else None
        return mod.ChatBedrockConverse(
            model=target.model,
            region_name=region_of(target),
            client=runtime,
            bedrock_client=control,
            temperature=options.temperature,
            max_tokens=ANSWER_TOKENS,
            **({"base_model": base_model} if base_model else {}),
        )

    def discover(self, access: ProviderAccess) -> Discovery:
        """Bedrock's text models and inference profiles, each with this account's access to it."""
        exceptions = require("botocore.exceptions", provider="Bedrock", install=_INSTALL)
        region = region_of(access)
        try:
            _runtime, control = aws_clients(access)
            summaries = control.list_foundation_models(byOutputModality="TEXT")["modelSummaries"]
            profiles = _inference_profiles(control)
            access_of = _availability_of(control, [m["modelId"] for m in summaries])
        except exceptions.NoCredentialsError:
            return Discovery(access.provider, problem="no AWS login was found", fix=_LOGIN_FIX)
        except exceptions.ClientError as exc:
            error = exc.response.get("Error", {})
            return Discovery(
                access.provider,
                problem=f"AWS refused the request ({error.get('Code')}): {error.get('Message', '')}",
                fix=_LOGIN_FIX,
            )
        except exceptions.BotoCoreError as exc:
            return Discovery(
                access.provider,
                problem=f"could not reach Bedrock in {region}: {exc}",
                fix=_REACH_FIX,
            )
        return Discovery(
            access.provider, models=bedrock_models(summaries, profiles, access_of, region)
        )


_LOGIN_FIX = "check the provider's AWS login, and that it may call bedrock:List* and bedrock:Get*"
_REACH_FIX = "check the region and your network connection"


def _inference_profiles(control: Any) -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    token = None
    while True:
        page = control.list_inference_profiles(
            typeEquals="SYSTEM_DEFINED", **({"nextToken": token} if token else {})
        )
        profiles += page.get("inferenceProfileSummaries", [])
        token = page.get("nextToken")
        if not token:
            return profiles


def _availability_of(control: Any, model_ids: list[str]) -> dict[str, dict[str, Any]]:
    """``GetFoundationModelAvailability`` for each model, made in parallel."""

    def one(model_id: str) -> tuple[str, dict[str, Any]]:
        return model_id, control.get_foundation_model_availability(modelId=model_id)

    with ThreadPoolExecutor(max_workers=_AVAILABILITY_WORKERS) as pool:
        return dict(pool.map(one, model_ids))


def availability(answer: dict[str, Any] | None, region: str) -> tuple[Availability, str]:
    """What ``GetFoundationModelAvailability`` says about one model, and what to do."""
    if not answer:
        return Availability.UNKNOWN, ""
    if answer.get("regionAvailability") == "NOT_AVAILABLE":
        return Availability.NOT_IN_REGION, f"not served in {region}"
    if answer.get("authorizationStatus") == "NOT_AUTHORIZED":
        return Availability.NEEDS_ACCESS, "this AWS identity is not allowed to use it (IAM)"
    agreement = (answer.get("agreementAvailability") or {}).get("status")
    if agreement == "PENDING":
        return Availability.NEEDS_ACCESS, "access was requested and is not granted yet"
    if agreement == "NOT_AVAILABLE" or answer.get("entitlementAvailability") == "NOT_AVAILABLE":
        return (
            Availability.NEEDS_ACCESS,
            "request access in the Bedrock console, under Model access",
        )
    if agreement == "ERROR":
        return Availability.UNKNOWN, "Bedrock could not say whether access is granted"
    return Availability.AVAILABLE, ""


def bedrock_models(
    summaries: list[dict[str, Any]],
    profiles: list[dict[str, Any]],
    access_of: dict[str, dict[str, Any]],
    region: str,
) -> tuple[DiscoveredModel, ...]:
    """What can be called: on-demand models directly, the rest through inference profiles.

    An inference profile's availability is that of the model behind it.
    """
    models = []
    for summary in summaries:
        on_demand = "ON_DEMAND" in (summary.get("inferenceTypesSupported") or [])
        active = (summary.get("modelLifecycle") or {}).get("status", "ACTIVE") == "ACTIVE"
        if not (on_demand and active):
            continue
        state, note = availability(access_of.get(summary["modelId"]), region)
        models.append(
            DiscoveredModel(
                id=summary["modelId"],
                label=summary.get("modelName") or "",
                availability=state,
                note=note,
            )
        )
    for profile in profiles:
        arns = [entry.get("modelArn", "") for entry in profile.get("models") or []]
        behind = arns[0].rsplit("/", 1)[-1] if arns else ""
        state, note = availability(access_of.get(behind), region)
        models.append(
            DiscoveredModel(
                id=profile["inferenceProfileId"],
                label=profile.get("inferenceProfileName") or "",
                availability=state,
                note=note,
            )
        )
    return tuple(models)


__all__ = ["BedrockProvider", "availability", "aws_clients", "bedrock_models", "region_of"]
