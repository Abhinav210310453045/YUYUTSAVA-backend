import React from 'react'
import { createRoot } from 'react-dom/client'
import '@fontsource/jetbrains-mono/400.css'
import '@fontsource/jetbrains-mono/600.css'
import './styles/globals.css'
// Imported after globals so its transparent-background override wins — the
// overlay window must not paint a backdrop of its own.
import './styles/overlay.css'
import VoiceOverlay from './components/voice/VoiceOverlay'
import AskOverlay from './components/asks/AskOverlay'
import { initBase } from './api/client'

// The overlay is a thin second renderer: an always-on-top, all-Spaces window
// that can reach the user when the main app isn't in front of them. It hosts
// two things — the voice conversation (same /ws/converse as the Voice panel)
// and pending asks, which is what makes "grant permission while you're busy in
// another app" work at all.
//
// It deliberately does NOT mount SSEProvider: that provider forwards wake-word
// events to main and fires OS notifications, and from this window that would
// pop the overlay at itself. AskOverlay runs its own lean, ask-only stream.
initBase().then(() => {
  createRoot(document.getElementById('root')).render(
    <>
      <VoiceOverlay />
      <AskOverlay />
    </>
  )
})
