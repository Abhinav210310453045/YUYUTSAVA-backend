"""``ModelHandle`` — identity and capabilities come from the provider.

ADR-004 item 2.

Two things are being protected here, and they pull in opposite directions:

**Nothing may change.** ``model_name_of`` feeds every usage row and the task
registry's ``model`` column. Every provider configuration but one must
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

No API calls: constructing a chat model only builds a client object — Bedrock's
included, now that its clients are built from the login in the config.

Run:  .venv/bin/python -m pytest test/llm/test_model_handle.py
"""

from __future__ import annotations

import gc
import unittest

from test.support.targets import every_provider_type
from test.support.targets import every_type_target as target
from yuyutsava.llm.config.schema import connection_types
from yuyutsava.llm.factory import chat_model, handle_for, model_handle, model_name_of, supports
from yuyutsava.llm.handle import Capability
from yuyutsava.llm.providers.registry import all_providers, provider_for

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
    "bedrock": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
}

TARGETS = every_provider_type()


class NameIsUnchanged(unittest.TestCase):
    """The reporting path must be behaviour-preserving everywhere it worked."""

    def test_every_recorded_name_still_matches(self) -> None:
        for key, expected in BASELINE_NAMES.items():
            with self.subTest(provider=key):
                self.assertEqual(
                    model_name_of(chat_model(TARGETS[key])),
                    expected,
                    f"{key} reports a different model name than before this "
                    f"change; usage rows and the task registry would shift",
                )

    def test_the_baseline_covers_every_buildable_provider(self) -> None:
        """Negative control — the check above proves nothing about a provider it skips."""
        covered = set(BASELINE_NAMES) | {"azure"}
        self.assertEqual(
            covered,
            set(TARGETS),
            "a provider is in TARGETS but pinned by neither the baseline nor the Azure case",
        )


class AzureIdentityWasBroken(unittest.TestCase):
    """The one deliberate change, plus proof that it was needed."""

    def setUp(self) -> None:
        self.model = chat_model(TARGETS["azure"])

    def test_the_attribute_probe_really_is_blind_to_azure(self) -> None:
        """Negative control: the mechanism this replaced yields nothing here."""
        probed = ""
        for attr in ("model_name", "model", "model_id"):
            v = getattr(self.model, attr, None)
            if isinstance(v, str) and v:
                probed = v
                break
        self.assertEqual(
            probed,
            "",
            "AzureChatOpenAI now exposes a usable name attribute, so the bug "
            "this fixes no longer exists — re-check whether naming the model "
            "from the target (Provider.model_name) is still needed",
        )

    def test_the_handle_names_it_anyway(self) -> None:
        self.assertEqual(model_name_of(self.model), "gpt-4o")

    def test_falls_back_to_the_deployment(self) -> None:
        """Without ``base_model`` the deployment is the only identifier there is."""
        self.assertEqual(model_name_of(chat_model(target("azure/my-deploy"))), "my-deploy")


class HandleCarriesTheProvider(unittest.TestCase):
    def test_host_not_implementation_class(self) -> None:
        """Five hosts share OpenAICompatibleProvider; the handle must separate them."""
        for key in ("groq", "openrouter", "ollama", "openai"):
            with self.subTest(host=key):
                self.assertEqual(model_handle(TARGETS[key]).provider, key)

    def test_generic_escape_hatch_reports_the_provider_key(self) -> None:
        self.assertEqual(
            model_handle(target("openai_compatible/some/model")).provider, "openai_compatible"
        )

    def test_native_providers_report_their_own_key(self) -> None:
        for key in ("anthropic", "google", "vertex", "bedrock", "mistral", "cohere", "azure"):
            with self.subTest(provider=key):
                self.assertEqual(model_handle(TARGETS[key]).provider, key)

    def test_every_provider_type_has_exactly_one_provider(self) -> None:
        """A type the config accepts but nothing builds would fail only when chosen."""
        served = [kind for provider in all_providers() for kind in provider.types]
        self.assertEqual(sorted(served), sorted(connection_types()))


