"""Bedrock logins work from clients we build, isolated from ``~/.aws`` and the environment.

The ``bedrock`` provider type builds its own boto3 clients and hands them to
``ChatBedrockConverse`` (``client=`` / ``bedrock_client=``)
(docs/architecture/decisions/ADR-005-models-config.md §3; design §2 rule 6).

**Why not pass the login straight to ``ChatBedrockConverse``?**

- Given ``api_key=``, it still builds its session from the machine's default AWS
  profile.
- On a machine whose default profile uses the ``login_session`` provider, that
  fails unless ``botocore[crt]`` is installed. So a Bedrock API key worked or not
  depending on unrelated ``~/.aws`` contents. This was measured on the owner's
  machine on 2026-10-02.

**What is pinned here, with every request answered locally at botocore's
``before-send`` hook (nothing is sent):**

- **API key:** a session that never reads ``~/.aws`` signs with
  ``Authorization: Bearer <key>``. The token comes from a provider registered
  through botocore's public ``register_component``.
- **Access keys:** the same isolated session signs with SigV4.
- **Our client is used:** ``ChatBedrockConverse`` sends through the client it is
  given.
- **No control-plane call:** an application inference profile ARN with
  ``base_model`` needs no control-plane lookup at construction.
- **No environment change:** none of this touches ``os.environ``.
"""

from __future__ import annotations

import json
import os
import unittest

import pytest

boto3 = pytest.importorskip("boto3")
aws = pytest.importorskip("langchain_aws")

from botocore.awsrequest import AWSResponse  # noqa: E402 — after the optional-SDK skip
from botocore.config import Config  # noqa: E402
from botocore.credentials import CredentialResolver  # noqa: E402
from botocore.session import Session as BotocoreSession  # noqa: E402
from botocore.tokens import FrozenAuthToken, TokenProviderChain  # noqa: E402

_MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
_APP_PROFILE = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc123"
_REPLY = {
    "output": {"message": {"role": "assistant", "content": [{"text": "hi back"}]}},
    "stopReason": "end_turn",
    "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
    "metrics": {"latencyMs": 1},
}


class _StaticToken:
    """A Bedrock API key as a botocore token provider — no env var, no file."""

    METHOD = "yuyutsava-static"

    def __init__(self, token: str) -> None:
        self._token = token

    def load_token(self, **kwargs: object) -> _StaticToken:
        return self

    def get_frozen_token(self) -> FrozenAuthToken:
        return FrozenAuthToken(self._token)


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def stream(self, **kwargs: object):
        yield self._data

    def read(self) -> bytes:
        return self._data


class _NoControlPlane:
    def __getattr__(self, name: str):
        raise AssertionError(f"the Bedrock control plane was called: {name}")


def isolated_runtime_client(*, api_key: str | None = None, keys: tuple[str, str] | None = None):
    """A bedrock-runtime client whose only login is the one passed in."""
    session = BotocoreSession()
    for variable in ("config_file", "credentials_file"):
        session.set_config_variable(variable, os.devnull)
    # No credential chain at all: not the environment, not a container or the
    # instance metadata endpoint. A login is only what we hand the session.
    session.register_component("credential_provider", CredentialResolver(providers=[]))
    config = None
    if api_key is not None:
        session.register_component(
            "token_provider", TokenProviderChain(providers=[_StaticToken(api_key)])
        )
        config = Config(auth_scheme_preference="httpBearerAuth")
    key_id, secret = keys if keys else (None, None)
    boto = boto3.Session(
        botocore_session=session, aws_access_key_id=key_id, aws_secret_access_key=secret
    )
    return boto.client("bedrock-runtime", region_name="us-east-1", config=config)


def answer_and_auth(client, model: str = _MODEL, **kwargs: object) -> tuple[str, str]:
    """``(reply text, Authorization header)`` for one Converse call through *client*."""
    seen: dict[str, str] = {}

    def answer_locally(request, **_: object) -> AWSResponse:
        header = request.headers.get("Authorization", b"")
        seen["auth"] = header.decode() if isinstance(header, bytes) else header
        body = _Body(json.dumps(_REPLY).encode())
        return AWSResponse(request.url, 200, {"content-type": "application/json"}, body)

    client.meta.events.register("before-send.bedrock-runtime.Converse", answer_locally)
    chat = aws.ChatBedrockConverse(
        model=model,
        region_name="us-east-1",
        client=client,
        bedrock_client=_NoControlPlane(),
        **kwargs,
    )
    return chat.invoke("hi").content, seen.get("auth", "")


def test_an_api_key_signs_as_bearer_without_reading_aws_config() -> None:
    before = dict(os.environ)
    reply, auth = answer_and_auth(isolated_runtime_client(api_key="KEY123"))
    assert (reply, auth) == ("hi back", "Bearer KEY123"), f"reply {reply!r}, Authorization {auth!r}"
    assert dict(os.environ) == before, "building or calling the client changed os.environ"


def test_access_keys_sign_with_sigv4() -> None:
    reply, auth = answer_and_auth(isolated_runtime_client(keys=("AKIDEXAMPLE", "SECRET")))
    assert reply == "hi back"
    assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/"), f"Authorization {auth!r}"


def test_an_application_inference_profile_needs_no_control_plane_call() -> None:
    reply, _ = answer_and_auth(
        isolated_runtime_client(api_key="KEY123"),
        model=_APP_PROFILE,
        base_model="anthropic.claude-sonnet-4-5-20250929-v1:0",
        provider="anthropic",
    )
    assert reply == "hi back"


class TheChecksFail(unittest.TestCase):
    """Negative control: the isolated session really has no login of its own."""

    def test_without_a_login_the_call_is_refused_not_borrowed_from_aws_config(self) -> None:
        from botocore.exceptions import NoCredentialsError

        with self.assertRaises(NoCredentialsError):
            answer_and_auth(isolated_runtime_client())


if __name__ == "__main__":
    unittest.main()
