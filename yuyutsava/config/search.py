"""Web search provider settings, from the environment."""

from __future__ import annotations

from dataclasses import dataclass

from yuyutsava.config.env import env

# ---------------------------------------------------------------------------
# Search provider config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SearchConfig:
    """API keys for external web search providers (Tavily, Exa).

    Missing keys are not an error — the corresponding ws_* tools are simply
    absent from make_search_tools() output. No provider configured = no tools.
    """

    tavily_api_key: str = ""
    exa_api_key: str = ""

    @classmethod
    def from_env(cls) -> SearchConfig:
        """Build from env; missing keys leave the provider unavailable."""
        return cls(
            tavily_api_key=env("TAVILY_API_KEY"),
            exa_api_key=env("EXA_API_KEY"),
        )

    def is_available(self) -> dict[str, bool]:
        """Return which providers have a configured API key."""
        return {
            "tavily": bool(self.tavily_api_key),
            "exa": bool(self.exa_api_key),
        }
