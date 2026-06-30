"""Named UI sounds ("earcons") for the daemon-side sound layer.

An *earcon* is a short, recognisable sound that marks a UI event — the overlay
opening, the agent finishing, an error. They are referenced by **name**, never
by hard-coded path, so they can be overridden: drop a ``{name}.wav`` into the
earcons directory (``$YUYUTSAVA_EARCONS_DIR`` or the bundled ``assets/earcons``)
and it wins.

Defaults are **synthesized from stdlib** (simple tone sequences with click-free
envelopes) so the repo ships no binary blobs and the sounds always exist even on
a fresh checkout. :func:`generate_default_earcons` writes the bundled assets;
:func:`ensure_earcons` lazily materialises any that are missing at runtime.

Resolution order for :func:`earcon_path`:

1. ``$YUYUTSAVA_EARCONS_DIR/{name}.wav`` (user override), if present
2. bundled ``assets/earcons/{name}.wav``, if present
3. cache ``~/.yuyutsava/earcons/{name}.wav`` — synthesized on demand
"""

from __future__ import annotations

import math
import os
import struct
import wave
from pathlib import Path

# Match the capture/playback sample rate used elsewhere (io/audio.SAMPLE_RATE).
_SAMPLE_RATE = 16_000
_AMPLITUDE = 0.35  # headroom so earcons aren't jarringly loud

# Canonical earcon vocabulary. The renderer mirrors these names.
EARCON_NAMES: tuple[str, ...] = ("open", "close", "listening", "done", "error")

# Default tone designs: each earcon is a sequence of (frequency_hz, seconds).
# Rising = "opening/positive", falling = "closing", low/long = "error".
_DEFAULT_TONES: dict[str, list[tuple[float, float]]] = {
    "open": [(660.0, 0.09), (988.0, 0.12)],          # ascending two-tone
    "close": [(988.0, 0.09), (660.0, 0.12)],         # descending two-tone
    "listening": [(880.0, 0.10)],                     # single soft blip
    "done": [(660.0, 0.08), (880.0, 0.08), (1175.0, 0.13)],  # pleasant rising triad
    "error": [(220.0, 0.16), (180.0, 0.20)],          # low, downward buzz
}


def _bundled_dir() -> Path:
    """Repo-bundled assets dir: ``<repo>/assets/earcons``."""
    # earcons.py -> audio_io -> yuyutsava -> <repo root>
    return Path(__file__).resolve().parents[2] / "assets" / "earcons"


def _override_dir() -> Path | None:
    raw = os.environ.get("YUYUTSAVA_EARCONS_DIR", "").strip()
    return Path(raw).expanduser() if raw else None


def _cache_dir() -> Path:
    return Path.home() / ".yuyutsava" / "earcons"


def _synthesize_wav(tones: list[tuple[float, float]], out_path: Path) -> None:
    """Render a tone sequence to a 16-bit mono WAV with a short fade envelope."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    frames = bytearray()
    fade = int(_SAMPLE_RATE * 0.008)  # ~8ms attack/decay to avoid clicks
    for freq, dur in tones:
        n = int(_SAMPLE_RATE * dur)
        for i in range(n):
            env = 1.0
            if i < fade:
                env = i / fade
            elif i > n - fade:
                env = max(0.0, (n - i) / fade)
            sample = _AMPLITUDE * env * math.sin(2.0 * math.pi * freq * (i / _SAMPLE_RATE))
            frames += struct.pack("<h", int(sample * 32767))
    with wave.open(str(out_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(_SAMPLE_RATE)
        wf.writeframes(bytes(frames))


def generate_default_earcons(dest: Path | None = None) -> Path:
    """Write the default earcon WAVs into ``dest`` (default: bundled assets).

    Idempotent: regenerates every default earcon. Returns the directory written.
    Used to seed the committed ``assets/earcons`` and as the runtime fallback.
    """
    target = dest or _bundled_dir()
    target.mkdir(parents=True, exist_ok=True)
    for name, tones in _DEFAULT_TONES.items():
        _synthesize_wav(tones, target / f"{name}.wav")
    return target


def earcon_path(name: str) -> Path:
    """Resolve ``name`` to a playable WAV path, synthesizing a default if needed.

    Raises ``KeyError`` for an unknown earcon name.
    """
    if name not in EARCON_NAMES:
        raise KeyError(f"unknown earcon {name!r}; known: {EARCON_NAMES}")

    override = _override_dir()
    if override is not None:
        p = override / f"{name}.wav"
        if p.exists():
            return p

    bundled = _bundled_dir() / f"{name}.wav"
    if bundled.exists():
        return bundled

    # Last resort: synthesize the default into the user cache dir.
    cached = _cache_dir() / f"{name}.wav"
    if not cached.exists():
        _synthesize_wav(_DEFAULT_TONES[name], cached)
    return cached


def ensure_earcons() -> None:
    """Materialise any missing earcons so later playback never blocks on synth."""
    for name in EARCON_NAMES:
        earcon_path(name)
