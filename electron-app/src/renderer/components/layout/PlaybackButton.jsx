import React, { useCallback, useEffect, useState } from 'react'
import { audioPlayer } from '../../audio'
import { getSpeaker, subscribeSpeaker } from '../../conversations/store'
import { useNav } from '../../nav/NavProvider'

// Transport for a spoken reply you've walked away from.
//
// Conversations no longer die when you leave their view, so a voice answer can
// still be talking while you're on Settings or another card. This is the one
// control that follows it: ■ while it's audible (click to pause), ▶ once
// paused (click to resume), and nothing at all when it's silent — or when
// you're already looking at the conversation doing the talking, which has its
// own per-bubble control. It sits beside the voice-mode toggle: two buttons,
// different jobs, same cause.

// Square = "this is playing, stop it here". Triangle = "resume".
function TransportIcon({ playing }) {
  return playing ? (
    <svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor" aria-hidden>
      <rect x="5" y="5" width="14" height="14" rx="2" />
    </svg>
  ) : (
    <svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor" aria-hidden>
      <path d="M8 5v14l11-7z" />
    </svg>
  )
}

export default function PlaybackButton() {
  const { activePanel, params, push, switchTab } = useNav()
  const [playback, setPlayback] = useState(() => audioPlayer.playbackState())
  const [speaker, setSpeaker] = useState(getSpeaker)

  useEffect(() => audioPlayer.onChange(setPlayback), [])
  useEffect(() => subscribeSpeaker(setSpeaker), [])

  const nav = speaker?.nav || null
  // Already looking at the conversation that's speaking? Its own bubble
  // controls are right there — a second transport in the titlebar is noise.
  const onOwningView = !!nav && activePanel === nav.panel
    && (nav.panel !== 'todos' || params.cardId === nav.params?.cardId)

  const onToggle = useCallback(() => {
    if (audioPlayer.isPaused()) audioPlayer.resume()
    else audioPlayer.pause()
  }, [])

  const onGo = useCallback(() => {
    if (!nav) return
    if (nav.panel === 'todos') push('todos', nav.params || {})
    else switchTab(nav.panel)
  }, [nav, push, switchTab])

  if (!playback.playing || !speaker || onOwningView) return null

  const paused = playback.paused
  const label = speaker.label || 'Conversation'

  return (
    <span style={{
      display: 'flex',
      alignItems: 'center',
      gap: 4,
      height: 28,
      padding: '0 4px 0 2px',
      borderRadius: 6,
      border: '1px solid rgba(120,160,255,0.30)',
      background: 'rgba(120,160,255,0.10)',
      animation: 'fade-in 0.2s ease',
    }}>
      <button
        onClick={onToggle}
        title={paused ? `Resume ${label}'s spoken reply` : `Pause ${label}'s spoken reply`}
        className="tap-pop"
        style={{
          width: 24,
          height: 24,
          borderRadius: 5,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          background: 'transparent',
          border: 'none',
          color: 'var(--text-info)',
          cursor: 'pointer',
        }}
      >
        <TransportIcon playing={!paused} />
      </button>
      <button
        onClick={onGo}
        title={`${label} is speaking — go to it`}
        style={{
          background: 'transparent',
          border: 'none',
          padding: 0,
          color: 'var(--text-info)',
          fontFamily: 'var(--font-mono)',
          fontSize: 10,
          letterSpacing: '0.08em',
          textTransform: 'uppercase',
          cursor: 'pointer',
          maxWidth: 110,
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
        }}
      >
        {label}
      </button>
    </span>
  )
}
