"""The events configuration: which event sources run, stored in ``events_config.json``."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from yuyutsava.storage.paths import events_config_path

# ---------------------------------------------------------------------------
# Events config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceConfig:
    """One entry from ``events_config.json`` ``sources`` map."""

    name: str
    enabled: bool = True
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EventsConfig:
    """Loaded ``events_config.json``."""

    sources: dict[str, SourceConfig]

    @classmethod
    def from_file(cls, path: Path | None = None) -> EventsConfig:
        path = path or events_config_path()
        if not path.exists():
            return cls.default()
        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc
        sources_raw = raw.get("sources", {}) or {}
        sources: dict[str, SourceConfig] = {}
        for name, body in sources_raw.items():
            if not isinstance(body, dict):
                continue
            enabled = bool(body.get("enabled", True))
            params = {k: v for k, v in body.items() if k != "enabled"}
            sources[name] = SourceConfig(name=name, enabled=enabled, params=params)
        return cls(sources=sources) if sources else cls.default()

    @classmethod
    def default(cls) -> EventsConfig:
        """Sensible default if no config file exists: watch ``~/Downloads``."""
        return cls(
            sources={
                "fs": SourceConfig(
                    name="fs",
                    enabled=True,
                    params={
                        "roots": [str(Path.home() / "Downloads")],
                        "ignore": ["*.tmp", ".DS_Store", "*.crdownload", "*.part"],
                        "coalesce_window_ms": 750,
                    },
                ),
            }
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"sources": {}}
        for name, src in self.sources.items():
            body: dict[str, Any] = {"enabled": src.enabled}
            body.update(src.params)
            out["sources"][name] = body
        return out

    def to_file(self, path: Path | None = None) -> Path:
        """Persist this config to events_config.json atomically."""
        path = path or events_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True))
        tmp.replace(path)
        return path

    def with_fs_root_added(self, root: str | Path) -> EventsConfig:
        """Return a new EventsConfig with ``root`` added to the fs source's roots."""
        root_str = str(Path(str(root)).expanduser())
        fs = self.sources.get("fs")
        if fs is None:
            new_fs = SourceConfig(
                name="fs",
                enabled=True,
                params={
                    "roots": [root_str],
                    "ignore": ["*.tmp", ".DS_Store", "*.crdownload", "*.part"],
                    "coalesce_window_ms": 750,
                },
            )
        else:
            roots = list(fs.params.get("roots") or [])
            if root_str not in roots:
                roots.append(root_str)
            params = {**fs.params, "roots": roots}
            new_fs = SourceConfig(name=fs.name, enabled=fs.enabled, params=params)
        return EventsConfig(sources={**self.sources, "fs": new_fs})

    def with_fs_root_removed(self, root: str | Path) -> EventsConfig:
        root_str = str(Path(str(root)).expanduser())
        fs = self.sources.get("fs")
        if fs is None:
            return self
        roots = [r for r in (fs.params.get("roots") or []) if r != root_str]
        params = {**fs.params, "roots": roots}
        new_fs = SourceConfig(name=fs.name, enabled=fs.enabled, params=params)
        return EventsConfig(sources={**self.sources, "fs": new_fs})
