"""Text-to-speech backends.

Default: ``piper`` CLI (fully local, privacy-first).
Cloud opt-in: ElevenLabs API (set ``TTS_PROVIDER=elevenlabs``).

Usage::

    tts = tts_from_env()
    await tts.synthesize("Hello!", Path("/tmp/hello.wav"))
    await play_wav(Path("/tmp/hello.wav"))
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path

logger = logging.getLogger("yuyutsava.io.tts")


class TTS(ABC):
    """Text-to-speech ABC. Synthesizes ``text`` into an audio file at ``output_path``."""

    @abstractmethod
    async def synthesize(self, text: str, output_path: Path) -> None: ...


class PiperTTS(TTS):
    """Local TTS via ``piper`` CLI binary.

    Piper writes a WAV file to ``--output_file``. Install: https://github.com/rhasspy/piper.
    Point ``PIPER_MODEL`` at the downloaded ``.onnx`` model file.
    """

    def __init__(self, model: str, model_config: str | None = None) -> None:
        self._model = model
        self._model_config = model_config

    async def synthesize(self, text: str, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["piper", "--model", self._model, "--output_file", str(output_path)]
        if self._model_config:
            cmd += ["--config", self._model_config]

        loop = asyncio.get_running_loop()

        def _run() -> None:
            proc = subprocess.run(
                cmd,
                input=text.encode("utf-8"),
                capture_output=True,
                timeout=30,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"piper exited {proc.returncode}: "
                    f"{proc.stderr.decode('utf-8', 'replace')[:200]}"
                )

        await loop.run_in_executor(None, _run)

    @classmethod
    def from_env(cls) -> PiperTTS:
        model = os.environ.get("PIPER_MODEL", "").strip()
        if not model:
            raise RuntimeError(
                "Set PIPER_MODEL to the path of a .onnx piper model file. "
                "Download models from https://github.com/rhasspy/piper/blob/master/VOICES.md"
            )
        config = os.environ.get("PIPER_MODEL_CONFIG", "").strip() or None
        return cls(model=model, model_config=config)


class ElevenLabsTTS(TTS):
    """Cloud TTS via ElevenLabs API. Output is MP3; requires ``ELEVENLABS_API_KEY``.

    To play the output use ``play_audio`` (needs soundfile) rather than ``play_wav``.
    """

    def __init__(self, api_key: str, voice_id: str = "21m00Tcm4TlvDq8ikWAM") -> None:
        self._api_key = api_key
        self._voice_id = voice_id

    async def synthesize(self, text: str, output_path: Path) -> None:
        try:
            import httpx
        except ImportError as exc:
            raise RuntimeError("httpx not installed") from exc

        output_path.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{self._voice_id}"
        headers = {
            "xi-api-key": self._api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        }
        body = {
            "text": text,
            "model_id": "eleven_monolingual_v1",
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.5},
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=body, headers=headers)
            resp.raise_for_status()
        # Save as .mp3 regardless of output_path suffix; caller uses play_audio.
        mp3_path = output_path.with_suffix(".mp3")
        mp3_path.write_bytes(resp.content)
        # Overwrite output_path with bytes so callers using output_path work too.
        if mp3_path != output_path:
            output_path.write_bytes(resp.content)

    @classmethod
    def from_env(cls) -> ElevenLabsTTS:
        key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
        if not key:
            raise RuntimeError("Set ELEVENLABS_API_KEY for ElevenLabs TTS")
        voice_id = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM").strip()
        return cls(api_key=key, voice_id=voice_id)


def tts_from_env() -> TTS:
    """Build a TTS instance from the environment. Default: ``piper`` (local)."""
    provider = os.environ.get("TTS_PROVIDER", "piper").strip().lower()
    if provider == "elevenlabs":
        return ElevenLabsTTS.from_env()
    return PiperTTS.from_env()
