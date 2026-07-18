import React, { useEffect, useRef, useState } from 'react'
import { blockSrc } from './src'
import { audioPlayer } from '../../../audio'

// Phase-7 audio block. Plays through the shared AudioPlayer singleton (fetch
// + Web Audio decode, the persisted-clip path ChatPanel bubbles use) rather
// than an <audio> element — the app CSP's media-src doesn't allowlist the
// daemon, and the singleton keeps card clips, chat replies, and earcons on
// one output with one pause/stop story.

export const matches = (att) => (att.mime || '').startsWith('audio/')

// Same glyph as ChatPanel's spoken-reply control.
function PlayPauseIcon({ playing }) {
  return playing ? (
    <svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor" aria-hidden>
      <rect x="6" y="5" width="4" height="14" rx="1.2" />
      <rect x="14" y="5" width="4" height="14" rx="1.2" />
    </svg>
  ) : (
    <svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor" aria-hidden>
      <path d="M8 5v14l11-7z" />
    </svg>
  )
}

function humanSize(bytes) {
  if (typeof bytes !== 'number' || !isFinite(bytes)) return null
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

export default function AudioBlock({ attachment, cardId }) {
  const [playing, setPlaying] = useState(false)
  const [paused, setPaused] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const pollRef = useRef(null)
  const playingRef = useRef(false)

  const clearPoll = () => {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null }
  }

  // Stop our own playback when the tile unmounts (card closed, attachment
  // deleted) — the shared player would otherwise keep the clip audible.
  useEffect(() => () => {
    clearPoll()
    if (playingRef.current) audioPlayer.stop()
  }, [])

  const stopped = () => {
    clearPoll()
    playingRef.current = false
    setPlaying(false)
    setPaused(false)
  }

  const start = async () => {
    setError(null)
    setLoading(true)
    audioPlayer.stop() // cut whatever else is on the shared player, reset its cursor
    try {
      await audioPlayer.playUrl(blockSrc(attachment, cardId))
    } catch (e) {
      setLoading(false)
      setError(e.message)
      return
    }
    setLoading(false)
    playingRef.current = true
    setPlaying(true)
    setPaused(false)
    // Poll until the player drains (a paused context freezes its clock, so
    // isPlaying() holds true across a pause) — the useConverse pattern.
    clearPoll()
    pollRef.current = setInterval(() => {
      if (!audioPlayer.isPlaying()) stopped()
    }, 250)
  }

  // ▶ plays (or resumes a paused clip), ⏸ pauses in place with position held.
  const onToggle = async () => {
    if (!playing) { await start(); return }
    if (audioPlayer.isPaused()) {
      await audioPlayer.resume()
      setPaused(false)
    } else {
      await audioPlayer.pause()
      setPaused(true)
    }
  }

  const size = humanSize(attachment.meta?.size)
  const audible = playing && !paused
  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 10, padding: '8px 10px',
      background: 'var(--bg-card)', border: '1px solid var(--border-subtle)',
      borderRadius: 6, fontFamily: 'var(--font-mono)', fontSize: 11, minWidth: 0,
    }}>
      <button
        onClick={onToggle}
        disabled={loading}
        title={playing ? (paused ? 'resume playback' : 'pause playback') : 'play audio'}
        className="tap-pop"
        style={{
          cursor: 'pointer', width: 24, height: 24, borderRadius: '50%', flexShrink: 0,
          display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
          background: audible ? 'rgba(120,160,255,0.30)' : 'rgba(120,160,255,0.12)',
          border: `1px solid rgba(120,160,255,${audible ? 0.6 : 0.35})`,
          color: 'var(--text-info)',
          boxShadow: audible ? '0 0 10px rgba(120,160,255,0.5)' : 'none',
          transition: 'background 0.2s, box-shadow 0.2s',
          opacity: loading ? 0.5 : 1,
        }}
      ><PlayPauseIcon playing={audible} /></button>
      <span style={{ fontSize: 14, flexShrink: 0 }}>🔊</span>
      <span style={{
        flex: 1, minWidth: 0, color: error ? 'var(--neon-red)' : 'var(--text-dim)',
        fontSize: 10, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
      }}>
        {error ? `> ${error}`
          : loading ? 'loading…'
          : `${attachment.mime}${size ? ` · ${size}` : ''}${paused ? ' · paused' : audible ? ' · playing' : ''}`}
      </span>
    </div>
  )
}
