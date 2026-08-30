"""Structural fingerprint of a built agent — the Phase 1 safety net.

ADR-001 replaces three ~250-line builders with one composition pipeline. That is
a behaviour-preserving refactor, and the only way to *know* it preserved
behaviour is to compare what each builder hands to the framework, before and
after.

This captures exactly that. Rather than compiling and invoking a graph, it
intercepts ``create_deep_agent`` and records its keyword arguments: the
middleware stack (in order), the bound tool names, the subagent specs, and a
hash of the system prompt. Those four things *are* the agent's structure —
everything the builders decide ends up in them.

Why interception rather than invocation:

  * no model call, no network, nothing billable;
  * no graph compilation, so it runs in milliseconds;
  * **middleware order is observable**, which it is not from outside a compiled
    graph — and order is a correctness property here (offload must precede
    compaction; budget must follow it).

Usage::

    before = fingerprint_cli()          # on the old builder
    # ... refactor ...
    after = fingerprint_cli()           # on the pipeline
    assert before == after

``diff_fingerprints`` reports what moved, field by field, so a mismatch names
the middleware or tool that changed instead of dumping two dicts.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import os
from pathlib import Path
from typing import Any

os.environ.setdefault("LLM_PROVIDER", "anthropic")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-fake-key-for-fingerprint")

REPO = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Interception
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _capture_deep_agent_kwargs(record: dict[str, Any]):
    """Patch ``engine.create_deep_agent`` to record kwargs and skip compilation."""
    from yuyutsava.core import engine

    original = engine.create_deep_agent

    def _fake(**kwargs: Any) -> Any:
        record.update(kwargs)

        class _Stub:
            """Stands in for a CompiledStateGraph; never invoked."""

        return _Stub()

    engine.create_deep_agent = _fake  # type: ignore[assignment]
    try:
        yield
    finally:
        engine.create_deep_agent = original  # type: ignore[assignment]


@contextlib.contextmanager
def _fake_chat_model():
    """Keep model construction offline — no provider SDK, no credentials, no calls."""
    import yuyutsava.llm as llm
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    original = llm.chat_model

    def _fake(settings: Any, **_kw: Any) -> Any:
        m = FakeListChatModel(responses=["ok"])
        # model_name_of() probes these; give it something stable so the
        # fingerprint does not vary by provider defaults.
        object.__setattr__(m, "model_name", "fingerprint-fake")
        return m

    llm.chat_model = _fake  # type: ignore[assignment]
    try:
        yield
    finally:
        llm.chat_model = original  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def _tool_name(t: Any) -> str:
    n = getattr(t, "name", None)
    if isinstance(n, str) and n:
        return n
    if isinstance(t, dict) and isinstance(t.get("name"), str):
        return t["name"]
    return getattr(t, "__name__", type(t).__name__)


def _subagent_digest(spec: Any) -> dict[str, Any]:
    """Identity + structure of one subagent spec, minus object identities.

    Middleware here goes through ``_middleware_digest`` rather than
    ``type(m).__name__``: subagents get their own stacks, and since Phase 4 those
    can contain a ``LangChainPolicyAdapter`` whose class name says nothing about
    what it enforces. Reporting a bare ``LangChainPolicyAdapter`` would hide a
    subagent losing its offload policy entirely.
    """
    if not isinstance(spec, dict):
        return {"name": str(spec), "kind": type(spec).__name__}
    kind = "async" if ("graph_id" in spec and "url" in spec) else "sync"
    return {
        "name": spec.get("name", "?"),
        "kind": kind,
        "middleware": [_middleware_digest(m) for m in (spec.get("middleware") or [])],
        "tools": sorted(_tool_name(t) for t in (spec.get("tools") or [])),
    }


def _middleware_digest(m: Any) -> str:
    """Class name, plus internals for containers whose contents are the behaviour.

    ``RetrievalInjectionMiddleware`` holds the ordered injector list, and the
    injectors are the whole point of it — a refactor could drop or reorder one
    and the class name alone would not move. Ordered, not sorted: injected
    blocks land in the prompt in list order, and the prompt is a cached prefix.

    ``SkillInjector`` additionally carries an ``agent`` scope which decides
    *whose* skills are searched, so it is rendered as ``SkillInjector(cli)`` vs
    ``SkillInjector(*)``. That distinction is invisible from the class name and
    is a real behavioural difference between the masters.
    """
    name = type(m).__name__

    # Phase 4: LangChainPolicyAdapter is a container too, and the policies inside
    # it ARE the behaviour. Reporting only the adapter's class name would make
    # the stack less legible than the framework subclasses it replaces, and would
    # hide a policy being dropped — the opposite of what this gate is for.
    #
    # Recurse rather than just naming them: RetrievalInjectionPolicy carries the
    # injector chain, and rendering it as a bare name would silently drop the
    # detail this function exists to expose.
    policies = getattr(m, "policies", None)
    if policies is not None:
        inner = ",".join(_middleware_digest(p) for p in policies)
        return f"{name}[{inner}]"

    injectors = getattr(m, "_injectors", None)
    if injectors is None:
        # A migrated Policy reports the name it declares, which the profile
        # enum and the ordering rules both match on.
        return getattr(m, "name", None) or name
    name = getattr(m, "name", None) or name
    parts = []
    for inj in injectors:
        # SkillInjector wraps a RetrievalInjector and keeps its agent scope in
        # the inner search kwargs, so it is not a direct attribute. The scope
        # decides WHOSE skills are searched: the CLI passes None (all agents),
        # the orchestrator and tinker scope to themselves. Invisible from the
        # class name, and a genuine behavioural difference.
        inner = getattr(inj, "_inner", None)
        scope = (getattr(inner, "_search_kwargs", None) or {}).get("agent")
        parts.append(f"{type(inj).__name__}({scope})" if scope else type(inj).__name__)
    return f"{name}[{','.join(parts)}]"


def _behaviour_order(middleware: list[Any]) -> list[str]:
    """Cross-cutting concerns in stack order, adapters expanded to their policies.

    ``middleware`` answers "what does the stack literally look like"; this answers
    "in what order do the behaviours run", which is what the ordering rules are
    actually about and what survives repackaging.

    Needed because the rules match by name and two of them went **silently
    vacuous** the moment ``ToolResultOffloadMiddleware`` became
    ``LangChainPolicyAdapter[ToolResultOffloadPolicy]``: the rule looked the old
    name up, did not find it, and skipped. ``EveryOrderRuleFires`` now makes that
    impossible to repeat.
    """
    out: list[str] = []
    for m in middleware:
        policies = getattr(m, "policies", None)
        if policies is not None:
            out.extend(p.name for p in policies)
        else:
            out.append(type(m).__name__)
    return out


#: The hook chains a stack entry can participate in. Order within a chain is
#: decided by list position; order *between* chains is decided by the agent loop
#: and is not a property of the list at all.
CHAINS = ("model_call", "tool_before", "tool_after",
          "before_model", "after_model", "after_agent")


def _chains_of(entry: Any) -> dict[str, list[str]]:
    """``chain -> [behaviour names]`` contributed by one stack entry."""
    out: dict[str, list[str]] = {c: [] for c in CHAINS}
    policies = getattr(entry, "policies", None)
    if policies is not None:
        for p in policies:
            if p.handles_model_call():
                out["model_call"].append(p.name)
            if p.handles_before():
                out["tool_before"].append(p.name)
            if p.handles_after():
                out["tool_after"].append(p.name)
            for phase in ("before_model", "after_model", "after_agent"):
                if p.observes(phase):
                    out[phase].append(p.name)
        return out

    # A framework middleware: read the hooks it overrides.
    from langchain.agents.middleware import AgentMiddleware

    name = type(entry).__name__
    hooks = {
        "model_call": "awrap_model_call",
        "tool_before": "awrap_tool_call",
        "before_model": "abefore_model",
        "after_model": "aafter_model",
        "after_agent": "aafter_agent",
    }
    for chain, hook in hooks.items():
        if getattr(type(entry), hook, None) is not getattr(AgentMiddleware, hook, None):
            out[chain].append(name)
    return out


def _chain_order(middleware: list[Any]) -> dict[str, list[str]]:
    """Behaviours per hook chain, in the order they will run.

    **This is what "middleware order" actually means.** Three of the four
    ordering rules this gate enforced compared entries in *different* chains —
    offload (a tool hook) against compaction (a before-model hook), for instance.
    Those orderings hold, but they hold because of how the agent loop is
    structured, not because of where the entries sit in the list, so asserting
    them against list position was checking a property the list does not control.

    Only same-chain pairs are genuinely positional. Collapsing the per-policy
    adapters into one changes list positions wholesale while leaving every
    same-chain order untouched, and this is the fingerprint field that shows it.
    """
    merged: dict[str, list[str]] = {c: [] for c in CHAINS}
    for entry in middleware:
        for chain, names in _chains_of(entry).items():
            merged[chain].extend(names)
    return merged


def _attached_behaviours(middleware: list[Any]) -> set[str]:
    """Every cross-cutting concern on the graph, however it is packaged.

    Phase 4 moves policies out of their own ``AgentMiddleware`` subclasses and
    into ``LangChainPolicyAdapter``. "Is the permission check attached?" is the
    same question before and after, so it gets one answer that does not depend
    on the packaging.
    """
    names: set[str] = set()
    for m in middleware:
        policies = getattr(m, "policies", None)
        if policies is not None:
            names.update(p.name for p in policies)
        else:
            names.add(type(m).__name__)
    return names


def fingerprint_from_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Normalise captured ``create_deep_agent`` kwargs into a comparable dict.

    Middleware is a **list** (order matters). Tools and subagents are sorted —
    their order is not a behavioural property. The prompt is hashed rather than
    stored so a fingerprint stays small and readable; ``prompt_len`` is kept
    alongside so a mismatch says whether the prompt grew or merely changed.
    """
    prompt = kwargs.get("system_prompt") or ""
    subs = kwargs.get("subagents") or []
    middleware = kwargs.get("middleware") or []
    return {
        "middleware": [_middleware_digest(m) for m in middleware],
        # Behaviours in run order, adapters expanded. `middleware` is the literal
        # stack; this is what the ordering rules assert against so a repackaging
        # cannot silently disable them.
        "order": _behaviour_order(middleware),
        # Per-hook-chain run order — the only ordering the list actually
        # controls. See `_chain_order`.
        "chains": _chain_order(middleware),
        # Flattened behaviour set: every middleware, plus every policy inside a
        # LangChainPolicyAdapter. `middleware` answers "in what order?" and must
        # stay a list; this answers "is X attached at all?" without a caller
        # having to know whether X is still its own class or lives in an adapter.
        "policies": sorted(_attached_behaviours(middleware)),
        "tools": sorted(_tool_name(t) for t in (kwargs.get("tools") or [])),
        "subagents": sorted(
            (_subagent_digest(s) for s in subs), key=lambda d: str(d.get("name"))
        ),
        "backend": type(kwargs.get("backend")).__name__,
        "prompt_sha": hashlib.sha256(prompt.encode()).hexdigest()[:16],
        "prompt_len": len(prompt),
        "has_checkpointer": kwargs.get("checkpointer") is not None,
    }


