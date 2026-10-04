"""deepagents' harness, as this system builds it: without deepagents' own summarizer.

``create_deep_agent`` puts its ``SummarizationMiddleware`` on every graph it
builds — the master, the general-purpose subagent and each declared subagent.
That summarizer decides from the model a graph was **built** with: it fires at
0.85 of that model's window, and on a provider's overflow error it summarizes
with that same model. A conversation that switches model (``llm/switch``) would
then summarize too early on a larger window, or call a provider the user has
since removed.

This system's own compactor (:class:`~yuyutsava.context.compaction.YuyutsavaCompactionMiddleware`)
already runs first (0.7 of the window), steers under the model each turn runs
on, and carries the overflow fallback, so it owns summarization (design rule 12,
owner's decision 2026-10-02, revisitable). What goes with deepagents' copy: its
``/conversation_history`` offload file — the transcript store and the
``ctx_history*`` readers cover evicted turns — and its clipping of
``write_file``/``edit_file`` arguments.

deepagents excludes middleware through a **harness profile**, looked up by the
provider of the model a graph is built on. :func:`exclude_deepagents_summarizer`
registers the exclusion under that provider before each build;
``test/framework_contract/test_summarizer_exclusion_contract.py`` proves it
takes effect for every provider type this system builds.

## The prompt suffix follows the model

A profile can also carry a ``system_prompt_suffix`` — deepagents ships them for
some Claude and OpenAI Codex models — which deepagents bakes into the system
prompt from the build model. :func:`retarget_prompt` swaps it for the suffix of
the model a turn actually runs on (design rule 13); the model switch calls it
(``llm/switch``).
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from deepagents import HarnessProfile, register_harness_profile

# The functions deepagents itself uses to find a pre-built model's profile, so the
# key registered here is the key it looks up, and the suffix read is the one it
# applied (pinned by test/framework_contract/test_summarizer_exclusion_contract.py).
from deepagents._models import get_model_provider
from deepagents.profiles.harness.harness_profiles import _harness_profile_for_model
from langchain_core.messages import SystemMessage

#: deepagents' summarizer, by the public name its middleware reports. A name
#: rather than the class: the class is private, and the name cannot match this
#: system's compactor, which reports its own.
_SUMMARIZER = "SummarizationMiddleware"

logger = logging.getLogger("yuyutsava.core.harness")

#: Registration merges into deepagents' registry; one at a time.
_lock = threading.Lock()


def exclude_deepagents_summarizer(model: Any) -> None:
    """Make every deepagents graph built on *model*'s provider leave out its summarizer.

    Merged by deepagents into whatever that provider already has, and into any
    model-level profile it ships (Claude's, for one); registering again changes
    nothing, so each build simply registers. A model whose provider cannot be told —
    a test double — keeps the summarizer, and says so.
    """
    provider = get_model_provider(model)
    if not provider:
        logger.warning(
            "cannot tell which provider %s belongs to; its deepagents graph keeps "
            "deepagents' summarizer",
            type(model).__name__,
        )
        return
    with _lock:
        register_harness_profile(provider, HarnessProfile(excluded_middleware={_SUMMARIZER}))


def prompt_suffix(model: Any) -> str:
    """The ``system_prompt_suffix`` deepagents adds to prompts for *model*; ``""`` for none."""
    return _harness_profile_for_model(model, None).system_prompt_suffix or ""


def retarget_prompt(system_message: Any, built: Any, model: Any) -> Any:
    """*system_message* as written for *model* rather than *built*, the graph's own.

    Removes *built*'s suffix where deepagents put it (after a blank line) and
    appends *model*'s at the end — where in the prompt it sits does not change
    what it asks. Unchanged when both models share a suffix, or have none.
    """
    old, new = prompt_suffix(built), prompt_suffix(model)
    if old == new or system_message is None:
        return system_message
    blocks: list[Any] = []
    removed = not old
    for block in system_message.content_blocks:
        if not removed and block.get("type") == "text" and f"\n\n{old}" in block["text"]:
            block = {**block, "text": block["text"].replace(f"\n\n{old}", "", 1)}
            removed = True
        blocks.append(block)
    if new:
        blocks.append({"type": "text", "text": f"\n\n{new}"})
    return SystemMessage(content_blocks=blocks)


__all__ = ["exclude_deepagents_summarizer", "prompt_suffix", "retarget_prompt"]
