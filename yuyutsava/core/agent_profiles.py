"""The master-agent capability matrix, as data.

Three master agents are built by three ~250-line functions in
:mod:`yuyutsava.core.engine` — ``build_cli_deepagent``, ``build_orchestrator``
and ``build_tinker_agent``. They perform the same assembly steps in the same
order and differ in a handful of capability choices.

**Those choices were previously recorded nowhere.** They existed only as the
difference between three long functions, so "does the tinker master enforce the
subagent gate?" could only be answered by diffing them. This module states each
profile explicitly, which makes the matrix reviewable in a pull request and
diffable over time.

Status
------
This module is **descriptive, not yet prescriptive**. It is transcribed from the
three builders and verified against them by
``test/core/test_agent_profiles.py``; the builders do not consume it yet. That
happens when the composition pipeline lands (ADR-001), at which point these
profiles become the single source of truth and the transcription test is
replaced by the builders reading these values directly.

Do not "fix" a divergence by editing this file alone — that would make the
matrix lie. Change the builder, then update the profile (the conformance test
fails until both agree).

See docs/architecture-review/adr/ADR-001-agent-build-pipeline.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Policy(str, Enum):
    """Cross-cutting middleware attached to a master's own graph.

    Order is fixed by the pipeline, not by this list — see
    ``AgentProfile.policies`` for the ordering contract.
    """

    TOOL_FILTER = "ToolFilterPolicy"
    FILESYSTEM_PROMPT = "FilesystemPromptPolicy"
    VOICE_STYLE = "VoiceStylePolicy"
    SUBAGENT_GATE = "SubagentGatePolicy"
    BUDGET = "BudgetPolicy"
    USAGE = "UsagePolicy"
    # Phase 4: migrated to a plain policy behind LangChainPolicyAdapter. The
    # value is the name the assembled stack reports, which is now the policy's
    # rather than a middleware class's — see ADR-004 and PROGRESS finding AX.
    PERMISSION = "PermissionPolicy"


class Injector(str, Enum):
    """Per-turn retrieval injected into the prompt via RetrievalInjectionPolicy."""

    MEMORY = "MemoryInjector"
    SKILLS = "SkillInjector"
    CONVERSATION = "ConversationInjector"
    PREFS = "PrefsInjector"
    TODO_NOTES = "TodoNoteInjector"


class ToolFamily(str, Enum):
    """Tool groups bound onto the master (subagents get their own sets)."""

    CONTEXT = "ctx_*"
    MEMORY = "mem_*"
    VISUALS = "vis_*"
    ARTIFACTS = "artifact_*"
    AGENT_MEMORY = "um_*"
    TODO = "todo_*"
    SKILLS = "sk_*"
    SEARCH = "ws_*"
    TASK_RUNNER = "tr_*"
    DB = "db_*"
    MCP = "mcp"


class Requirement(str, Enum):
    """Whether a capability is always on, opt-in, or absent."""

    ALWAYS = "always"
    #: Attached only when the caller wires the backing dependency (store, flag, …).
    CONDITIONAL = "conditional"
    NEVER = "never"


@dataclass(frozen=True)
class AgentProfile:
    """One master agent's declared capabilities.

    ``policies`` records *which* policies apply, not their order. Ordering is a
    single system-wide rule enforced at assembly time:

        tool filter -> filesystem prompt -> voice/gate -> context (offload ->
        compaction -> transcript) -> budget -> usage -> permission -> retrieval

    Offload must precede compaction (it runs on the tool path, before the
    compactor counts tokens); budget must follow compaction so it sees
    post-compaction usage.
    """

    role: str
    policies: frozenset[Policy]
    injectors: frozenset[Injector]
    tools: frozenset[ToolFamily]

    #: ``todo_*`` scope: "capture" (add/list/get) or "full" (the editing set).
    todo_scope: str
    #: Namespace for the per-agent ``um_*`` behaviour store.
    agent_memory_namespace: str

    #: ``agent=`` passed to SkillInjector, deciding WHOSE skills are retrieved.
    #: ``None`` means unscoped — every agent's skills are candidates.
    skill_injector_scope: str | None = None

    permission: Requirement = Requirement.CONDITIONAL
    budget: Requirement = Requirement.CONDITIONAL
    #: Docker execution mode in addition to local.
    supports_docker: bool = False
    #: Async subagents hosted on a *different* Agent Protocol server.
    supports_remote_async: bool = False
    #: Shell backend inherits the user's environment (False = unattended).
    inherit_env: bool = True

    notes: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# The three profiles as they exist today (transcribed from core/engine.py)
# ---------------------------------------------------------------------------

CLI_PROFILE = AgentProfile(
    role="cli",
    policies=frozenset({
        Policy.TOOL_FILTER, Policy.FILESYSTEM_PROMPT, Policy.VOICE_STYLE,
        Policy.SUBAGENT_GATE, Policy.BUDGET, Policy.USAGE, Policy.PERMISSION,
    }),
    injectors=frozenset({
        Injector.MEMORY, Injector.SKILLS, Injector.CONVERSATION, Injector.PREFS,
    }),
    tools=frozenset({
        ToolFamily.CONTEXT, ToolFamily.MEMORY, ToolFamily.VISUALS,
        ToolFamily.ARTIFACTS, ToolFamily.AGENT_MEMORY, ToolFamily.TODO,
        ToolFamily.SKILLS, ToolFamily.SEARCH, ToolFamily.TASK_RUNNER,
        ToolFamily.DB, ToolFamily.MCP,
    }),
    todo_scope="capture",
    agent_memory_namespace="cli",
    skill_injector_scope="cli",
    permission=Requirement.CONDITIONAL,   # permission_check=True by default
    budget=Requirement.CONDITIONAL,       # only when the daemon passes budget_tokens
    supports_docker=True,                 # the ONLY master with a Docker mode
    supports_remote_async=True,
    inherit_env=True,
)

ORCHESTRATOR_PROFILE = AgentProfile(
    role="orchestrator",
    policies=frozenset({
        Policy.TOOL_FILTER, Policy.FILESYSTEM_PROMPT, Policy.VOICE_STYLE,
        Policy.SUBAGENT_GATE, Policy.BUDGET, Policy.USAGE,
    }),
    injectors=frozenset({
        Injector.MEMORY, Injector.SKILLS, Injector.CONVERSATION,
    }),
    tools=frozenset({
        ToolFamily.CONTEXT, ToolFamily.MEMORY, ToolFamily.ARTIFACTS,
        ToolFamily.AGENT_MEMORY, ToolFamily.TODO, ToolFamily.SKILLS,
        ToolFamily.SEARCH, ToolFamily.MCP,
    }),
    todo_scope="capture",
    agent_memory_namespace="orchestrator",
    skill_injector_scope="orchestrator",
    permission=Requirement.NEVER,
    budget=Requirement.ALWAYS,
    supports_docker=False,
    supports_remote_async=True,
    inherit_env=False,                    # unattended: does NOT inherit user env
    notes=(
        "No PermissionPolicy: the orchestrator runs unattended, so a HITL "
        "pause has no one to answer it. Consent is enforced at the tr_* tool "
        "layer instead.",
        "No tr_* family on the master itself — it delegates execution to "
        "subagents rather than running tools directly.",
    ),
)

TINKER_PROFILE = AgentProfile(
    role="tinker",
    policies=frozenset({
        Policy.TOOL_FILTER, Policy.FILESYSTEM_PROMPT, Policy.VOICE_STYLE,
        Policy.SUBAGENT_GATE, Policy.BUDGET, Policy.USAGE, Policy.PERMISSION,
    }),
    injectors=frozenset({
        Injector.MEMORY, Injector.SKILLS, Injector.CONVERSATION,
        Injector.PREFS, Injector.TODO_NOTES,
    }),
    tools=frozenset({
        ToolFamily.CONTEXT, ToolFamily.MEMORY, ToolFamily.VISUALS,
        ToolFamily.ARTIFACTS, ToolFamily.AGENT_MEMORY, ToolFamily.TODO,
        ToolFamily.SKILLS, ToolFamily.SEARCH, ToolFamily.TASK_RUNNER,
        ToolFamily.DB, ToolFamily.MCP,
    }),
    todo_scope="full",                    # the whole point: it edits the board
    agent_memory_namespace="tinker",
    skill_injector_scope="tinker",
    permission=Requirement.CONDITIONAL,
    budget=Requirement.ALWAYS,            # default 60_000
    supports_docker=False,
    supports_remote_async=True,
    inherit_env=True,
)

ALL_PROFILES: tuple[AgentProfile, ...] = (
    CLI_PROFILE, ORCHESTRATOR_PROFILE, TINKER_PROFILE,
)

PROFILES_BY_ROLE: dict[str, AgentProfile] = {p.role: p for p in ALL_PROFILES}


# ---------------------------------------------------------------------------
# Divergence reporting
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DivergenceNote:
    """Why a capability differs across masters — and whether that was a decision.

    The matrix on its own cannot tell a deliberate architectural boundary from
    drift nobody noticed. Both look like "cli=yes, orchestrator=no". That
    distinction is the whole value of the matrix, so it is recorded explicitly.
    """

    rationale: str
    #: Confirmed as intended. ADR-001 must PRESERVE these, not normalise them.
    by_design: bool = True


# Divergences that have been reviewed and confirmed intentional. Anything in
# ``divergences()`` but absent here is UNREVIEWED — nobody has yet said whether
# it is a decision or an accident. Driving that second set to zero (by deciding,
# not necessarily by changing code) is the real goal.
REVIEWED_DIVERGENCES: dict[str, DivergenceNote] = {
    "tr_*": DivergenceNote(
        "The orchestrator is a plain router: it handles events and delegates all "
        "execution to subagents, so it deliberately has no task-runner hands. "
        "Giving it tr_* would let it bypass the delegation boundary."
    ),
    "db_*": DivergenceNote(
        "Same routing boundary as tr_*. The orchestrator reads what it needs via "
        "its recall/context tools; direct DB access belongs to delegated work."
    ),
    "vis_*": DivergenceNote(
        "Same routing boundary. Visual artefacts are produced by the agent doing "
        "the work, not by the router dispatching it."
    ),
    "PermissionPolicy": DivergenceNote(
        "The orchestrator runs unattended, so a HITL interrupt would have no one "
        "to answer it and would park the task forever. Consent is enforced one "
        "level down, at the tr_* tool layer inside subagents."
    ),
    "inherit_env": DivergenceNote(
        "The orchestrator executes unattended and must not inherit the user's "
        "shell environment; the CLI and tinker masters run on the user's behalf "
        "and do."
    ),
    "TodoNoteInjector": DivergenceNote(
        "Board-note recall only makes sense for a master pinned to a card. The "
        "CLI and orchestrator have no board context to recall against."
    ),
    "supports_docker": DivergenceNote(
        "Docker execution is a CLI affordance for sandboxing the user's own "
        "work. The daemon masters run against the host workspace."
    ),
    "PrefsInjector": DivergenceNote(
        "The orchestrator receives preferences as a build-time prefs_block "
        "string assembled per task by the daemon loop, rather than a per-turn "
        "injector. A fresh orchestrator graph is built per task, so build-time "
        "injection is equivalent there — different mechanism, same effect."
    ),
}


# Divergences RESOLVED by an explicit decision on 2026-08-08. Kept as a record so
# a future reader knows these were settled deliberately, not lost in a refactor.
#
#   skill_injector_scope   -> every master now scopes to its OWN skills. The store
#                             filter is `agent IS NULL OR agent = <role>`, so each
#                             agent keeps shared skills and no longer pulls skills
#                             another agent authored for itself.
#   supports_remote_async  -> tinker may delegate to subagents for any task, sync
#                             or async, local or on a peer daemon.
#   FILESYSTEM_PROMPT      -> the orchestrator now strips the deepagents filesystem
#                             block too. It has no filesystem tools at all, so the
#                             block advertised nothing and cost ~700 cache-prefix
#                             tokens per task.
#   SUBAGENT_GATE          -> tinker now honours the runtime subagent toggle. Its
#                             bundle is cached per card and can outlive a toggle
#                             change, so without the gate a disabled subagent stayed
#                             reachable from the board.



def unreviewed_divergences() -> dict[str, dict[str, bool]]:
    """Divergences nobody has confirmed as intentional.

    This — not the raw count — is the number worth driving down. A divergence
    with a recorded rationale is an architectural decision; one without is an
    unanswered question, and ADR-001 would otherwise normalise it away silently.
    """
    return {
        cap: row
        for cap, row in divergences().items()
        if not (cap in REVIEWED_DIVERGENCES and REVIEWED_DIVERGENCES[cap].by_design)
    }


def divergences() -> dict[str, dict[str, bool]]:
    """Capabilities that are NOT uniform across the masters.

    The point of this function is that the answer is currently long. Each row is
    a capability some masters have and others do not — every one of which is a
    decision no one recorded, and which a fourth master agent would have to
    re-derive by reading three functions.

    Returns ``{capability: {role: present}}``, only for non-uniform capabilities.
    """
    out: dict[str, dict[str, bool]] = {}

    for enum_cls, attr in ((Policy, "policies"), (Injector, "injectors"), (ToolFamily, "tools")):
        for member in enum_cls:
            row = {p.role: member in getattr(p, attr) for p in ALL_PROFILES}
            if len(set(row.values())) > 1:
                out[member.value] = row

    for flag in ("supports_docker", "supports_remote_async", "inherit_env"):
        row = {p.role: bool(getattr(p, flag)) for p in ALL_PROFILES}
        if len(set(row.values())) > 1:
            out[flag] = row

    # Scope is not a boolean: "unscoped vs scoped-to-self" is the divergence.
    scopes = {p.role: p.skill_injector_scope for p in ALL_PROFILES}
    if len({s is None for s in scopes.values()}) > 1:
        out["skill_injector_scope"] = {r: s is not None for r, s in scopes.items()}

    return out


def format_matrix() -> str:
    """The full matrix as a text table — for docs, review, and eyeballing."""
    rows: list[tuple[str, dict[str, str]]] = []
    for enum_cls, attr in ((Policy, "policies"), (Injector, "injectors"), (ToolFamily, "tools")):
        for member in enum_cls:
            rows.append((
                member.value,
                {p.role: ("yes" if member in getattr(p, attr) else "-") for p in ALL_PROFILES},
            ))
    for flag in ("supports_docker", "supports_remote_async", "inherit_env"):
        rows.append((flag, {p.role: ("yes" if getattr(p, flag) else "-") for p in ALL_PROFILES}))
    for flag in ("permission", "budget"):
        rows.append((flag, {p.role: getattr(p, flag).value for p in ALL_PROFILES}))
    for flag in ("todo_scope", "agent_memory_namespace"):
        rows.append((flag, {p.role: str(getattr(p, flag)) for p in ALL_PROFILES}))

    roles = [p.role for p in ALL_PROFILES]
    width = max(len(label) for label, _ in rows) + 2
    lines = ["".ljust(width) + "".join(r.ljust(15) for r in roles)]
    lines.append("-" * (width + 15 * len(roles)))
    for label, cells in rows:
        lines.append(label.ljust(width) + "".join(cells[r].ljust(15) for r in roles))
    return "\n".join(lines)


if __name__ == "__main__":
    print(format_matrix())
    all_div, unreviewed = divergences(), unreviewed_divergences()
    print(
        f"\n{len(all_div)} capabilities differ across the three masters — "
        f"{len(all_div) - len(unreviewed)} by design, {len(unreviewed)} unreviewed:\n"
    )
    for cap, row in all_div.items():
        note = REVIEWED_DIVERGENCES.get(cap)
        mark = "  by-design" if (note and note.by_design) else "UNREVIEWED"
        lack = ",".join(r for r, v in row.items() if not v)
        print(f"  [{mark}] {cap:34} lacks={lack}")
    if unreviewed:
        print("\nUnreviewed divergences need a decision, not necessarily a change:")
        for cap in unreviewed:
            note = REVIEWED_DIVERGENCES.get(cap)
            print(f"\n  {cap}\n    {note.rationale if note else 'no rationale recorded'}")
