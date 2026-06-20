import React, { useState, useCallback, useEffect, useRef } from 'react'
import Titlebar from './components/layout/Titlebar'
import ActivityLog from './components/layout/ActivityLog'
import ProposalsPanel from './components/proposals/ProposalsPanel'
import SessionsPanel from './components/sessions/SessionsPanel'
import SettingsPanel from './components/settings/SettingsPanel'
import ChatPanel from './components/chat/ChatPanel'
import InWindowToast from './components/notifications/InWindowToast'
import { useSSE, getLogsEnabled, setLogsEnabled } from './hooks/useSSE.jsx'
import { NotificationsProvider } from './hooks/useNotifications.jsx'
import { getLogLevel, setLogLevel } from './api/client'

const ACTIVITY_MIN = 180
const ACTIVITY_MAX = 600

function ResizeHandle({ onMouseDown, side }) {
  const [hovered, setHovered] = useState(false)
  return (
    <div
      onMouseDown={onMouseDown}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        width: 4,
        flexShrink: 0,
        cursor: 'col-resize',
        background: hovered ? 'var(--neon-green)' : 'transparent',
        opacity: hovered ? 0.4 : 1,
        transition: 'background 0.15s',
        zIndex: 10,
        position: 'relative',
      }}
    >
      {/* wider invisible hit area */}
      <div style={{
        position: 'absolute',
        top: 0, bottom: 0,
        left: -4, right: -4,
      }} />
    </div>
  )
}

export default function App() {
  const [activePanel, setActivePanel] = useState('proposals')
  const { proposals, asks, eventLines, logLines, bgTasks, connected, pendingCount, removeProposal, removeAsk } = useSSE()

  const [logsEnabled, setLogsEnabledState] = useState(getLogsEnabled())
  const [logLevel, setLogLevelState] = useState('INFO')

  useEffect(() => {
    getLogLevel().then((r) => setLogLevelState(r.level)).catch(() => {})
  }, [])

  const onToggleLogs = useCallback((next) => {
    setLogsEnabled(next)
    setLogsEnabledState(next)
  }, [])

  const onChangeLogLevel = useCallback((next) => {
    setLogLevelState(next)
    setLogLevel(next).catch(() => {})
  }, [])

  // OS banner click forwarded from main: bring proposals tab forward so the
  // highlighted card is visible. The id-scroll happens inside ProposalsPanel,
  // which consumes highlightId from NotificationsProvider.
  useEffect(() => {
    const off = window.electronAPI?.onNotificationClick?.(() => {
      setActivePanel('proposals')
    })
    return () => off && off()
  }, [])

  // Tray menu navigation (tray → "Open"/"Settings"). 'dashboard' maps to the
  // proposals home view; other targets map straight to a panel id.
  useEffect(() => {
    const off = window.electronAPI?.onNavigate?.((target) => {
      setActivePanel(target === 'dashboard' ? 'proposals' : target)
    })
    return () => off && off()
  }, [])

  const [activityW, setActivityW] = useState(300)
  const [activityOpen, setActivityOpen] = useState(true)
  const [dragging, setDragging] = useState(false)

  const startDrag = useCallback((e) => {
    e.preventDefault()
    const startX = e.clientX
    const startActivity = activityW
    setDragging(true)

    const onMove = (ev) => {
      const dx = ev.clientX - startX
      setActivityW(Math.min(ACTIVITY_MAX, Math.max(ACTIVITY_MIN, startActivity - dx)))
    }
    const onUp = () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      setDragging(false)
    }
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
  }, [activityW])

  return (
    <NotificationsProvider>
      <div style={{
        height: '100vh',
        display: 'flex',
        flexDirection: 'column',
        background: 'var(--bg-deep)',
        overflow: 'hidden',
      }}>
        <Titlebar
          connected={connected}
          logsEnabled={logsEnabled}
          onToggleLogs={onToggleLogs}
          logLevel={logLevel}
          onChangeLogLevel={onChangeLogLevel}
          activePanel={activePanel}
          onNav={setActivePanel}
          pendingCount={pendingCount}
          activityOpen={activityOpen}
          onToggleActivity={setActivityOpen}
        />

        <div style={{ flex: 1, display: 'flex', overflow: 'hidden' }}>
          {/* Main panel — re-keyed so switching replays the entry animation. */}
          <div style={{ flex: 1, display: 'flex', overflow: 'hidden', minWidth: 0 }}>
            <div
              key={activePanel}
              style={{ flex: 1, display: 'flex', overflow: 'hidden', animation: 'fade-in 0.2s ease' }}
            >
              {activePanel === 'proposals' && (
                <ProposalsPanel
                  proposals={proposals}
                  asks={asks}
                  onRemoveProposal={removeProposal}
                  onRemoveAsk={removeAsk}
                />
              )}
              {activePanel === 'sessions' && <SessionsPanel />}
              {activePanel === 'settings' && <SettingsPanel />}
              {activePanel === 'chat' && <ChatPanel />}
            </div>
          </div>

          {activityOpen && <ResizeHandle onMouseDown={startDrag} side="right" />}

          <div style={{
            width: activityOpen ? activityW : 0,
            display: 'flex',
            flexDirection: 'column',
            overflow: 'hidden',
            borderTop: 'none',
            flexShrink: 0,
            transition: dragging ? 'none' : 'width 0.25s ease',
          }}>
            <ActivityLog events={eventLines} logs={logLines} bgTasks={bgTasks} width={activityW} />
          </div>
        </div>

        <InWindowToast />
      </div>
    </NotificationsProvider>
  )
}