class Capabilities(unittest.TestCase):
    """Only capabilities with a real call site are declared — assert both ways."""

    def test_gemini_providers_are_loop_affine(self) -> None:
        for key in ("google", "vertex"):
            with self.subTest(provider=key):
                self.assertTrue(
                    supports(chat_model(TARGETS[key]), Capability.LOOP_AFFINE),
                    f"{key} builds a loop-bound async client but does not "
                    f"declare it; see quirks/loop_affinity.py",
                )

    def test_others_are_not(self) -> None:
        for key in ("groq", "anthropic", "mistral", "cohere", "azure"):
            with self.subTest(provider=key):
                self.assertFalse(supports(chat_model(TARGETS[key]), Capability.LOOP_AFFINE))

    def test_only_openai_compatible_toggles_reasoning(self) -> None:
        self.assertTrue(supports(chat_model(TARGETS["groq"]), Capability.REASONING_TOGGLE))
        for key in ("anthropic", "google", "vertex", "bedrock", "mistral", "cohere", "azure"):
            with self.subTest(provider=key):
                self.assertFalse(
                    supports(chat_model(TARGETS[key]), Capability.REASONING_TOGGLE),
                    f"{key} claims a reasoning toggle it does not implement — "
                    f"disable_reasoning is silently ignored there",
                )

    def test_bedrock_is_built_for_real(self) -> None:
        """Its clients are built here now, so it no longer needs live AWS credentials."""
        provider = provider_for(TARGETS["bedrock"])
        self.assertEqual(provider.capabilities, frozenset())
        self.assertEqual(type(chat_model(TARGETS["bedrock"])).__name__, "ChatBedrockConverse")


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
        from yuyutsava.llm.factory import registered_count

        gc.collect()
        before = registered_count()
        models = [chat_model(TARGETS["groq"]) for _ in range(25)]
        self.assertEqual(
            registered_count(),
            before + 25,
            "models were built but not registered — model_name_of would fall back to probing",
        )
        del models
        gc.collect()
        self.assertEqual(
            registered_count(),
            before,
            "handles outlived their models; a long-running daemon would grow "
            "this dict without bound",
        )

    def test_each_instance_keeps_its_own_identity(self) -> None:
        a = chat_model(TARGETS["groq"])
        b = chat_model(TARGETS["ollama"])
        self.assertEqual(model_name_of(a), BASELINE_NAMES["groq"])
        self.assertEqual(model_name_of(b), BASELINE_NAMES["ollama"])
        self.assertEqual(handle_for(a).provider, "groq")
        self.assertEqual(handle_for(b).provider, "ollama")

    def test_the_handle_points_at_the_model_it_was_asked_about(self) -> None:
        """``handle_for`` rebuilds the handle; it must rebuild the right one."""
        a = chat_model(TARGETS["groq"])
        self.assertIs(handle_for(a).model, a)


class HandleShape(unittest.TestCase):
    def test_model_is_the_real_thing_not_a_wrapper(self) -> None:
        """ADR-004 Alternative C: we do not wrap ``BaseChatModel``."""
        from langchain_core.language_models import BaseChatModel

        self.assertIsInstance(model_handle(TARGETS["groq"]).model, BaseChatModel)

    def test_handle_is_frozen(self) -> None:
        handle = model_handle(TARGETS["groq"])
        with self.assertRaises(Exception):
            handle.name = "something-else"  # type: ignore[misc]

    def test_str_is_readable(self) -> None:
        self.assertEqual(str(model_handle(TARGETS["vertex"])), "vertex:gemini-2.5-flash")

    def test_the_module_imports_no_framework(self) -> None:
        """Domain code must be able to hold a handle without pulling LangChain in."""
        import ast
        import pathlib

        src = (pathlib.Path(__file__).resolve().parents[2] / "yuyutsava/llm/handle.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(src)

        # Everything under `if TYPE_CHECKING:` is erased at runtime. Collect
        # those nodes by identity — `ast.walk` descends into an If's body, so
        # skipping the If node itself would not skip its imports.
        deferred = {
            id(child)
            for node in ast.walk(tree)
            if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.dump(node.test)
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
                if any(candidate == fw or candidate.startswith(fw + ".") for fw in frameworks):
                    runtime_imports.append(candidate)
        self.assertEqual(
            runtime_imports,
            [],
            f"handle.py imports a framework at runtime: {runtime_imports}. The "
            f"annotation belongs under `if TYPE_CHECKING:`",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
