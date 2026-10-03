"""Each provider's model list is read into models, and each account's access into availability.

Two kinds of fixture, named by where they came from:

* **recorded** (``test/llm/fixtures/discovery/``) — real responses, trimmed to a
  few entries: OpenRouter's public ``/models``, a local Ollama's ``/api/tags``,
  Vertex Model Garden listings and a Cloud Quotas ``quotaInfo`` from the owner's
  project (2026-10-03, project id scrubbed);
* **documented** — written here from each provider's published response shape,
  for providers no key was available for (Groq, OpenAI, Anthropic, AI Studio,
  Mistral, Cohere).

Bedrock is driven through botocore's ``Stubber``, which checks every request and
response against AWS's own service model. Nothing is sent.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

import httpx

from yuyutsava.llm.config.loader import load_text
from yuyutsava.llm.discovery import Availability
from yuyutsava.llm.providers import listing, vertex
from yuyutsava.llm.providers.anthropic import parse_anthropic_models
from yuyutsava.llm.providers.bedrock import BedrockProvider, availability
from yuyutsava.llm.providers.cohere import parse_cohere_models
from yuyutsava.llm.providers.google import parse_ai_studio_models
from yuyutsava.llm.providers.mistral import parse_mistral_models
from yuyutsava.llm.providers.openai_compat import (
    parse_groq_models,
    parse_ollama_tags,
    parse_openai_models,
    parse_openrouter_models,
)
from yuyutsava.llm.providers.registry import discover
from yuyutsava.llm.target import access_for

FIXTURES = Path(__file__).parent / "fixtures" / "discovery"


def recorded(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class OpenAICompatibleHosts(unittest.TestCase):
    def test_openrouter_gives_window_output_price_and_tools(self) -> None:
        models = {m.id: m for m in parse_openrouter_models(recorded("openrouter_models.json"))}
        claude = next(m for m in models.values() if m.id.startswith("anthropic/claude"))
        self.assertGreater(claude.context_window, 100_000)
        self.assertTrue(claude.max_output)
        self.assertTrue(claude.tools)
        self.assertGreater(claude.price[0], 0)  # per million, from OpenRouter's per-token string
        self.assertEqual(claude.availability, Availability.AVAILABLE)

    def test_ollama_lists_only_models_that_can_chat(self) -> None:
        self.assertEqual(
            parse_ollama_tags(recorded("ollama_tags.json")), ()
        )  # an embedding model only
        chat = {"models": [{"name": "llama3.2:3b", "capabilities": ["completion", "tools"]}]}
        [model] = parse_ollama_tags(chat)
        self.assertEqual((model.id, model.tools, model.price), ("llama3.2:3b", True, (0.0, 0.0)))

    def test_groq_marks_an_inactive_model(self) -> None:
        body = {"data": [  # documented shape
            {"id": "llama-3.3-70b-versatile", "context_window": 131072, "active": True},
            {"id": "old-model", "context_window": 8192, "active": False},
        ]}  # fmt: skip
        live, old = parse_groq_models(body)
        self.assertEqual((live.context_window, live.availability), (131072, Availability.AVAILABLE))
        self.assertEqual(old.availability, Availability.NEEDS_ACCESS)

    def test_openai_lists_ids(self) -> None:
        body = {
            "data": [{"id": "gpt-4o-mini", "object": "model", "owned_by": "system"}]
        }  # documented
        self.assertEqual([m.id for m in parse_openai_models(body)], ["gpt-4o-mini"])


class NativeProviders(unittest.TestCase):
    def test_anthropic(self) -> None:
        body = {"data": [{"id": "claude-sonnet-4-6", "display_name": "Claude Sonnet 4.6",  # documented
                          "max_input_tokens": 200000, "max_tokens": 64000, "type": "model"}]}  # fmt: skip
        [model] = parse_anthropic_models(body)
        self.assertEqual(
            (model.id, model.context_window, model.max_output), ("claude-sonnet-4-6", 200000, 64000)
        )

    def test_ai_studio_keeps_models_that_generate(self) -> None:
        body = {"models": [  # documented shape
            {"name": "models/gemini-2.5-flash", "displayName": "Gemini 2.5 Flash", "inputTokenLimit": 1048576,
             "outputTokenLimit": 65536, "supportedGenerationMethods": ["generateContent", "countTokens"]},
            {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
        ]}  # fmt: skip
        [model] = parse_ai_studio_models(body)
        self.assertEqual((model.id, model.context_window), ("gemini-2.5-flash", 1048576))

    def test_mistral_keeps_chat_models(self) -> None:
        body = {"data": [  # documented shape
            {"id": "mistral-large-latest", "max_context_length": 131072,
             "capabilities": {"completion_chat": True, "function_calling": True}},
            {"id": "mistral-embed", "capabilities": {"completion_chat": False}},
        ]}  # fmt: skip
        [model] = parse_mistral_models(body)
        self.assertEqual((model.id, model.tools), ("mistral-large-latest", True))

    def test_cohere(self) -> None:
        body = {
            "models": [{"name": "command-r-plus", "context_length": 128000, "features": ["tools"]}]
        }  # documented
        [model] = parse_cohere_models(body)
        self.assertEqual((model.id, model.tools), ("command-r-plus", True))


class Vertex(unittest.TestCase):
    def test_gemini_chat_models_only(self) -> None:
        ids = [m.id for m in vertex.vertex_gemini_models(recorded("vertex_garden_google.json"))]
        self.assertEqual(sorted(ids), ["gemini-2.5-pro", "gemini-3.5-flash"])

    def test_a_zero_quota_reads_as_needing_a_quota_increase(self) -> None:
        quotas = vertex.quota_values(recorded("vertex_quota_base_model.json"))
        self.assertEqual(quotas["anthropic-claude-sonnet-4-6"], 0)  # recorded as "details": {}
        self.assertEqual(quotas["google-ai-content-detection"], 600)
        models = {
            m.id: m
            for m in vertex.vertex_claude_models(recorded("vertex_garden_anthropic.json"), quotas)
        }
        haiku = models["claude-haiku-4-5@20251001"]  # a dated version keeps its @date
        self.assertEqual(haiku.availability, Availability.NEEDS_QUOTA)
        self.assertIn("base_model:anthropic-claude-haiku-4-5", haiku.note)
        self.assertEqual(
            models["claude-opus-5"].availability, Availability.UNKNOWN
        )  # no quota row for it

    def test_claude_without_quota_access_is_unknown_and_says_why(self) -> None:
        models = vertex.vertex_claude_models(recorded("vertex_garden_anthropic.json"), None)
        self.assertTrue(all(m.availability is Availability.UNKNOWN for m in models))
        self.assertIn("Cloud Quotas API", models[0].note)

    def test_open_models_are_publisher_slash_name(self) -> None:
        models = vertex.vertex_open_models("openai", recorded("vertex_garden_openai.json"))
        self.assertEqual([m.id for m in models], ["openai/gpt-oss-120b-maas"])


class Bedrock(unittest.TestCase):
    """Driven through botocore's Stubber, which validates against AWS's service model."""

    CONFIG = load_text(
        'version = 1\n[defaults]\nmodel = "aws/x"\n[providers.aws]\ntype = "bedrock"\nregion = "us-east-1"\n'
        'auth = { method = "aws_keys", access_key_id = "secret:id", secret_access_key = "secret:key" }\n'
    )

    def _discover(self, availability_by_model: dict[str, dict]):
        import boto3
        from botocore.stub import Stubber

        control = boto3.client(
            "bedrock", region_name="us-east-1", aws_access_key_id="a", aws_secret_access_key="b"
        )
        stub = Stubber(control)
        stub.add_response(
            "list_foundation_models",
            {"modelSummaries": [
                {"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-pro-v1:0",
                 "modelId": "amazon.nova-pro-v1:0", "modelName": "Nova Pro", "inferenceTypesSupported": ["ON_DEMAND"],
                 "modelLifecycle": {"status": "ACTIVE"}},
                {"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-4-6",
                 "modelId": "anthropic.claude-sonnet-4-6", "modelName": "Claude Sonnet 4.6",
                 "inferenceTypesSupported": ["INFERENCE_PROFILE"], "modelLifecycle": {"status": "ACTIVE"}},
            ]},
            {"byOutputModality": "TEXT"},
        )  # fmt: skip
        stub.add_response(
            "list_inference_profiles",
            {"inferenceProfileSummaries": [{
                "inferenceProfileName": "US Claude Sonnet 4.6", "inferenceProfileId": "us.anthropic.claude-sonnet-4-6",
                "inferenceProfileArn": "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.claude-sonnet-4-6",
                "models": [{"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-4-6"}],
                "status": "ACTIVE", "type": "SYSTEM_DEFINED"}]},
            {"typeEquals": "SYSTEM_DEFINED"},
        )  # fmt: skip
        for model_id, answer in availability_by_model.items():
            stub.add_response(
                "get_foundation_model_availability",
                {"modelId": model_id, **answer},
                {"modelId": model_id},
            )
        access = access_for(self.CONFIG, "aws", secrets={}, unavailable={})
        with (
            stub,
            mock.patch("yuyutsava.llm.providers.bedrock.aws_clients", return_value=(None, control)),
        ):
            return BedrockProvider().discover(access)

    def test_profiles_and_on_demand_models_with_this_accounts_access(self) -> None:
        granted = {"agreementAvailability": {"status": "AVAILABLE"}, "authorizationStatus": "AUTHORIZED",
                   "entitlementAvailability": "AVAILABLE", "regionAvailability": "AVAILABLE"}  # fmt: skip
        not_accepted = {**granted, "agreementAvailability": {"status": "NOT_AVAILABLE"}}
        found = self._discover(
            {"amazon.nova-pro-v1:0": granted, "anthropic.claude-sonnet-4-6": not_accepted}
        )
        models = {m.id: m for m in found.models}
        self.assertEqual(set(models), {"amazon.nova-pro-v1:0", "us.anthropic.claude-sonnet-4-6"})
        self.assertEqual(models["amazon.nova-pro-v1:0"].availability, Availability.AVAILABLE)
        profile = models["us.anthropic.claude-sonnet-4-6"]
        self.assertEqual(profile.availability, Availability.NEEDS_ACCESS)
        self.assertIn("Model access", profile.note)

    def test_each_availability_answer(self) -> None:
        base = {"agreementAvailability": {"status": "AVAILABLE"}, "authorizationStatus": "AUTHORIZED",
                "entitlementAvailability": "AVAILABLE", "regionAvailability": "AVAILABLE"}  # fmt: skip
        cases = {
            "region": ({**base, "regionAvailability": "NOT_AVAILABLE"}, Availability.NOT_IN_REGION),
            "iam": ({**base, "authorizationStatus": "NOT_AUTHORIZED"}, Availability.NEEDS_ACCESS),
            "pending": (
                {**base, "agreementAvailability": {"status": "PENDING"}},
                Availability.NEEDS_ACCESS,
            ),
            "error": ({**base, "agreementAvailability": {"status": "ERROR"}}, Availability.UNKNOWN),
            "none": (None, Availability.UNKNOWN),
        }
        for name, (answer, expected) in cases.items():
            with self.subTest(case=name):
                self.assertEqual(availability(answer, "us-east-1")[0], expected)


class FailuresAreExplained(unittest.TestCase):
    def _get(self, response_or_error):
        target = "yuyutsava.llm.providers.listing.httpx.get"
        if isinstance(response_or_error, Exception):
            return mock.patch(target, side_effect=response_or_error)
        return mock.patch(target, return_value=response_or_error)

    def test_a_refused_key(self) -> None:
        response = httpx.Response(401, json={"error": {"message": "Invalid API key"}})
        with self._get(response):
            result = listing.get_json("https://api.groq.com/openai/v1/models", label="Groq")
        self.assertEqual(result.problem, "Groq refused the login (401): Invalid API key")
        self.assertIn("check the key", result.fix)

    def test_an_unreachable_server(self) -> None:
        with self._get(httpx.ConnectError("refused")):
            result = listing.get_json(
                "http://localhost:11434/api/tags", label="Ollama", unreachable_fix="start Ollama"
            )
        self.assertEqual(result.problem, "could not reach Ollama at localhost:11434 (ConnectError)")
        self.assertEqual(result.fix, "start Ollama")

    def test_a_missing_secret_is_a_problem_not_an_error(self) -> None:
        access = access_for(
            load_text(
                'version = 1\n[defaults]\nmodel = "a/x"\n[providers.a]\ntype = "anthropic"\n'
                'auth = { method = "api_key", key = "secret:gone" }\n'
            ),
            "a",
            secrets={},
            unavailable={},
        )
        result = discover(access)
        self.assertFalse(result.ok)
        self.assertIn("cannot log in", result.problem)


class TheCheckFails(unittest.TestCase):
    """Negative control: a quota that is not zero is not read as needing an increase."""

    def test_a_granted_quota_is_available(self) -> None:
        body = recorded("vertex_garden_anthropic.json")
        models = vertex.vertex_claude_models(body, {"anthropic-claude-sonnet-4-6": 60})
        sonnet = next(m for m in models if m.id == "claude-sonnet-4-6")
        self.assertEqual(sonnet.availability, Availability.AVAILABLE)


if __name__ == "__main__":
    unittest.main()
