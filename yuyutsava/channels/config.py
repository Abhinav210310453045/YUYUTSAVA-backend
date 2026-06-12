"""Channel plugin configuration — ``~/.yuyutsava/channels_config.json``.

Same shape family as ``EventsConfig`` (the SourceRegistry config this
package's registry is modeled on)::

    {"channels": {"telegram": {"enabled": true, "poll_timeout_sec": 50}}}

Secrets never live here — the Telegram bot token is env-only
(``YUYUTSAVA_TELEGRAM_BOT_TOKEN``); params are tuning knobs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from yuyutsava.storage.paths import channels_config_path


@dataclass(frozen=True)
class ChannelConfig:
    """One channel's block in channels_config.json."""

    name: str
    enabled: bool = False
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChannelsConfig:
    """Loaded ``channels_config.json``."""

    channels: dict[str, ChannelConfig]

    @classmethod
    def from_file(cls, path: Path | None = None) -> "ChannelsConfig":
        path = path or channels_config_path()
        if not path.exists():
            return cls.default()
        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc
        channels_raw = raw.get("channels", {}) or {}
        channels: dict[str, ChannelConfig] = {}
        for name, body in channels_raw.items():
            if not isinstance(body, dict):
                continue
            enabled = bool(body.get("enabled", False))
            params = {k: v for k, v in body.items() if k != "enabled"}
            channels[name] = ChannelConfig(name=name, enabled=enabled, params=params)
        return cls(channels=channels)

    @classmethod
    def default(cls) -> "ChannelsConfig":
        """No file yet: every known plugin present but disabled."""
        return cls(channels={
            "telegram": ChannelConfig(name="telegram", enabled=False, params={}),
        })

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"channels": {}}
        for name, ch in self.channels.items():
            body: dict[str, Any] = {"enabled": ch.enabled}
            body.update(ch.params)
            out["channels"][name] = body
        return out

    def to_file(self, path: Path | None = None) -> Path:
        """Persist atomically (write-temp-then-replace, like EventsConfig)."""
        path = path or channels_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True))
        tmp.replace(path)
        return path

    def with_enabled(self, name: str, enabled: bool) -> "ChannelsConfig":
        """Copy with one channel's enabled flag flipped (params preserved)."""
        existing = self.channels.get(name, ChannelConfig(name=name))
        return ChannelsConfig(channels={
            **self.channels,
            name: ChannelConfig(name=name, enabled=enabled, params=dict(existing.params)),
        })
