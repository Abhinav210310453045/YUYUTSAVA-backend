"""``ModelHandle`` — identity and capabilities come from the provider.

Phase 4 step 4.1, ADR-004 item 2.

Two things are being protected here, and they pull in opposite directions:

**Nothing may change.** ``model_name_of`` feeds every usage row and the task
registry's ``model`` column. Ten of the eleven provider configurations must
return byte-identical names to what the attribute probe returned before this
change — those values were recorded from the running code first, and are pinned
below as literals.

**One thing must change.** The eleventh, Azure, returned ``""``. Not a
hypothetical fragility: ``AzureChatOpenAI`` is constructed from
``azure_deployment`` and leaves ``model_name`` at ``None``, so every Azure usage
row recorded a blank model and nothing failed.
``test_the_attribute_probe_really_is_blind_to_azure`` is the negative control —
it asserts the *old* mechanism still yields nothing, so the fix is demonstrably
load-bearing rather than incidental.

No API calls: constructing a chat model only builds a client object. Bedrock is
the one provider that cannot be constructed without live AWS credentials, so it
is asserted structurally rather than by building it.

Run:  .venv/bin/python test/llm/test_model_handle.py
"""

from __future__ import annotations

import gc
import unittest

from yuyutsava.core import config as C
from yuyutsava.llm import (
    Capability,
    ModelHandle,
    chat_model,
    handle_for,
    model_handle,
    model_name_of,
    provider_for,
    supports,
)

# Recorded from the pre-change code. `azure` is deliberately absent — it had no
# correct value to preserve; see `AzureIdentityWasBroken`.
BASELINE_NAMES = {
    "groq": "llama-3.3-70b-versatile",
    "openrouter": "openai/gpt-4o-mini",
    "ollama": "gemma4:e2b",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5-20251001",
    "google": "gemini-2.5-flash",
    "vertex": "gemini-2.5-flash",
    "mistral": "mistral-large-latest",
    "cohere": "command-r-plus",
}

SETTINGS = {
    "groq": C.GroqSettings(
        api_key="k", base_url="https://x", model="llama-3.3-70b-versatile"),
    "openrouter": C.OpenRouterSettings(
        api_key="k", base_url="https://x", model="openai/gpt-4o-mini"),
    "ollama": C.OllamaSettings(
        base_url="http://localhost:11434/v1", model="gemma4:e2b"),
    "openai": C.OpenAISettings(
        api_key="k", base_url="https://api.openai.com/v1", model="gpt-4o-mini"),
    "anthropic": C.AnthropicSettings(
        api_key="k", model="claude-haiku-4-5-20251001"),
    "google": C.GoogleSettings(api_key="k", model="gemini-2.5-flash"),
    "vertex": C.VertexSettings(
        project="p", location="us-central1", model="gemini-2.5-flash"),
    "mistral": C.MistralSettings(api_key="k", model="mistral-large-latest"),
    "cohere": C.CohereSettings(api_key="k", model="command-r-plus"),
    "azure": C.AzureOpenAISettings(
        api_key="k", azure_endpoint="https://x", azure_deployment="dep",
        api_version="2024-10-21", model="gpt-4o"),
}


class NameIsUnchanged(unittest.TestCase):
    """The reporting path must be behaviour-preserving everywhere it worked."""

    def test_every_recorded_name_still_matches(self) -> None:
        for key, expected in BASELINE_NAMES.items():
            with self.subTest(provider=key):
                self.assertEqual(
                    model_name_of(chat_model(SETTINGS[key])), expected,
                    f"{key} reports a different model name than before this "
                    f"change; usage rows and the task registry would shift",
                )

    def test_the_baseline_covers_every_buildable_provider(self) -> None:
        """Negative control — the check above proves nothing about a provider it skips."""
        covered = set(BASELINE_NAMES) | {"azure"}
        self.assertEqual(
            covered, set(SETTINGS),
            "a provider is in SETTINGS but pinned by neither the baseline nor "
            "the Azure case",
        )


