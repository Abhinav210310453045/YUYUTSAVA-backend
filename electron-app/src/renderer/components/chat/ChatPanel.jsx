import React, { useEffect, useRef, useState } from 'react'
import { commandAction, liveActions } from '../../conversations/noticeActions'
import { useConverse } from '../../hooks/useConverse'
import AskCard from '../asks/AskCard'
import ResizeHandle from '../common/ResizeHandle'
import ChatComposer from './ChatComposer'
import ChatHeader from './ChatHeader'
import ContextAside from './ContextAside'
import ContextChips from './ContextChips'
import { Bubble, EmptyThread, JumpToLatest } from './ThreadItems'
import { CTX_MAX_W, CTX_MIN_W, useContextColumn } from './useContextColumn'
import { useFollowOutput } from './useFollowOutput'

export default function ChatPanel({
  resumeId = null,
  active = true,
  agent = null,
  card = null,
  origin = 'ui',
  title = 'Chat — orchestrator',
  placeholder = 'message the orchestrator (Enter to send, Shift+Enter for newline)',
  emptyGlyph = '◈',
  emptyHint = '> talk to YUYUTSAVA — it can run tasks, make visuals, and delegate',
  showVoice = true,
  showNewSession = true,
  onTurnEnd = null,
  // { text, ts }: appends text into the composer draft. The ts nonce makes a
  // repeat selection retrigger; the user still reviews and hits send. Used by
  // the card view's "Ask Tinker about selection" handoff.
  draftSeed = null,
  // Selection-context chips (all optional — inert for Chat/Voice surfaces):
  // contextChips = [{ key, label, accent? }] rendered as removable pills above
  // the composer; the next send carries buildContext() invisibly and then
  // onChipsConsumed() fires so the host clears its selection.
  contextChips = null,
  onRemoveChip = null,
  onClearChips = null,
  buildContext = null,
  onChipsConsumed = null,
  // Extra controls the host renders on the right side of the header (e.g.
  // the card view's chat-history dropdown + New-chat button).
  headerActions = null,
  // Fires with the server hello ({ session_id, thread_id, resuming, … }) so a
  // host that drives resumeId can learn the id of a freshly minted session.
  onSessionChange = null,
  // Identity of the conversation in the shared store (conversations/sessions.js),
  // which is what survives this component being unmounted. Defaults to
  // origin/agent/card/resumeId; a host that can show two different chats at
  // the same coordinates (the TODO card view's "New chat", which has no
  // resumeId yet) must pass its own so they don't collide.
  sessionKey = null,
  // Show the live context/spend column. Off where the chat is already a
  // sidebar itself (the TODO card view's tinker aside) — a panel inside a
  // panel has no room to be read.
  showContext = false,
  // What "New chat" means here — a notice's New-chat action and a typed "/new".
  // Defaults to starting a fresh conversation in place; the TODO card's tinker
  // pane passes its own, which opens a new chat on the card.
  onNewChat = null,
}) {
  const {
    messages,
    connected,
    busy,
    pendingAsks,
    hello,
    listening,
    speaking,
    playingId,
    paused,
    usage,
    queued,
    send,
    answerAsk,
    interrupt,
    startVoice,
    stopVoice,
    replay,
    togglePause,
    newSession,
    sendNow,
    discardQueued,
    retry,
  } = useConverse({ origin, resumeId, agent, card, sessionKey, active })
  const [draft, setDraft] = useState('')
  const [fb, setFb] = useState({}) // messageId -> 'up' | 'down' (local selection)
  const wasBusyRef = useRef(false)

  const { ctxOpen, setCtxOpen, ctxW, ctxDragging, startCtxDrag } = useContextColumn(origin)

  const { scrollRef, atBottom, onScroll, jumpToLatest } = useFollowOutput([messages, pendingAsks])

  // Notify the host view when a turn finishes (busy true → false).
  useEffect(() => {
    if (wasBusyRef.current && !busy) onTurnEnd?.()
    wasBusyRef.current = busy
  }, [busy, onTurnEnd])

  // Panel stays mounted when hidden — don't leave the push-to-talk mic hot.
  useEffect(() => {
    if (!active && listening) stopVoice()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active])

  // Seed the composer from the host view (selection → Ask Tinker). Appends
  // rather than replaces so an in-progress draft is never clobbered.
  useEffect(() => {
    if (draftSeed?.text) setDraft((d) => (d ? `${d}\n${draftSeed.text}` : draftSeed.text))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draftSeed?.ts])

  // Surface the live session id to the host once the server hello lands.
  useEffect(() => {
    if (hello?.session_id) onSessionChange?.(hello)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hello?.session_id])

  // A notice's actions, and the same actions typed as the REPL's commands.
  const onNoticeAction = (id) => {
    if (id === 'retry') retry()
    else if (id === 'new_chat') (onNewChat || newSession)()
  }

  const onSubmit = () => {
    const typed = commandAction(draft)
    if (typed) {
      onNoticeAction(typed)
      setDraft('')
      return
    }
    const hasChips = contextChips && contextChips.length > 0
    const ok = send(draft, {
      context: hasChips && buildContext ? buildContext() : undefined,
    })
    if (!ok) return // keep draft + chips — the frame never left (disconnected/busy)
    setDraft('')
    if (hasChips) onChipsConsumed?.()
  }
  const onKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      onSubmit()
    }
  }

  // Text of the user turn immediately preceding message index i (for feedback
  // snapshot + regenerate).
  const precedingUserText = (i) => {
    for (let j = i - 1; j >= 0; j--) if (messages[j].role === 'user') return messages[j].text
    return ''
  }

  return (
    <div style={{ flex: 1, display: 'flex', minWidth: 0, overflow: 'hidden' }}>
      <div
        style={{
          flex: 1,
          display: 'flex',
          flexDirection: 'column',
          minWidth: 0,
          overflow: 'hidden',
          position: 'relative',
        }}
      >
        <ChatHeader
          connected={connected}
          title={title}
          headerActions={headerActions}
          showNewSession={showNewSession}
          showContext={showContext}
          usage={usage}
          ctxOpen={ctxOpen}
          onToggleContext={() => setCtxOpen(!ctxOpen)}
          onNewSession={newSession}
        />

        {/* animated gradient mesh behind the thread */}
        <div
          aria-hidden
          style={{
            position: 'absolute',
            inset: 0,
            top: 50,
            background: 'var(--grad-mesh)',
            opacity: 0.7,
            pointerEvents: 'none',
            animation: 'mesh-drift 18s ease-in-out infinite',
            zIndex: 0,
          }}
        />

        {/* messages */}
        <div
          ref={scrollRef}
          onScroll={onScroll}
          style={{
            flex: 1,
            overflowY: 'auto',
            padding: '20px 24px',
            position: 'relative',
            zIndex: 1,
            display: 'flex',
            flexDirection: 'column',
            gap: 12,
          }}
        >
          {messages.length === 0 && <EmptyThread glyph={emptyGlyph} hint={emptyHint} />}
          {messages.map((m, i) => (
            <Bubble
              key={m.id}
              m={{ ...m, feedback: fb[m.id] }}
              userText={precedingUserText(i)}
              sessionId={hello?.session_id || null}
              onRegenerate={
                m.role === 'assistant' && !busy ? () => send(precedingUserText(i)) : null
              }
              onFeedback={(rating) => setFb((p) => ({ ...p, [m.id]: rating }))}
              playing={playingId === m.id}
              paused={playingId === m.id && paused}
              onReplay={replay}
              onTogglePause={togglePause}
              noticeActions={liveActions(messages, i, busy)}
              onNoticeAction={onNoticeAction}
            />
          ))}

          {/* The ask block, and the ONLY place an ask renders inline. It arrives
            on this conversation's own channel, so it is this conversation's by
            construction — an ask raised anywhere else reaches the user through
            the Inbox and the overlay instead, never through someone's open
            chat. Same card as those surfaces, so the option set (including the
            approve/session/project consent scopes) is identical everywhere. */}
          {/* Every pending question, oldest first.
            The wrapper is load-bearing: AskCard sets `overflow: hidden`, which
            per CSS flexbox makes its automatic minimum size 0, so in a long
            thread flexbox took all the negative free space out of the card and
            crushed it to an invisible sliver — the reason a question could
            only be answered from the Inbox. Its bubble siblings have visible
            overflow and refuse to shrink, so the card was the only victim.
            overflow: visible + flexShrink: 0 is exactly what ProposalsPanel
            does, which is why the same card behaves there. */}
          {/* jump back to live, when the user has scrolled away */}
          {!atBottom && <JumpToLatest onClick={jumpToLatest} />}

          {pendingAsks.map((ask) => (
            <div
              key={ask.ask_id || 'ask'}
              style={{ display: 'flex', flexDirection: 'column', gap: 4, flexShrink: 0 }}
            >
              <AskCard ask={ask} onAnswer={(_ask, response) => answerAsk(response, ask.ask_id)} />
            </div>
          ))}
        </div>

        {/* selection-context chips — what the next message will be scoped to */}
        <ContextChips
          chips={contextChips}
          onRemoveChip={onRemoveChip}
          onClearChips={onClearChips}
        />

        <ChatComposer
          draft={draft}
          setDraft={setDraft}
          onKeyDown={onKeyDown}
          onSubmit={onSubmit}
          busy={busy}
          placeholder={placeholder}
          queued={queued}
          sendNow={sendNow}
          discardQueued={discardQueued}
          showVoice={showVoice}
          listening={listening}
          speaking={speaking}
          startVoice={startVoice}
          stopVoice={stopVoice}
          interrupt={interrupt}
        />
      </div>
      {showContext && ctxOpen && (
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
