import React from 'react'
import { createRoot } from 'react-dom/client'
// Self-hosted JetBrains Mono (weights used by --font-mono). Bundled by Vite
// and served from 'self', so the strict CSP needs no external font hosts.
import '@fontsource/jetbrains-mono/400.css'
import '@fontsource/jetbrains-mono/600.css'
import '@fontsource/jetbrains-mono/700.css'
import './styles/globals.css'
import App from './App'
import { SSEProvider } from './hooks/useSSE.jsx'
import { initBase } from './api/client'

initBase().then(() => {
  createRoot(document.getElementById('root')).render(
    <SSEProvider>
      <App />
    </SSEProvider>
  )
})