class AzureIdentityWasBroken(unittest.TestCase):
    """The one deliberate change, plus proof that it was needed."""

    def setUp(self) -> None:
        self.model = chat_model(SETTINGS["azure"])

    def test_the_attribute_probe_really_is_blind_to_azure(self) -> None:
        """Negative control: the mechanism this replaced yields nothing here."""
        probed = ""
        for attr in ("model_name", "model", "model_id"):
            v = getattr(self.model, attr, None)
            if isinstance(v, str) and v:
                probed = v
                break
        self.assertEqual(
            probed, "",
            "AzureChatOpenAI now exposes a usable name attribute, so the bug "
            "this fixes no longer exists — re-check whether the override in "
            "AzureOpenAIProvider.model_name is still the right answer",
        )

    def test_the_handle_names_it_anyway(self) -> None:
        self.assertEqual(model_name_of(self.model), "gpt-4o")

    def test_falls_back_to_the_deployment(self) -> None:
        """``model`` is informational on Azure and may be empty."""
        settings = C.AzureOpenAISettings(
            api_key="k", azure_endpoint="https://x", azure_deployment="my-deploy",
            api_version="2024-10-21", model="")
        self.assertEqual(model_name_of(chat_model(settings)), "my-deploy")


class HandleCarriesTheProvider(unittest.TestCase):
    def test_host_not_implementation_class(self) -> None:
        """Five hosts share OpenAICompatibleProvider; the handle must separate them."""
        for key in ("groq", "openrouter", "ollama", "openai"):
            with self.subTest(host=key):
                self.assertEqual(model_handle(SETTINGS[key]).provider, key)

    def test_generic_escape_hatch_reports_the_provider_key(self) -> None:
        settings = C.OpenAICompatibleSettings(
            api_key="k", base_url="https://x", model="some/model")
        self.assertEqual(model_handle(settings).provider, "openai_compatible")

    def test_native_providers_report_their_own_key(self) -> None:
        for key in ("anthropic", "google", "vertex", "mistral", "cohere", "azure"):
            with self.subTest(provider=key):
                self.assertEqual(model_handle(SETTINGS[key]).provider, key)

    def test_every_provider_declares_a_key(self) -> None:
        """Ratchet — a new provider with no key would report ``""`` forever."""
        from yuyutsava.llm.providers import _FALLBACK, _PROVIDERS

        for provider in (*_PROVIDERS, _FALLBACK):
            with self.subTest(provider=type(provider).__name__):
                self.assertTrue(
                    provider.key,
                    f"{type(provider).__name__} declares no `key`; every model "
                    f"it builds would be recorded with an empty provider",
                )


class Capabilities(unittest.TestCase):
    """Only capabilities with a real call site are declared — assert both ways."""

    def test_gemini_providers_are_loop_affine(self) -> None:
        for key in ("google", "vertex"):
            with self.subTest(provider=key):
                self.assertTrue(
                    supports(chat_model(SETTINGS[key]), Capability.LOOP_AFFINE),
                    f"{key} builds a loop-bound async client but does not "
                    f"declare it; see quirks/loop_affinity.py",
                )

    def test_others_are_not(self) -> None:
        for key in ("groq", "anthropic", "mistral", "cohere", "azure"):
            with self.subTest(provider=key):
                self.assertFalse(
                    supports(chat_model(SETTINGS[key]), Capability.LOOP_AFFINE))

    def test_only_openai_compatible_toggles_reasoning(self) -> None:
        self.assertTrue(
            supports(chat_model(SETTINGS["groq"]), Capability.REASONING_TOGGLE))
        for key in ("anthropic", "google", "vertex", "mistral", "cohere", "azure"):
            with self.subTest(provider=key):
                self.assertFalse(
                    supports(chat_model(SETTINGS[key]), Capability.REASONING_TOGGLE),
                    f"{key} claims a reasoning toggle it does not implement — "
                    f"disable_reasoning is silently ignored there",
                )

    def test_bedrock_declares_nothing_special(self) -> None:
        """Structural: Bedrock cannot be built without live AWS credentials."""
        provider = provider_for(
            C.BedrockSettings(region="us-east-1", model="m"))
        self.assertEqual(type(provider).__name__, "BedrockProvider")
        self.assertEqual(provider.key, "bedrock")
        self.assertEqual(provider.capabilities, frozenset())
        self.assertEqual(
            provider.model_name(C.BedrockSettings(region="us-east-1", model="m")),
            "m",
        )


