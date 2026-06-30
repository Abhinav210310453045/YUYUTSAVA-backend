import React from 'react'
import { createRoot } from 'react-dom/client'
import '@fontsource/jetbrains-mono/400.css'
import '@fontsource/jetbrains-mono/600.css'
import './styles/globals.css'
import VoiceOverlay from './components/voice/VoiceOverlay'
import { initBase } from './api/client'

// The overlay is a thin second renderer over the same /ws/converse conversation
// as the main Voice panel. It needs the daemon base URL but none of the main
// app's SSE/proposal plumbing.
initBase().then(() => {
  createRoot(document.getElementById('root')).render(<VoiceOverlay />)
})
