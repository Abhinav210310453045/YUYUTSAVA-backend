"""
yuyutsava.llm — the LLM provider layer.

Owns everything about talking to a model provider that is not pure config: which
optional SDK to import, how to construct its chat model, and which wire-format
quirks that SDK needs corrected.

    settings   how to reach each provider: settings from the environment
    factory    chat_model() — the single seam every model is built through
    handle     ModelHandle — a model plus its identity and capabilities
    base       the Provider contract + require() for optional SDKs
    providers  one module per provider (anthropic, vertex, google, …)
    quirks     reusable per-provider wire fixes, shared across providers

Supported providers (set ``LLM_PROVIDER``):

OpenAI-compatible — all served by ``providers/openai_compat.py``, which differs
only by ``base_url``:
    groq · openrouter · ollama · openai · openai_compatible
        (xAI, DeepSeek, Together, Fireworks, Perplexity, …)

Native SDKs (lazy-imported; install the matching extra):
    anthropic → ChatAnthropic            (prompt caching via middleware)
    google    → ChatGoogleGenerativeAI   [yuyutsava[google]]   ⎫ Gemini wire format:
    vertex    → ChatVertexAI             [yuyutsava[vertex]]   ⎭ quirks/gemini_parts
    bedrock   → ChatBedrockConverse      [yuyutsava[bedrock]]
    azure     → AzureChatOpenAI          (langchain-openai, already installed)
    mistral   → ChatMistralAI            [yuyutsava[mistral]]
    cohere    → ChatCohere               [yuyutsava[cohere]]

Layering: ``settings`` (provider DATA — how to reach a provider) imports only
``yuyutsava.config``, and the provider modules build on it, never the reverse.
Much of the codebase reads settings; a quirk hook hanging off a Settings class
would drag SDK code into all of it.

This file deliberately imports nothing: import a name from the module that owns
it (``factory``, ``handle``, ``settings``, ``providers.registry``). Python runs a
package's ``__init__.py`` before any module inside it, so re-exporting the factory
here made reading a provider setting load LangChain.
"""
