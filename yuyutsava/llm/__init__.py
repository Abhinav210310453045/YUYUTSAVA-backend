"""
yuyutsava.llm — the LLM provider layer.

Owns everything about talking to a model provider that is not pure config: which
optional SDK to import, how to construct its chat model, and which wire-format
quirks that SDK needs corrected.

    config     models.toml: schema, loader, store, secrets, watcher, the edits
               every surface makes, what this machine has, env leftovers
    tasks      the jobs a model is assigned to (chat, triage, subagent, …)
    selection  TaskModels — each task's model, from models.toml or the environment
    switch     a conversation's own model, applied per turn
    settings   the environment route: LLM_PROVIDER and its variables, read
               only while there is no models.toml
    target     ModelTarget — one model, its provider and how to log in
    factory    chat_model() — the single seam every model is built through
    handle     ModelHandle — a model plus its identity and capabilities
    base       the Provider contract + require() for optional SDKs
    providers  one module per provider type (anthropic, vertex, google, …)
    quirks     reusable per-provider wire fixes, shared across providers

Provider types — ``type`` in a ``[providers.<name>]`` table of ``models.toml``
(or ``LLM_PROVIDER`` without that file):

OpenAI-compatible — all served by ``providers/openai_compat.py``, which differs
only by ``base_url``:
    groq · openrouter · ollama · openai · openai_compatible
        (xAI, DeepSeek, Together, Fireworks, Perplexity, …)

Native SDKs (lazy-imported; install the matching extra):
    anthropic → ChatAnthropic            (prompt caching via middleware)
    google    → ChatGoogleGenerativeAI   [yuyutsava[google]]
    vertex    → by model family          [yuyutsava[vertex]]
                  Gemini: ChatGoogleGenerativeAI(vertexai=True), quirks/gemini_parts
                  Claude: ChatAnthropicVertex (prompt caching via llm/prompt_cache)
                  open models (MaaS): ChatOpenAI on Vertex's OpenAI endpoint
    bedrock   → ChatBedrockConverse      [yuyutsava[bedrock]]  (its own boto3 clients)
    azure     → AzureChatOpenAI          (langchain-openai, already installed)
    mistral   → ChatMistralAI            [yuyutsava[mistral]]
    cohere    → ChatCohere               [yuyutsava[cohere]]

Layering: ``config`` and ``settings`` (provider DATA — how to reach a provider)
import no SDK, and the provider modules build on them, never the reverse.
Much of the codebase reads settings; a quirk hook hanging off a Settings class
would drag SDK code into all of it.

This file deliberately imports nothing: import a name from the module that owns
it (``factory``, ``handle``, ``settings``, ``providers.registry``). Python runs a
package's ``__init__.py`` before any module inside it, so re-exporting the factory
here made reading a provider setting load LangChain.
"""
