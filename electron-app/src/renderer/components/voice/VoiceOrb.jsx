import React, { useEffect, useRef } from 'react'
import { audioPlayer } from '../../audio'

// Reactive audio centerpiece for the Voice panel: a glowing gradient orb whose
// size/glow tracks live amplitude, ringed by a radial waveform from the FFT.
// It reads the mic analyser while `listening`, the TTS analyser while
// `speaking`, and breathes gently when idle. Pure canvas + requestAnimationFrame
// — no deps. The clickable MicButton sits on top (this is the backdrop).
//
// props: { listening, speaking, getMicAnalyser, size }

const GREEN = [0, 255, 136]
const BLUE = [120, 160, 255]

export default function VoiceOrb({ listening, speaking, getMicAnalyser, size = 200 }) {
  const canvasRef = useRef(null)
  // Latest state in refs so the rAF loop (started once) always sees fresh values.
  const stateRef = useRef({ listening, speaking, getMicAnalyser })
  stateRef.current = { listening, speaking, getMicAnalyser }

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    const dpr = window.devicePixelRatio || 1
    canvas.width = size * dpr
    canvas.height = size * dpr
    ctx.scale(dpr, dpr)

    const cx = size / 2
    const cy = size / 2
    // Keep the core small enough that the outer glow (drawn to R*2.0 below) and
    // the waveform ring stay inside the canvas — otherwise the bloom gets clipped
    // by the canvas edge and reads as a hard-cut box.
    const baseR = size * 0.16
    let raf = 0
    let phase = 0
    let smoothLevel = 0
    const freq = new Uint8Array(128)

    const pickAnalyser = () => {
      const { listening: l, speaking: s, getMicAnalyser: g } = stateRef.current
      if (s) return audioPlayer.getAnalyser()
      if (l) return g?.()
      return null
    }

    const draw = () => {
      const { listening: l, speaking: s } = stateRef.current
      const active = l || s
      const accent = s ? BLUE : GREEN
      const analyser = pickAnalyser()

      // Amplitude 0..1 from the FFT (or a gentle idle breath).
      let level = 0
      let bins = 0
      if (analyser) {
        const n = Math.min(freq.length, analyser.frequencyBinCount)
        analyser.getByteFrequencyData(freq)
        let sum = 0
        for (let i = 0; i < n; i++) sum += freq[i]
        bins = n
        level = n ? Math.min(1, (sum / n) / 140) : 0
      }
      if (!active) level = 0.12 + 0.05 * Math.sin(phase * 1.4) // idle breathing
      smoothLevel += (level - smoothLevel) * 0.18
      phase += 0.03

      ctx.clearRect(0, 0, size, size)

      const [r, g, b] = accent
      const R = baseR * (1 + smoothLevel * 0.55)

      // Outer glow — kept within the canvas so it fades to nothing instead of
      // being sliced off at the edge.
      const glowR = Math.min(R * 2.85, size / 2)
      const glow = ctx.createRadialGradient(cx, cy, R * 0.3, cx, cy, glowR)
      glow.addColorStop(0, `rgba(${r},${g},${b},${0.22 + smoothLevel * 0.4})`)
      glow.addColorStop(1, `rgba(${r},${g},${b},0)`)
      ctx.fillStyle = glow
      ctx.beginPath(); ctx.arc(cx, cy, glowR, 0, Math.PI * 2); ctx.fill()

      // Radial waveform ring
      if (analyser && bins) {
        ctx.lineWidth = 2
        ctx.strokeStyle = `rgba(${r},${g},${b},0.85)`
        ctx.beginPath()
        const spokes = 72
        for (let i = 0; i <= spokes; i++) {
          const a = (i / spokes) * Math.PI * 2
          const bin = Math.floor((i / spokes) * bins)
          const amp = (freq[bin] || 0) / 255
          const rr = R * 1.35 + amp * size * 0.16
          const x = cx + Math.cos(a) * rr
          const y = cy + Math.sin(a) * rr
          i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y)
        }
        ctx.stroke()
      }

      // Core orb (gradient fill)
      const core = ctx.createRadialGradient(cx - R * 0.3, cy - R * 0.3, R * 0.1, cx, cy, R)
      core.addColorStop(0, `rgba(${r},${g},${b},0.95)`)
      core.addColorStop(1, `rgba(${r},${g},${b},0.35)`)
      ctx.fillStyle = core
      ctx.beginPath(); ctx.arc(cx, cy, R, 0, Math.PI * 2); ctx.fill()

      raf = requestAnimationFrame(draw)
    }
    raf = requestAnimationFrame(draw)
    return () => cancelAnimationFrame(raf)
  }, [size])

  return (
    <canvas
      ref={canvasRef}
      style={{ width: size, height: size, display: 'block' }}
      aria-hidden
    />
  )
}
