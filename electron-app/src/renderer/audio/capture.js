// Microphone capture for the voice pipeline.
//
// Captures mono PCM at 16 kHz (the rate the daemon's VAD/STT expect) and emits
// fixed-size Int16 frames to a callback, which the WS layer base64-encodes and
// streams as {type:"audio"} messages. We run an AudioContext pinned to 16 kHz so
// the browser resamples for us, and a tiny AudioWorklet (loaded from a Blob URL
// so there's no bundler path to manage) chunks the float samples into frames.
//
// echoCancellation/noiseSuppression are requested so the agent's own TTS coming
// out of the speakers doesn't false-trigger barge-in (a known issue when keeping
// the mic open during playback). autoGainControl is intentionally OFF: AGC boosts
// quiet input toward a target level, which would amplify residual echo and defeat
// the daemon's energy-gated barge-in (see VadSegmenter barge_energy_threshold).

const FRAME_SAMPLES = 480 // 30 ms @ 16 kHz — matches the daemon VAD frame size

// AudioWorklet processor source. Accumulates input samples and posts Int16
// frames of FRAME_SAMPLES. Kept dependency-free; runs in the audio thread.
const WORKLET_SRC = `
class PcmCapture extends AudioWorkletProcessor {
  constructor(options) {
    super()
    this._frame = (options.processorOptions && options.processorOptions.frameSamples) || ${FRAME_SAMPLES}
    this._buf = new Float32Array(this._frame)
    this._n = 0
  }
  process(inputs) {
    const input = inputs[0]
    if (input && input[0]) {
      const ch = input[0]
      for (let i = 0; i < ch.length; i++) {
        this._buf[this._n++] = ch[i]
        if (this._n === this._frame) {
          const out = new Int16Array(this._frame)
          for (let j = 0; j < this._frame; j++) {
            let s = this._buf[j]
            s = s < -1 ? -1 : s > 1 ? 1 : s
            out[j] = s < 0 ? s * 0x8000 : s * 0x7fff
          }
          this.port.postMessage(out, [out.buffer])
          this._n = 0
        }
      }
    }
    return true
  }
}
registerProcessor('pcm-capture', PcmCapture)
`

export class MicCapture {
  constructor({ onFrame, sampleRate = 16000 } = {}) {
    this._onFrame = onFrame
    this._sampleRate = sampleRate
    this._stream = null
    this._ctx = null
    this._node = null
    this._source = null
    this._workletUrl = null
  }

  get active() { return this._ctx != null }

  async start() {
    if (this._ctx) return
    this._stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: false },
    })
    const Ctx = window.AudioContext || window.webkitAudioContext
    this._ctx = new Ctx({ sampleRate: this._sampleRate })
    if (this._ctx.state === 'suspended') { try { await this._ctx.resume() } catch { /* ignore */ } }

    this._workletUrl = URL.createObjectURL(new Blob([WORKLET_SRC], { type: 'application/javascript' }))
    await this._ctx.audioWorklet.addModule(this._workletUrl)

    this._source = this._ctx.createMediaStreamSource(this._stream)
    this._node = new AudioWorkletNode(this._ctx, 'pcm-capture', {
      processorOptions: { frameSamples: FRAME_SAMPLES },
    })
    this._node.port.onmessage = (e) => { this._onFrame?.(e.data) }

    // A worklet only runs when its output is pulled — route through a silent
    // gain to the destination so it processes without any audible monitor.
    const sink = this._ctx.createGain()
    sink.gain.value = 0
    this._source.connect(this._node)
    this._node.connect(sink).connect(this._ctx.destination)
  }

  async stop() {
    try { this._node && (this._node.port.onmessage = null) } catch { /* ignore */ }
    try { this._source && this._source.disconnect() } catch { /* ignore */ }
    try { this._node && this._node.disconnect() } catch { /* ignore */ }
    try { this._stream && this._stream.getTracks().forEach((t) => t.stop()) } catch { /* ignore */ }
    try { this._ctx && (await this._ctx.close()) } catch { /* ignore */ }
    if (this._workletUrl) { try { URL.revokeObjectURL(this._workletUrl) } catch { /* ignore */ } }
    this._stream = this._ctx = this._node = this._source = this._workletUrl = null
  }
}
