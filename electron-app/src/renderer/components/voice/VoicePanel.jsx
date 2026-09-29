import React, { useEffect, useRef } from 'react'
import { liveActions } from '../../conversations/noticeActions'
import { useConverse } from '../../hooks/useConverse'
import { useSettings } from '../../hooks/useSettings'
import WakeWordOnboarding from './WakeWordOnboarding'
import AskCard from '../asks/AskCard'
import ResizeHandle from '../common/ResizeHandle'
import ContextAside from '../chat/ContextAside'
import { CTX_MAX_W, CTX_MIN_W, useContextColumn } from '../chat/useContextColumn'
import FloatingMic from './FloatingMic'
import VoiceBubble from './VoiceBubble'
import { EmptyVoiceThread, VoiceHeader, VoiceModeNotice, WakeWordNote } from './VoiceChrome'

// Persisted flag (in the daemon .env via main/settings.js) for the dismissible
// "wake keywords live in Settings" note. Stored as a UI-only env key.
const NOTE_KEY = 'UI_VOICE_NOTE_DISMISSED'

export default function VoicePanel({
  onOpenSettings,
  autoStartSignal = 0,
  resumeId = null,
  active = true,
  // Daemon-owned voice mode (App passes it down from useRuntimeSettings).
  // Defaults to fully on so the panel behaves as before when unset.
  voiceSettings = { wake_enabled: true, tts_enabled: true },
}) {
  const {
    messages,
    connected,
    busy,
    pendingAsk,
    listening,
    speaking,
    playingId,
    usage,
    answerAsk,
    startVoice,
    stopVoice,
    interrupt,
    replay,
    newSession,
    retry,
    getMicAnalyser,
  } = useConverse({ origin: 'voice', resumeId, active })
  const { settings, loading: settingsLoading, save } = useSettings()
  const scrollRef = useRef(null)
  const panelRef = useRef(null)

  // Context column, shared with the chat screen.
  const { ctxOpen, setCtxOpen, ctxW, ctxDragging, startCtxDrag } = useContextColumn('voice')

  const wakeOn = voiceSettings?.wake_enabled !== false
  const ttsOn = voiceSettings?.tts_enabled !== false
  const voiceModeOff = !wakeOn && !ttsOn

  const noteDismissed = settings[NOTE_KEY] === '1'
  // First-run wake-word setup: no wake word chosen yet and not yet onboarded.
  // Skipped while the wake word is switched off — offering to set one up is
  // noise when the user has just told us not to listen.
  const needsOnboarding =
    !settingsLoading && wakeOn && !settings.WAKE_WORDS && settings.UI_WAKE_ONBOARDED !== '1'

  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages, pendingAsk])

  // True while the CURRENT mic session was opened FOR the user (wake/hotkey)
  // rather than BY them tapping the mic — see the mute effect below.
  const autoStarted = useRef(false)

  // Hotkey/wake while the window is focused bumps autoStartSignal → start the
  // mic. Skip the initial mount (signal 0) so opening the panel by hand is quiet.
  // With the wake word off the mic is never opened for the user — only by their
  // own tap (main also gates the hotkey; this is the renderer-side guard).
  useEffect(() => {
    if (autoStartSignal > 0 && !listening && wakeOn) {
      autoStarted.current = true
      startVoice()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoStartSignal])

  // Turning the wake word off must close a mic WE opened — otherwise the
  // toggle claims "not listening" while the mic light is still on. A mic the
  // user opened by tapping is left alone: manual capture is exactly what voice
  // mode off still allows.
  useEffect(() => {
    if (!wakeOn && listening && autoStarted.current) {
      autoStarted.current = false
      stopVoice()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wakeOn])

  // The panel now stays mounted when you navigate away (so the conversation
  // survives). Stop the mic when it's hidden so we never leave a hot mic running
  // in the background — and so it can't collide with the wake-word overlay's mic.
  useEffect(() => {
    if (!active && listening) stopVoice()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active])

  // Voice's New also closes the mic, whichever way it is asked for.
  const startNew = () => {
    stopVoice()
    newSession()
  }
  const onNoticeAction = (id) => {
    if (id === 'retry') retry()
    else if (id === 'new_chat') startNew()
  }

  const dismissNote = () => {
    save({ [NOTE_KEY]: '1' })
  }

  // Kept short — the pill floats with the draggable mic, so a terse label reads
  // cleaner than a full sentence.
  const status = speaking
    ? 'speaking…'
    : busy
      ? 'working…'
      : listening
        ? 'listening…'
        : voiceModeOff
          ? 'voice mode off · tap to talk'
          : 'tap to talk · drag to move'

  return (
    <div style={{ flex: 1, display: 'flex', minWidth: 0, overflow: 'hidden' }}>
      {/* panelRef stays on THIS column, not the row: it is the mic's drag
        bounds, so keeping it here is what stops the floating mic from parking
        underneath the context panel. */}
      <div
        ref={panelRef}
        style={{
          flex: 1,
          display: 'flex',
          flexDirection: 'column',
          minWidth: 0,
          overflow: 'hidden',
          position: 'relative',
        }}
      >
        {needsOnboarding && <WakeWordOnboarding save={save} onDone={() => {}} />}

        {/* drifting gradient mesh — one continuous surface behind the thread */}
        <div
          aria-hidden
          style={{
            position: 'absolute',
            inset: 0,
            background: 'var(--grad-mesh)',
            opacity: 0.7,
            pointerEvents: 'none',
            animation: 'mesh-drift 18s ease-in-out infinite',
            zIndex: 0,
          }}
        />

        <VoiceHeader
          connected={connected}
          usage={usage}
          ctxOpen={ctxOpen}
          onToggleContext={() => setCtxOpen(!ctxOpen)}
          onNewSession={startNew}
        />

        {(!wakeOn || !ttsOn) && <VoiceModeNotice wakeOn={wakeOn} voiceModeOff={voiceModeOff} />}

        {wakeOn && !noteDismissed && (
          <WakeWordNote onOpenSettings={onOpenSettings} onDismiss={dismissNote} />
        )}

        {/* transcript / conversation thread — fills the panel; the mic floats over
          it (draggable) so nothing is permanently reserved or covered */}
        <div
          ref={scrollRef}
          style={{
            flex: 1,
            overflowY: 'auto',
            padding: '14px 24px 100px',
            position: 'relative',
            zIndex: 1,
            display: 'flex',
            flexDirection: 'column',
            gap: 12,
          }}
        >
          {messages.length === 0 && <EmptyVoiceThread />}
          {messages.map((m, i) => (
            <VoiceBubble
              key={m.id}
              m={m}
              playing={playingId === m.id}
              onReplay={replay}
              noticeActions={liveActions(messages, i, busy)}
              onNoticeAction={onNoticeAction}
            />
          ))}

          {/* The inline ask, and the only place one renders in this view. It
            arrives on this conversation's own channel, so it belongs here by
            construction; anything raised elsewhere reaches the user through the
            Inbox and the overlay instead. Shared card, so the consent scopes
            (approve / session / project) survive here too. */}
          {pendingAsk && (
            <AskCard
              ask={pendingAsk}
              onAnswer={(ask, response) => answerAsk(response, ask?.ask_id)}
            />
          )}
        </div>

        {/* draggable floating mic — lives over the thread, movable anywhere so it
          never blocks a chart/answer */}
        <FloatingMic
          boundsRef={panelRef}
          listening={listening}
          speaking={speaking}
          busy={busy}
          getMicAnalyser={getMicAnalyser}
          status={status}
          onToggle={() => {
            // Either way this is now a mic the USER owns, so a voice-mode flip
            // won't yank it out from under them.
            autoStarted.current = false
            if (listening) stopVoice()
            else startVoice()
          }}
          onStop={interrupt}
        />
      </div>
      {ctxOpen && (
        <>
          <ResizeHandle onMouseDown={startCtxDrag} side="right" />
          <div
            style={{
              width: ctxW,
              minWidth: CTX_MIN_W,
              maxWidth: CTX_MAX_W,
              flexShrink: 0,
              borderLeft: '1px solid var(--border-subtle)',
              overflow: 'hidden',
              transition: ctxDragging ? 'none' : 'width 0.15s ease',
            }}
          >
            <ContextAside usage={usage} />
          </div>
        </>
      )}
    </div>
  )
}
