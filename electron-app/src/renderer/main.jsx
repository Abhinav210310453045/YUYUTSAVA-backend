import React from 'react'
import { createRoot } from 'react-dom/client'
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
