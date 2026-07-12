import React, { useState, useCallback, useEffect, useRef } from 'react'
import Titlebar from './components/layout/Titlebar'
import ActivityLog from './components/layout/ActivityLog'
import ProposalsPanel from './components/proposals/ProposalsPanel'
import SessionsPanel from './components/sessions/SessionsPanel'
import ArtifactsPanel from './components/artifacts/ArtifactsPanel'
import TodosPanel from './components/todos/TodosPanel'
import SettingsPanel from './components/settings/SettingsPanel'
import ChatPanel from './components/chat/ChatPanel'
import VoicePanel from './components/voice/VoicePanel'
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
  // Thread id to resume when the Chat panel opens from a session row. Cleared on
  // any plain navigation so the Chat nav icon always starts a fresh UI session.
  const [chatResumeId, setChatResumeId] = useState(null)
  // Thread id to resume when the Voice panel opens from a voice session row.
  const [voiceResumeId, setVoiceResumeId] = useState(null)
  // Chat & Voice are mounted once visited and then kept alive (just hidden) so
  // navigating away — e.g. to Settings mid-conversation — and back preserves the
  // live WebSocket, messages, and audio instead of destroying them.
  const [visited, setVisited] = useState({ chat: false, voice: false })
  const { proposals, asks, eventLines, logLines, bgTasks, connected, pendingCount, removeProposal, removeAsk } = useSSE()

  // Plain navigation no longer resets the chat thread — returning to Chat/Voice
  // shows the last conversation. A fresh thread is started explicitly via the
  // per-view "New" button, or by opening a session from the Sessions list.
  const navTo = useCallback((target) => {
    setActivePanel(target)
    if (target === 'chat' || target === 'voice') {
      setVisited((v) => (v[target] ? v : { ...v, [target]: true }))
    }
  }, [])

  // Open a session from a Sessions row, routing by its origin: voice sessions
  // resume in the Voice panel, chat/ui sessions in the Chat panel. Both reuse
  // the session's own thread_id so the backend continues the same conversation.
  const onOpenSession = useCallback((session) => {
    const target = session?.origin === 'voice' ? 'voice' : 'chat'
    if (target === 'voice') setVoiceResumeId(session.id)
    else setChatResumeId(session.id)
    setActivePanel(target)
    setVisited((v) => (v[target] ? v : { ...v, [target]: true }))
  }, [])

  // Hotkey/wake while the window is focused: switch to the Voice panel and bump
  // a nonce so the panel auto-starts the mic (even if it was already mounted).
  const [voiceAutoStart, setVoiceAutoStart] = useState(0)
  useEffect(() => {
    const off = window.electronAPI?.onVoiceActivate?.(() => {
      setActivePanel('voice')
      setVisited((v) => (v.voice ? v : { ...v, voice: true }))
      setVoiceAutoStart((n) => n + 1)
    })
    return () => off && off()
  }, [])

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
      navTo('proposals')
    })
    return () => off && off()
  }, [navTo])

  // Tray menu navigation (tray → "Open"/"Settings"). 'dashboard' maps to the
  // proposals home view; other targets map straight to a panel id.
  useEffect(() => {
    const off = window.electronAPI?.onNavigate?.((target) => {
      navTo(target === 'dashboard' ? 'proposals' : target)
    })
    return () => off && off()
  }, [navTo])

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
          onNav={navTo}
          pendingCount={pendingCount}
          activityOpen={activityOpen}
          onToggleActivity={setActivityOpen}
        />

        <div style={{ flex: 1, display: 'flex', overflow: 'hidden' }}>
          {/* Main panel. Stateless views remount (re-keyed) so switching replays
              the entry animation; Chat & Voice stay mounted once visited and are
              only hidden, preserving their live conversation across navigation. */}
          <div style={{ flex: 1, display: 'flex', overflow: 'hidden', minWidth: 0, position: 'relative' }}>
            {(activePanel === 'proposals' || activePanel === 'sessions' || activePanel === 'todos' || activePanel === 'artifacts' || activePanel === 'settings') && (
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
                {activePanel === 'sessions' && <SessionsPanel onOpenSession={onOpenSession} />}
                {activePanel === 'todos' && <TodosPanel />}
                {activePanel === 'artifacts' && <ArtifactsPanel />}
                {activePanel === 'settings' && <SettingsPanel />}
              </div>
            )}
            {visited.chat && (
              <div style={{ flex: 1, display: activePanel === 'chat' ? 'flex' : 'none', overflow: 'hidden', minWidth: 0 }}>
                <ChatPanel resumeId={chatResumeId} active={activePanel === 'chat'} />
              </div>
            )}
            {visited.voice && (
              <div style={{ flex: 1, display: activePanel === 'voice' ? 'flex' : 'none', overflow: 'hidden', minWidth: 0 }}>
                <VoicePanel onOpenSettings={() => navTo('settings')} autoStartSignal={voiceAutoStart} resumeId={voiceResumeId} active={activePanel === 'voice'} />
              </div>
            )}
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