class UnknownModels(unittest.TestCase):
    """Models this system did not build still have to work."""

    class _Fake:
        model_name = "fake-model-v1"

    def test_probe_still_answers_for_a_foreign_model(self) -> None:
        self.assertEqual(model_name_of(self._Fake()), "fake-model-v1")

    def test_no_handle_for_a_foreign_model(self) -> None:
        self.assertIsNone(handle_for(self._Fake()))

    def test_capabilities_are_denied_not_assumed(self) -> None:
        for cap in Capability:
            with self.subTest(capability=cap):
                self.assertFalse(supports(self._Fake(), cap))

    def test_nameless_model_yields_empty_string(self) -> None:
        self.assertEqual(model_name_of(object()), "")


class TheRegistryDoesNotLeak(unittest.TestCase):
    """A daemon builds a fresh model per task; entries must not accumulate."""

    def test_entries_are_evicted_when_the_model_is_collected(self) -> None:
        from yuyutsava.llm.factory import _registered_count

        gc.collect()
        before = _registered_count()
        models = [chat_model(SETTINGS["groq"]) for _ in range(25)]
        self.assertEqual(
            _registered_count(), before + 25,
            "models were built but not registered — model_name_of would fall "
            "back to probing",
        )
        del models
        gc.collect()
        self.assertEqual(
            _registered_count(), before,
            "handles outlived their models; a long-running daemon would grow "
            "this dict without bound",
        )

    def test_each_instance_keeps_its_own_identity(self) -> None:
        a = chat_model(SETTINGS["groq"])
        b = chat_model(SETTINGS["ollama"])
        self.assertEqual(model_name_of(a), BASELINE_NAMES["groq"])
        self.assertEqual(model_name_of(b), BASELINE_NAMES["ollama"])
        self.assertEqual(handle_for(a).provider, "groq")
        self.assertEqual(handle_for(b).provider, "ollama")

    def test_the_handle_points_at_the_model_it_was_asked_about(self) -> None:
        """``handle_for`` rebuilds the handle; it must rebuild the right one."""
        a = chat_model(SETTINGS["groq"])
        self.assertIs(handle_for(a).model, a)


class HandleShape(unittest.TestCase):
    def test_model_is_the_real_thing_not_a_wrapper(self) -> None:
        """ADR-004 Alternative C: we do not wrap ``BaseChatModel``."""
        from langchain_core.language_models import BaseChatModel

        self.assertIsInstance(model_handle(SETTINGS["groq"]).model, BaseChatModel)

    def test_handle_is_frozen(self) -> None:
        handle = model_handle(SETTINGS["groq"])
        with self.assertRaises(Exception):
            handle.name = "something-else"  # type: ignore[misc]

    def test_str_is_readable(self) -> None:
        self.assertEqual(str(model_handle(SETTINGS["vertex"])),
                         "vertex:gemini-2.5-flash")

    def test_the_module_imports_no_framework(self) -> None:
        """Domain code must be able to hold a handle without pulling LangChain in."""
        import ast
        import pathlib

        src = (
            pathlib.Path(__file__).resolve().parents[2] / "yuyutsava/llm/handle.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(src)

        # Everything under `if TYPE_CHECKING:` is erased at runtime. Collect
        # those nodes by identity — `ast.walk` descends into an If's body, so
        # skipping the If node itself would not skip its imports.
        deferred = {
            id(child)
            for node in ast.walk(tree)
            if isinstance(node, ast.If)
            and "TYPE_CHECKING" in ast.dump(node.test)
            for stmt in node.body
            for child in ast.walk(stmt)
        }
        frameworks = ("langchain", "langchain_core", "langgraph", "deepagents")
        runtime_imports: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)) or id(node) in deferred:
                continue
            module = getattr(node, "module", None) or ""
            for candidate in (module, *(a.name for a in node.names)):
                if any(candidate == fw or candidate.startswith(fw + ".")
                       for fw in frameworks):
                    runtime_imports.append(candidate)
        self.assertEqual(
            runtime_imports, [],
            f"handle.py imports a framework at runtime: {runtime_imports}. The "
            f"annotation belongs under `if TYPE_CHECKING:`",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
