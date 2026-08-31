"""Speak like a person when the turn came in by voice.

Phase 4 step 4.6, seventh migration (was ``VoiceStyleMiddleware``).

A voice turn and a typed turn share one agent and one system prompt. This
appends a spoken-style addendum on voice turns only, so the text path is
completely unaffected — the addendum never enters the prompt it would otherwise
be cached with.
"""

from __future__ import annotations

from yuyutsava.policy.base import Policy
from yuyutsava.policy.types import ModelCall

VOICE_STYLE_ADDENDUM = (
    "\n\n## Speaking aloud (voice turn)\n"
    "Your reply will be spoken by a text-to-speech voice, so talk like a person "
    "on a call — do not read out a document. Follow these rules for THIS reply:\n"
    "- Keep it short: usually one to three sentences. Answer first, then stop.\n"
    "- Use plain spoken prose. No markdown, no bullet or numbered lists, no "
    "headings, no code fences, no tables, no emoji.\n"
    "- Never spell out long IDs, hashes, UUIDs, URLs or file paths character by "
    "character. Refer to them by name (e.g. \"the background task\") or read only "
    "the last few characters if the user truly needs to distinguish them.\n"
    "- Prefer natural phrasing and contractions (\"I've\", \"it's\", \"you're\").\n"
    "- If the full answer is long or has many parts, give the key point in one "
    "breath and offer to go deeper (\"want me to walk through the rest?\") instead "
    "of dumping everything at once.\n"
    "This styling applies only to what you say back to the user; it does not "
    "change which tools you call or how you do the work."
)


def is_voice_turn() -> bool:
    """Whether the active run was started by the voice surface.

    Reads the LangGraph ``RunnableConfig`` the same way the context middleware
    do. Defensive: outside a graph run, or if the shape changes, degrade to
    not-voice (a no-op) rather than raising — styling never breaks a turn.
    """
    try:
        from langgraph.config import get_config

        cfg = get_config() or {}
        conf = cfg.get("configurable", {}) or {}
        return conf.get("modality") == "voice"
    except Exception:  # noqa: BLE001 — styling never breaks a turn
        return False


class VoiceStylePolicy(Policy):
    """Append a spoken-style addendum to the system prompt on voice turns only."""

    name = "VoiceStylePolicy"

    def __init__(self, addendum: str = VOICE_STYLE_ADDENDUM) -> None:
        super().__init__()
        self._addendum = addendum

    async def revise_model_call(self, call: ModelCall) -> None:
        if is_voice_turn():
            call.append_system_text(self._addendum)


__all__ = ["VOICE_STYLE_ADDENDUM", "VoiceStylePolicy", "is_voice_turn"]
