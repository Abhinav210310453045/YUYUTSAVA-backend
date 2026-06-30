// Smooth, adaptive pacing of streamed LLM text in the renderer.
//
// The chat WS delivers assistant text as token *chunks* that arrive in irregular
// sizes and at irregular intervals (a big block, a stall, another block).
// Rendering each chunk the instant it arrives mirrors that irregularity and feels
// jerky. TokenSmoother decouples the *display* rate from the *arrival* rate:
// feed() appends to an internal buffer without blocking; a single rAF-driven
// drain reveals the buffer a few characters at a time at a steady, adaptive
// cadence.
//
//   * When caught up, it emits at a gentle steady pace (baseCps) so prose flows
//     like a typewriter.
//   * When the model is far ahead (large backlog), it speeds up toward maxCps so
//     it never lags noticeably and a burst finishes fast.
//
// This is a JS mirror of yuyutsava/cli/stream_smoother.py (TokenSmoother). It is
// transport-agnostic (just a `write` callback), so the voice UI can reuse it.
export class TokenSmoother {
  // write:        (chunk:string) => void — appends revealed text to the sink.
  // baseCps:      steady chars/sec when caught up (the typewriter "feel").
  // maxCps:       upper bound chars/sec used to burn down a large backlog.
  // catchupChars: backlog (chars) at which the delay is ~halved; smaller = eager.
  constructor(write, { baseCps = 150, maxCps = 1200, catchupChars = 120 } = {}) {
    this._write = write
    this._baseDelay = 1000 / Math.max(1, baseCps) // ms per tick when caught up
    this._minDelay = 1000 / Math.max(1, maxCps)
    this._catchup = Math.max(1, catchupChars)
    this._buf = ''
    this._raf = null
    this._lastTick = 0
    this._stopped = false
  }

  // Append text and ensure the drain loop is running. Non-blocking.
  feed(text) {
    if (!text || this._stopped) return
    this._buf += text
    this._ensureRunning()
  }

  // Reveal everything buffered immediately (used before non-prose events, on
  // final/turn_end, and on teardown so nothing is left half-shown).
  flush() {
    if (this._buf) {
      const out = this._buf
      this._buf = ''
      this._write(out)
    }
    this._cancel()
  }

  // Stop the loop and drop any pending text without writing it.
  stop() {
    this._stopped = true
    this._buf = ''
    this._cancel()
  }

  // -- internals -------------------------------------------------------------

  _ensureRunning() {
    if (this._raf != null || this._stopped) return
    this._lastTick = 0
    this._raf = requestAnimationFrame(this._tick)
  }

  _cancel() {
    if (this._raf != null) {
      cancelAnimationFrame(this._raf)
      this._raf = null
    }
  }

  // Per-tick delay: larger backlog -> shorter delay (mirrors _delay_for).
  _delayFor(backlog) {
    const delay = this._baseDelay / (1 + backlog / this._catchup)
    return delay < this._minDelay ? this._minDelay : delay
  }

  _tick = (now) => {
    this._raf = null
    if (this._stopped) return
    if (!this._buf) return // idle; feed() will restart us

    if (this._lastTick === 0) this._lastTick = now
    const backlog = this._buf.length
    const elapsed = now - this._lastTick

    if (elapsed >= this._delayFor(backlog)) {
      // Emit a chunk sized to the backlog: a few chars when caught up, more per
      // tick when far behind, so big bursts catch up without thousands of frames.
      const chunk = 1 + Math.floor(backlog / this._catchup)
      const out = this._buf.slice(0, chunk)
      this._buf = this._buf.slice(chunk)
      this._write(out)
      this._lastTick = now
    }

    if (this._buf) this._raf = requestAnimationFrame(this._tick)
  }
}
