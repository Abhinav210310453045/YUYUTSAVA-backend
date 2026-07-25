import React, { useState, useCallback, useEffect } from 'react'
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
import ResizeHandle from './components/common/ResizeHandle'
import { useSSE, getLogsEnabled, setLogsEnabled } from './hooks/useSSE.jsx'
import { useRuntimeSettings } from './hooks/useRuntimeSettings'
import { NotificationsProvider } from './hooks/useNotifications.jsx'
import { NavProvider, useNav } from './nav/NavProvider'
import { getLogLevel, setLogLevel } from './api/client'

const ACTIVITY_MIN = 180
const ACTIVITY_MAX = 600

export default function App() {
  return (
    <NavProvider>
      <AppShell />
    </NavProvider>
  )
}

function AppShell() {
  // Navigation — including which tab is active, how deep you are in it, and
  // restoring all of that after an in-run reload — lives in NavProvider.
  // Hide/minimize keeps the renderer alive, so those need nothing at all.
  const { activePanel, switchTab, topRouteOf } = useNav()
  // Thread to resume rides the tab's route params: opening a row in Sessions
  // pins it, and it survives navigating away and back.
  const chatResumeId = topRouteOf('chat').params.resumeId || null
  const voiceResumeId = topRouteOf('voice').params.resumeId || null
  // Chat & Voice are mounted once visited and then kept alive (just hidden) so
  // navigating away — e.g. to Settings mid-conversation — and back preserves the
  // live WebSocket, messages, and audio instead of destroying them.
  const [visited, setVisited] = useState(() => ({ chat: true, voice: activePanel === 'voice' }))
  const { proposals, eventLines, logLines, bgTasks, connected, pendingCount, removeProposal } = useSSE()

  // A tab only needs mounting once it's been looked at — including when the
  // nav tree is restored straight onto Voice after a reload.
  useEffect(() => {
    if (activePanel === 'chat' || activePanel === 'voice') {
      setVisited((v) => (v[activePanel] ? v : { ...v, [activePanel]: true }))
    }
  }, [activePanel])

  // Open a session from a Sessions row, routing by its origin: voice sessions
  // resume in the Voice panel, chat/ui sessions in the Chat panel. Both reuse
  // the session's own thread_id so the backend continues the same conversation.
  const onOpenSession = useCallback((session) => {
    const target = session?.origin === 'voice' ? 'voice' : 'chat'
    switchTab(target, { resumeId: session.id })
  }, [switchTab])

  // Daemon-owned voice mode. The titlebar button flips it; the state also
  // reaches the overlay and the CLI, since it lives in the daemon.
  const { voice, voiceOn, saving: voiceSaving, setVoice } = useRuntimeSettings()

  // The main process owns the global hotkey and the overlay, and has no daemon
  // client of its own — so the renderer pushes the flag to it. Re-pushed on
  // every change (and on mount) so a daemon restart can't leave main stale.
  useEffect(() => {
    window.electronAPI?.setVoiceModeEnabled?.(voiceOn)
  }, [voiceOn])

  const onToggleVoice = useCallback((next) => {
    // One button, both halves: "voice mode" as the user thinks of it is
    // "does it listen for me, and does it talk back".
    setVoice({ wake_enabled: next, tts_enabled: next }).catch(() => {})
  }, [setVoice])

  // Hotkey/wake while the window is focused: switch to the Voice panel and bump
  // a nonce so the panel auto-starts the mic (even if it was already mounted).
  const [voiceAutoStart, setVoiceAutoStart] = useState(0)
  useEffect(() => {
    const off = window.electronAPI?.onVoiceActivate?.(() => {
      switchTab('voice')
      setVoiceAutoStart((n) => n + 1)
    })
    return () => off && off()
  }, [switchTab])

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
      switchTab('proposals')
    })
    return () => off && off()
  }, [switchTab])

  // Tray menu navigation (tray → "Open"/"Settings"). 'dashboard' maps to the
  // proposals home view; other targets map straight to a panel id.
  useEffect(() => {
    const off = window.electronAPI?.onNavigate?.((target) => {
      switchTab(target === 'dashboard' ? 'proposals' : target)
    })
    return () => off && off()
  }, [switchTab])

  const [activityW, setActivityW] = useState(300)
  const [activityOpen, setActivityOpen] = useState(false)
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
          onNav={switchTab}
          pendingCount={pendingCount}
          activityOpen={activityOpen}
          onToggleActivity={setActivityOpen}
          voiceOn={voiceOn}
          voiceSaving={voiceSaving}
          onToggleVoice={onToggleVoice}
        />

        <div style={{ flex: 1, display: 'flex', overflow: 'hidden' }}>
          {/* Main panel. Stateless views remount on tab switch (re-keyed so the
              entry animation replays) — their drill-down position comes back
              from the route and their view state from useViewState. Chat &
              Voice stay mounted once visited and are only hidden, preserving
              their live conversation across navigation. */}
          <div style={{ flex: 1, display: 'flex', overflow: 'hidden', minWidth: 0, position: 'relative' }}>
            {(activePanel === 'proposals' || activePanel === 'sessions' || activePanel === 'todos' || activePanel === 'artifacts' || activePanel === 'settings') && (
              <div
                key={activePanel}
                style={{ flex: 1, display: 'flex', overflow: 'hidden', animation: 'fade-in 0.2s ease' }}
              >
                {activePanel === 'proposals' && (
                  <ProposalsPanel
                    proposals={proposals}
                    onRemoveProposal={removeProposal}
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
                <VoicePanel onOpenSettings={() => switchTab('settings')} autoStartSignal={voiceAutoStart} resumeId={voiceResumeId} active={activePanel === 'voice'} voiceSettings={voice} />
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