def diff_fingerprints(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Human-readable differences; empty list means structurally identical."""
    out: list[str] = []
    for key in sorted(set(before) | set(after)):
        b, a = before.get(key), after.get(key)
        if b == a:
            continue
        if key == "middleware":
            out.append(f"middleware order/content changed:\n    before: {b}\n    after:  {a}")
        elif key in ("tools",):
            bs, as_ = set(b or []), set(a or [])
            if bs - as_:
                out.append(f"tools REMOVED: {sorted(bs - as_)}")
            if as_ - bs:
                out.append(f"tools ADDED: {sorted(as_ - bs)}")
        elif key == "subagents":
            bn = {d["name"] for d in (b or [])}
            an = {d["name"] for d in (a or [])}
            if bn - an:
                out.append(f"subagents REMOVED: {sorted(bn - an)}")
            if an - bn:
                out.append(f"subagents ADDED: {sorted(an - bn)}")
            if bn == an:
                out.append(f"subagent internals changed:\n    before: {b}\n    after:  {a}")
        else:
            out.append(f"{key}: {b!r} -> {a!r}")
    return out


# ---------------------------------------------------------------------------
# Per-profile fingerprints
# ---------------------------------------------------------------------------


def _workspace() -> Path:
    ws = REPO / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def wired_stores(tmp: Path) -> dict[str, Any]:
    """The optional dependencies that make the FULL middleware stack materialise.

    Critical for coverage. With no stores wired, ``_context_middleware`` adds
    neither offload nor compaction, ``BudgetPolicy`` and ``UsagePolicy``
    are skipped, and no injectors are built — so a fingerprint taken on the bare
    path contains none of them, and would happily accept a refactor that
    scrambled their order.

    Middleware order is a correctness property here:

        offload BEFORE compaction   (offload runs on the tool path, so the
                                     compactor counts post-offload tokens)
        budget  AFTER  compaction   (the ceiling must see real, final usage)
        usage   AFTER  budget       (passive accounting of the same usage)

    Stores are constructed but never used — schema creation is lazy — so this
    stays fast and touches no database.
    """
    from yuyutsava.context.artifacts_unified import (
    UnifiedArtifactStore, sqlite_artifact_store,
)
    from yuyutsava.context.config import ContextSettings
    from yuyutsava.context.summary_store_unified import sqlite_summary_store
    from yuyutsava.context.transcript_store_unified import sqlite_transcript_store
    from yuyutsava.daemon.usage import SqliteUsageStore
    from yuyutsava.memory.store_unified import sqlite_memory_store
    from yuyutsava.skills.store_unified import sqlite_skill_store

    class _Opaque:
        """Stand-in for a dependency the builders only null-check and pass on.

        ``transcript_index`` (PgTranscriptIndex) and ``prefs_store`` (PrefsStore)
        need a live Postgres pool / events store to construct for real, but the
        builders only test them for None and hand them to an injector whose
        ``__init__`` just stores the reference. A sentinel is enough to make the
        injector materialise, which is what the fingerprint needs to see.
        """

    db = tmp / "fingerprint.db"
    return {
        "artifact_store": sqlite_artifact_store(db),
        "summary_store": sqlite_summary_store(db),
        "transcript_store": sqlite_transcript_store(db),
        "memory_store": sqlite_memory_store(db),
        "usage_store": SqliteUsageStore(db),
        "skill_store": sqlite_skill_store(db),
        "transcript_index": _Opaque(),
        "prefs_store": _Opaque(),
        "context_settings": ContextSettings(),
        "budget_tokens": 120_000,
    }


def fingerprint_cli(**overrides: Any) -> dict[str, Any]:
    from yuyutsava.agents.general_purpose.agent import GeneralPurposeAgent
    from yuyutsava.agents.task_runner.agent import TaskRunnerAgent
    from yuyutsava.core import engine
    from yuyutsava.core.config import AnthropicSettings, LocalSettings, SearchConfig
    from yuyutsava.skills.registry import SkillRegistry

    ws = _workspace()
    search = SearchConfig(tavily_api_key="dummy", exa_api_key="dummy")
    task_runner = TaskRunnerAgent(workspace_root=ws, sandbox_root=(ws / "_sandbox").resolve())
    gp = GeneralPurposeAgent(
        task_runner=task_runner,
        skill_registry=SkillRegistry(workspace_dir=ws),
        search_config=search,
    )
    kwargs: dict[str, Any] = {}
    with _fake_chat_model(), _capture_deep_agent_kwargs(kwargs):
        engine.build_cli_deepagent(
            ws,
            AnthropicSettings(api_key="sk-fake", model="claude-haiku-4-5-20251001"),
            execution_mode="local",
            local_settings=LocalSettings(),
            permission_check=True,
            search_config=search,
            subagents=[gp],
            **overrides,
        )
    return fingerprint_from_kwargs(kwargs)


def fingerprint_tinker(**overrides: Any) -> dict[str, Any]:
    from yuyutsava.core import engine
    from yuyutsava.core.config import AnthropicSettings, SearchConfig

    card_ws = _workspace() / "_fingerprint_card"
    card_ws.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, Any] = {}
    with _fake_chat_model(), _capture_deep_agent_kwargs(kwargs):
        engine.build_tinker_agent(
            "card_fingerprint",
            card_ws,
            AnthropicSettings(api_key="sk-fake", model="claude-haiku-4-5-20251001"),
            permission_check=True,
            search_config=SearchConfig(tavily_api_key="dummy", exa_api_key="dummy"),
            **overrides,
        )
    return fingerprint_from_kwargs(kwargs)


def fingerprint_orchestrator(**overrides: Any) -> dict[str, Any]:
    """Orchestrator fingerprint with a minimally-populated ``OrchestratorDeps``.

    The daemon builds these deps from a live subsystem graph; here they are
    stubbed to the fields ``build_orchestrator`` actually reads. Optional
    dependencies stay ``None``, which exercises the "nothing wired" path — the
    same shape a standalone daemon boot takes before stores are attached.
    """
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from yuyutsava.agents.orchestrator.agent import OrchestratorDeps
    from yuyutsava.core import engine

    class _Channels:
        """ChannelRouter stand-in; only used to build the ask_user tool."""

        async def ask(self, *_a: Any, **_kw: Any) -> str:
            return ""

    class _Store:
        """Events Store stand-in; only used to build the recall tool."""

        async def recall(self, *_a: Any, **_kw: Any) -> list:
            return []

    model = FakeListChatModel(responses=["ok"])
    object.__setattr__(model, "model_name", "fingerprint-fake")

    # The orchestrator's inputs are split across two surfaces, unlike the
    # CLI/tinker builders which take everything as keywords. Route each part of
    # the shared ``wired_stores()`` dict to where build_orchestrator expects it.
    budget_tokens = overrides.pop("budget_tokens", 120_000)
    skill_store = overrides.pop("skill_store", None)   # build arg, not a deps field

    # The orchestrator has NO prefs_store: it receives user preferences as a
    # build-time ``prefs_block`` string assembled by the daemon loop, rather
    # than a per-turn PrefsInjector like the CLI and tinker masters. That is a
    # real capability divergence, recorded as PrefsInjector in the matrix
    # (core/agent_profiles.py) — not an oversight here.
    overrides.pop("prefs_store", None)

    deps_fields = {f.name for f in dataclasses.fields(OrchestratorDeps)}
    unknown = set(overrides) - deps_fields
    if unknown:
        raise TypeError(f"OrchestratorDeps has no field(s): {sorted(unknown)}")

    from yuyutsava.core.config import SearchConfig

    # search_config and skill_registry must be supplied or the orchestrator binds
    # no ws_* / sk_* tools at all — its profile declares both families, and a
    # fingerprint taken without them silently under-reports what it can do.
    # (Third time an optional dependency defaulting to None hid a code path from
    # this harness; the first two were the context stores and async subagents.)
    deps = OrchestratorDeps(
        subagents={},
        subagent_model=model,
        channels=_Channels(),          # type: ignore[arg-type]
        store=_Store(),                # type: ignore[arg-type]
        subagent_token_budget=40_000,
        workspace_root=_workspace(),
        search_config=overrides.pop(
            "search_config", SearchConfig(tavily_api_key="dummy", exa_api_key="dummy")
        ),
        **overrides,
    )
    kwargs: dict[str, Any] = {}
    with _fake_chat_model(), _capture_deep_agent_kwargs(kwargs):
        from yuyutsava.skills.registry import SkillRegistry

        engine.build_orchestrator(
            model=model,
            deps=deps,
            budget_tokens=budget_tokens,
            skill_store=skill_store,
            # Without a registry the orchestrator binds no sk_* tools.
            skill_registry=SkillRegistry(workspace_dir=_workspace()),
        )
    return fingerprint_from_kwargs(kwargs)


FINGERPRINTERS = {
    "cli": fingerprint_cli,
    "orchestrator": fingerprint_orchestrator,
    "tinker": fingerprint_tinker,
}


class _StubAsyncSubagent:
    """Minimal stand-in for a background-capable subagent.

    ``_async_subagent_wiring`` only needs three things from a subagent: an opt-in
    flag, a name (for the disabled-roster filter), and a spec factory whose
    output carries ``graph_id`` + ``url`` — the dict shape deepagents routes to
    AsyncSubAgentMiddleware.
    """

    supports_async = True

    def __init__(self, name: str) -> None:
        self.name = name
        # The orchestrator's capabilities block renders these into the prompt.
        self.description = f"stub subagent {name}"

    def async_subagent_name(self) -> str:
        return f"{self.name}-async"

    def as_async_subagent_spec(self, url: str | None = None) -> dict[str, Any]:
        return {"name": self.name, "graph_id": f"graph_{self.name}", "url": url or "http://remote"}


class _StubNonAsyncSubagent(_StubAsyncSubagent):
    """Opted out of background mode; must be skipped, not failed on."""

    supports_async = False


def async_wiring(*, remote: bool = True) -> dict[str, Any]:
    """Inputs that make the background-subagent path materialise.

    Without these, ``async_subagents`` is None in every fingerprint and the whole
    async branch — spec construction, the host-URL guard, the cap middleware and
    the two interrupt middlewares — is never executed, so a refactor of it would
    pass the gate while being completely unverified.

    Includes a ``supports_async=False`` subagent so the opt-in filter is
    exercised rather than assumed.
    """
    out: dict[str, Any] = {
        "async_subagents": [_StubAsyncSubagent("bg-worker"), _StubNonAsyncSubagent("sync-only")],
        "async_host_url": "http://localhost:2024",
        "async_task_mirror": object(),   # presence is all the cap middleware needs
        "async_max_concurrent": 4,
    }
    if remote:
        out["remote_async_subagents"] = [_StubAsyncSubagent("peer-daemon")]
    return out


def all_fingerprints() -> dict[str, dict[str, Any]]:
    """Every profile in both the bare and fully-wired configurations.

    Both matter. The bare path is what a standalone CLI builds; the wired path
    is what the daemon builds, and is the only one that exercises the
    offload/compaction/budget/usage ordering rule.
    """
    import tempfile

    class _Opaque:
        pass

    # Dependencies only one profile accepts. ``note_index`` is tinker-only —
    # board-wide note recall makes no sense for a master that is not pinned to
    # a card — so it cannot live in the shared wired_stores() dict.
    profile_extras: dict[str, dict[str, Any]] = {"tinker": {"note_index": _Opaque()}}

    # All three masters delegate to subagents for any task — sync or async,
    # local or on a peer daemon. Tinker was the exception until 2026-08-08; it
    # now gets the same remote peer as its siblings.
    async_extras = {role: async_wiring() for role in FINGERPRINTERS}

    out: dict[str, dict[str, Any]] = {}
    with tempfile.TemporaryDirectory() as td:
        stores = wired_stores(Path(td))
        for role, fn in FINGERPRINTERS.items():
            out[f"{role}:bare"] = fn()
            out[f"{role}:wired"] = fn(**stores, **profile_extras.get(role, {}))
            out[f"{role}:async"] = fn(
                **stores, **profile_extras.get(role, {}), **async_extras[role]
            )
    return out


if __name__ == "__main__":
    import json

    for key, fp in all_fingerprints().items():
        print(f"### {key}")
        print(json.dumps(fp, indent=2))
        print()
