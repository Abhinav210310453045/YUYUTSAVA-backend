// Renderer-side sound layer (Web Audio) — the client mirror of the daemon's
// yuyutsava/audio_io package. Because the daemon can be remote (Tailscale),
// playback for the Electron/mobile UI happens here, on the user's device.
//
// Two primitives, matching the server:
//   * earcons   — short named UI sounds, synthesized with oscillators so we ship
//                 no audio assets and they always exist (same vocabulary +
//                 tone designs as yuyutsava/audio_io/earcons.py).
//   * PCM queue — streamed TTS audio (Phase 4) played gaplessly back-to-back,
//                 with stop() for barge-in.
//
// This module is agent-agnostic: the chat playback, the Voice panel, and the
// mic overlay all reuse the same AudioPlayer.

// Earcon tone sequences: [frequencyHz, seconds][]. Mirrors the server defaults.
const EARCON_TONES = {
  open: [[660, 0.09], [988, 0.12]],
  close: [[988, 0.09], [660, 0.12]],
  listening: [[880, 0.10]],
  done: [[660, 0.08], [880, 0.08], [1175, 0.13]],
  error: [[220, 0.16], [180, 0.20]],
}

export const EARCON_NAMES = Object.keys(EARCON_TONES)

const PEAK_GAIN = 0.22 // headroom so earcons aren't jarring
const FADE_SEC = 0.008 // click-free attack/decay

export class AudioPlayer {
  constructor() {
    this._ctx = null
    // Scheduling cursor for the gapless PCM queue (audiocontext time).
    this._pcmCursor = 0
    // Live nodes we may need to stop() for barge-in.
    this._active = new Set()
    // Shared analyser tapping the output — feeds the Voice orb/waveform while
    // the agent is speaking. Sources connect through it to the destination.
    this._analyser = null
  }

  // Lazily create + resume the context. Browsers block audio until a user
  // gesture; call this from a click/keypress handler before playback.
  async ensureContext() {
    if (!this._ctx) {
      const Ctx = window.AudioContext || window.webkitAudioContext
      this._ctx = new Ctx()
    }
    if (!this._analyser) {
      this._analyser = this._ctx.createAnalyser()
      this._analyser.fftSize = 256
      this._analyser.smoothingTimeConstant = 0.8
      this._analyser.connect(this._ctx.destination)
    }
    if (this._ctx.state === 'suspended') {
      try { await this._ctx.resume() } catch { /* ignore */ }
    }
    return this._ctx
  }

  // AnalyserNode for the agent's TTS output, or null before first playback.
  // Consumers (VoiceOrb) call getByteFrequencyData/getByteTimeDomainData on it.
  getAnalyser() { return this._analyser }

  // Where playback sources should connect: through the analyser when present so
  // the visualizer sees the audio, else straight to the speakers.
  _out() { return this._analyser || this._ctx.destination }

  // Play a named earcon. Unknown names are a no-op (logged).
  async playEarcon(name) {
    const tones = EARCON_TONES[name]
    if (!tones) { console.warn(`unknown earcon ${name}`); return }
    const ctx = await this.ensureContext()
    let t = ctx.currentTime
    for (const [freq, dur] of tones) {
      const osc = ctx.createOscillator()
      const gain = ctx.createGain()
      osc.type = 'sine'
      osc.frequency.value = freq
      // Envelope: ramp up, hold, ramp down — avoids clicks.
      gain.gain.setValueAtTime(0, t)
      gain.gain.linearRampToValueAtTime(PEAK_GAIN, t + FADE_SEC)
      gain.gain.setValueAtTime(PEAK_GAIN, t + dur - FADE_SEC)
      gain.gain.linearRampToValueAtTime(0, t + dur)
      osc.connect(gain).connect(ctx.destination)
      osc.start(t)
      osc.stop(t + dur)
      this._track(osc)
      t += dur
    }
  }

  // Queue a chunk of streamed PCM for gapless playback (Phase 4 TTS out).
  // `pcm` is an Int16Array (or Float32Array in [-1,1]); `sampleRate` in Hz.
  async enqueuePcm(pcm, sampleRate = 22050) {
    if (!pcm || pcm.length === 0) return
    const ctx = await this.ensureContext()
    const float = pcm instanceof Float32Array ? pcm : int16ToFloat32(pcm)
    const buffer = ctx.createBuffer(1, float.length, sampleRate)
    buffer.getChannelData(0).set(float)

    const src = ctx.createBufferSource()
    src.buffer = buffer
    src.connect(this._out())

    // Schedule after whatever is already queued (gapless), but never in the past.
    const startAt = Math.max(ctx.currentTime, this._pcmCursor)
    src.start(startAt)
    this._pcmCursor = startAt + buffer.duration
    this._track(src)
  }

  // Replay a persisted clip served as a WAV URL (Phase 6b cross-restart replay).
  // Fetches + decodes the file, then plays it through the same gapless cursor so
  // stop() (barge-in) tears it down like any other audio.
  async playUrl(url) {
    if (!url) return
    const ctx = await this.ensureContext()
    const resp = await fetch(url)
    if (!resp.ok) throw new Error(`audio ${resp.status}`)
    const buffer = await ctx.decodeAudioData(await resp.arrayBuffer())
    const src = ctx.createBufferSource()
    src.buffer = buffer
    src.connect(this._out())
    const startAt = Math.max(ctx.currentTime, this._pcmCursor)
    src.start(startAt)
    this._pcmCursor = startAt + buffer.duration
    this._track(src)
  }

  // True while queued TTS audio is still scheduled to play. The daemon finishes
  // synthesizing a whole reply long before the client finishes *playing* it, so
  // only the client (via this scheduling cursor) knows when the voice reply is
  // truly done — used to mute the mic and ignore interrupt-y events until then.
  isPlaying() {
    return !!this._ctx && this._pcmCursor > this._ctx.currentTime + 0.05
  }

  // Seconds of audio still queued ahead of the playback cursor (0 when idle).
  secondsRemaining() {
    if (!this._ctx) return 0
    return Math.max(0, this._pcmCursor - this._ctx.currentTime)
  }

  // Barge-in: stop everything currently playing/queued and reset the cursor.
  stop() {
    for (const node of this._active) {
      try { node.stop() } catch { /* already stopped */ }
    }
    this._active.clear()
    this._pcmCursor = this._ctx ? this._ctx.currentTime : 0
  }

  _track(node) {
    this._active.add(node)
    node.addEventListener?.('ended', () => this._active.delete(node))
    node.onended = () => this._active.delete(node)
  }
}

function int16ToFloat32(int16) {
  const out = new Float32Array(int16.length)
  for (let i = 0; i < int16.length; i++) out[i] = int16[i] / 32768
  return out
}

// PCM <-> base64 for the WS audio frames (binary-safe, chunked to avoid
// blowing the call stack on String.fromCharCode for large buffers).
export function int16ToBase64(int16) {
  const bytes = new Uint8Array(int16.buffer, int16.byteOffset, int16.byteLength)
  let bin = ''
  const CHUNK = 0x8000
  for (let i = 0; i < bytes.length; i += CHUNK) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK))
  }
  return btoa(bin)
}

export function base64ToInt16(b64) {
  const bin = atob(b64)
  const bytes = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
  return new Int16Array(bytes.buffer, bytes.byteOffset, Math.floor(bytes.byteLength / 2))
}

// Shared singleton — the app's one sound sink (chat, voice panel, overlay).
export const audioPlayer = new AudioPlayer()
