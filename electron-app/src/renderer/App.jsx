import React, { useState, useCallback, useEffect, useRef } from 'react'
import Titlebar from './components/layout/Titlebar'
import Sidebar from './components/layout/Sidebar'
import ActivityLog from './components/layout/ActivityLog'
import ProposalsPanel from './components/proposals/ProposalsPanel'
import SettingsPanel from './components/settings/SettingsPanel'
import ChatPanel from './components/chat/ChatPanel'
import InWindowToast from './components/notifications/InWindowToast'
import { useSSE } from './hooks/useSSE.jsx'
import { NotificationsProvider } from './hooks/useNotifications.jsx'

const SIDEBAR_MIN = 48
const SIDEBAR_MAX = 200
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
  const { proposals, asks, logLines, connected, pendingCount, removeProposal, removeAsk } = useSSE()

  // OS banner click forwarded from main: bring proposals tab forward so the
  // highlighted card is visible. The id-scroll happens inside ProposalsPanel,
  // which consumes highlightId from NotificationsProvider.
  useEffect(() => {
    const off = window.electronAPI?.onNotificationClick?.(() => {
      setActivePanel('proposals')
    })
    return () => off && off()
  }, [])

  const [sidebarW, setSidebarW] = useState(60)
  const [activityW, setActivityW] = useState(300)
  const dragRef = useRef(null)

  const startDrag = useCallback((which, e) => {
    e.preventDefault()
    const startX = e.clientX
    const startSidebar = sidebarW
    const startActivity = activityW

    const onMove = (ev) => {
      const dx = ev.clientX - startX
      if (which === 'sidebar') {
        setSidebarW(Math.min(SIDEBAR_MAX, Math.max(SIDEBAR_MIN, startSidebar + dx)))
      } else {
        setActivityW(Math.min(ACTIVITY_MAX, Math.max(ACTIVITY_MIN, startActivity - dx)))
      }
    }
    const onUp = () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
    }
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
  }, [sidebarW, activityW])

  return (
    <NotificationsProvider>
      <div style={{
        height: '100vh',
        display: 'flex',
        flexDirection: 'column',
        background: 'var(--bg-deep)',
        overflow: 'hidden',
      }}>
        <Titlebar connected={connected} />

        <div style={{ flex: 1, display: 'flex', overflow: 'hidden' }}>
          <Sidebar active={activePanel} onNav={setActivePanel} pendingCount={pendingCount} width={sidebarW} />

          <ResizeHandle onMouseDown={(e) => startDrag('sidebar', e)} side="left" />

          {/* Main panel */}
          <div style={{ flex: 1, display: 'flex', overflow: 'hidden' }}>
            {activePanel === 'proposals' && (
              <ProposalsPanel
                proposals={proposals}
                asks={asks}
                onRemoveProposal={removeProposal}
                onRemoveAsk={removeAsk}
              />
            )}
            {activePanel === 'settings' && <SettingsPanel />}
            {activePanel === 'chat' && <ChatPanel />}
          </div>

          <ResizeHandle onMouseDown={(e) => startDrag('activity', e)} side="right" />

          <ActivityLog lines={logLines} width={activityW} />
        </div>

        <InWindowToast />
      </div>
    </NotificationsProvider>
  )
}
