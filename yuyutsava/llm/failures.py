"""What to tell a person when a model call fails for a reason they can act on.

The CLI's failed-turn line and the app's error event say the same thing, from
here (design rule 15):

* **No quota for the model.** Vertex answers a model the project has no quota
  for — every Claude model on a new project — with the same 429
  ``RESOURCE_EXHAUSTED`` a busy minute gets, so the retry ladder treats it as
  busy and the turn ends saying "provider busy". It is not busy, and resending
  will never work: the quota needs raising. The per-base-model quota and its
  "submit a quota increase request" wording are what separate it.
* **Provider busy.** A capacity error the retry ladder gave up on; resending
  later can work.
* **Too long even summarized.** Compaction retried once and the prompt still
  did not fit; this model's window is too small for the conversation.
* **No such model here.** The provider does not serve the model where it was
  asked for it — on Vertex, usually a model served at ``global`` but not in the
  provider's own location.

Anything else is ``None``: the caller shows the error itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from yuyutsava.llm.overflow import is_context_overflow
from yuyutsava.llm.quirks.first_chunk_retry import classify_provider_busy, is_provider_busy

#: Vertex's quota refusal: a per-base-model quota, or its request to raise one.
_QUOTA = re.compile(r"per_base_model|submit a quota increase request", re.IGNORECASE)
#: "... with base model: anthropic-claude-haiku-4-5." — the colon is required: the
#: quota's own name ("..._per_base_model with ...") says "base_model" too.
_BASE_MODEL = re.compile(r"base model:\s*([A-Za-z0-9._@\-]*[A-Za-z0-9])", re.IGNORECASE)
_PROJECT = re.compile(r"projects/([a-z][a-z0-9\-]{4,61}[a-z0-9])")
#: A provider saying it has no such model (Vertex/genai, OpenAI-compatible, Anthropic, Bedrock).
_NOT_FOUND = re.compile(
    r"NOT_FOUND|model_not_found|was not found|does not exist|not_found_error"
    r"|ResourceNotFoundException|model .* not found",
    re.IGNORECASE,
)
_MODEL_NAMED = re.compile(r"models?/([A-Za-z0-9._@:\-]+)|model '([^']+)'")


@dataclass(frozen=True)
class QuotaRefusal:
    """A call refused because the project has no quota (left) for the model."""

    #: Vertex's name for the model, e.g. ``anthropic-claude-haiku-4-5``; ``""`` unknown.
    base_model: str = ""
    #: The Google Cloud project, when the error names it.
    project: str = ""

    @property
    def link(self) -> str:
        """Where the quota is raised."""
        query = f"?project={self.project}" if self.project else ""
        return f"https://console.cloud.google.com/iam-admin/quotas{query}"

    def describe(self) -> str:
        model = self.base_model or "this model"
        return (
            f"no quota for {model} on Vertex — request an increase "
            f"(per-base-model online prediction quota) at {self.link}, or switch model; "
            "resending will not help until it is raised"
        )


def quota_refusal(exc: BaseException) -> QuotaRefusal | None:
    """The quota refusal *exc* is, or ``None``."""
    text = str(exc)
    if not _QUOTA.search(text):
        return None
    base = _BASE_MODEL.search(text)
    project = _PROJECT.search(text)
    return QuotaRefusal(
        base_model=base.group(1) if base else "",
        project=project.group(1) if project else "",
    )


def describe_model_failure(exc: BaseException) -> str | None:
    """One line saying why a model call failed and what to do, or ``None`` for anything else."""
    if (quota := quota_refusal(exc)) is not None:
        return quota.describe()
    if is_provider_busy(exc):
        return (
            f"provider busy ({classify_provider_busy(exc)}) — retries gave up; resend to try "
            "again (VERTEX_RETRY_BUDGET_SEC, default 90, and VERTEX_MAX_RETRIES control how "
            "long it keeps trying)"
        )
    text = str(exc)
    if _NOT_FOUND.search(text) and "model" in text.lower() and not is_provider_busy(exc):
        named = _MODEL_NAMED.search(text)
        model = next((g for g in named.groups() if g), "") if named else ""
        return (
            f"the provider does not serve {model or 'this model'} where it was asked for — "
            "`yuyutsava models discover` shows what is served and where; on Vertex, give it an "
            "alias with the location that serves it: "
            "`yuyutsava models alias <name> <provider>/<model> --location global`"
        )
    if is_context_overflow(exc):
        return (
            "the conversation does not fit this model's window even summarized — start a new "
            "chat, or switch to a model with a larger window"
        )
    return None


__all__ = ["QuotaRefusal", "describe_model_failure", "quota_refusal"]
