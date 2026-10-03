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
from typing import Any, ClassVar

from langchain_core.language_models import BaseChatModel

from yuyutsava.llm.base import ANSWER_TOKENS, BuildOptions, Provider, require
from yuyutsava.llm.target import ModelTarget

_INSTALL = "'yuyutsava[bedrock]'"


def region_of(target: ModelTarget) -> str:
    """The model's own region when its alias sets one, else the provider's."""
    if target.entry is not None and target.entry.region:
        return target.entry.region
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


def aws_clients(target: ModelTarget) -> tuple[Any, Any]:
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


__all__ = ["BedrockProvider", "aws_clients", "region_of"]
