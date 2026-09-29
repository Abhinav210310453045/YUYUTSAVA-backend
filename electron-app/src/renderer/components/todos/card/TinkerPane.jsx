import React, { useCallback, useEffect, useRef, useState } from 'react'
import { listTodoChats } from '../../../api/client'
import { clamp, startResize } from '../../../lib/resize'
import { useNav } from '../../../nav/NavProvider'
import { useViewState } from '../../../nav/useViewState'
import ChatPanel from '../../chat/ChatPanel'
import NewSessionButton from '../../common/NewSessionButton'
import ResizeHandle from '../../common/ResizeHandle'
import TinkerChatHistory from '../TinkerChatHistory'

// Tinker pane width bounds + persistence (localStorage) — the divider is the
// same ResizeHandle as the app's activity rail.
const TINKER_MIN = 380
const TINKER_MAX = 800
const TINKER_W_KEY = 'yy.todo.tinkerW'

// "Think with TinkerAgent": the resizable pane beside the board. The chat is
// the shared ChatPanel pointed at agent=tinker — a card can hold many chats;
// the pane opens on the most recent one.
export default function TinkerPane({
  cardId,
  scope,
  onTurnEnd,
  contextChips,
  onRemoveChip,
  onClearChips,
  buildSelectionContext,
  onChipsConsumed,
}) {
  const { params, replace } = useNav()
  // The card's tinker chats (newest first). WHICH one is showing is navigation
  // state (params.chat): undefined = not resolved yet, 'new' = fresh chat with
  // no resume, else a session id. `epoch` bumps force a ChatPanel remount.
  const [chats, setChats] = useState(null)
  const chatParam = params.chat
  const chatSelId = chatParam === undefined ? undefined : chatParam === 'new' ? null : chatParam
  const [chatEpoch, setChatEpoch] = useViewState('chatEpoch', 0, scope)
  const [liveChatId, setLiveChatId] = useState(null)
  // Same handle + drag pattern as App.jsx's activity rail.
  const [tinkerW, setTinkerW] = useState(() => {
    const v = Number(localStorage.getItem(TINKER_W_KEY))
    return v >= TINKER_MIN && v <= TINKER_MAX ? v : 480
  })
  const [tinkerDragging, setTinkerDragging] = useState(false)
  // loadChats must only auto-resolve the pane's chat when nothing has picked
  // one yet, without re-running every time the selection changes.
  const chatParamRef = useRef(chatParam)
  chatParamRef.current = chatParam

  // The card's chat sessions — resolves which chat the tinker pane opens on
  // (most recent, or fresh when the card has none). Best-effort: on failure
  // fall back to a fresh chat, which always works.
  const loadChats = useCallback(async () => {
    // Resolving the pane's chat is a lateral move, not a new depth level:
    // `replace` keeps back going card → board rather than walking back
    // through every chat that was ever opened.
    try {
      const rows = await listTodoChats(cardId)
      setChats(rows)
      if (chatParamRef.current === undefined) replace({ chat: rows[0]?.id ?? 'new' })
    } catch {
      setChats((c) => c ?? [])
      if (chatParamRef.current === undefined) replace({ chat: 'new' })
    }
  }, [cardId, replace])

  useEffect(() => {
    loadChats()
  }, [loadChats])

  const onNewChat = useCallback(() => {
    // 'new' + epoch bump remounts the ChatPanel with no resume_id → the
    // server mints a fresh todo:<card>:<ULID> session.
    setChatEpoch((n) => n + 1)
    replace({ chat: 'new' })
  }, [replace, setChatEpoch])

  const onSelectChat = useCallback(
    (s) => {
      setChatEpoch((n) => n + 1)
      replace({ chat: s.id })
    },
    [replace, setChatEpoch],
  )

  // The card view re-pulls what a tinker turn may have written; the chat list
  // is re-pulled too (the first turn sets the session's title + message count).
  const onTinkerTurnEnd = useCallback(() => {
    onTurnEnd()
    loadChats()
  }, [onTurnEnd, loadChats])

  const startTinkerDrag = useCallback(
    (e) =>
      startResize(e, {
        sizeAt: (dx) => clamp(tinkerW - dx, TINKER_MIN, TINKER_MAX),
        onSize: setTinkerW,
        onStart: () => setTinkerDragging(true),
        onEnd: (w) => {
          setTinkerDragging(false)
          localStorage.setItem(TINKER_W_KEY, String(w))
        },
      }),
    [tinkerW],
  )

  return (
    <>
      {/* Same drag-bar resize as the app's activity rail; width persists. */}
      <ResizeHandle onMouseDown={startTinkerDrag} side="right" />
      <div
        style={{
          width: tinkerW,
          minWidth: TINKER_MIN,
          maxWidth: TINKER_MAX,
          flexShrink: 0,
          borderLeft: '1px solid var(--border-subtle)',
          display: 'flex',
          flexDirection: 'column',
          overflow: 'hidden',
          transition: tinkerDragging ? 'none' : 'width 0.15s ease',
        }}
      >
        {/* The shared chat surface, unforked: agent=tinker + card routes
                      to the card's bundle; resumeId picks WHICH of the card's
                      chats (none = fresh). The key hard-remounts on chat switch so
                      composer/draft state never leaks across sessions. */}
        {chatSelId !== undefined && (
          <ChatPanel
            key={`tinker:${cardId}:${chatSelId ?? `new-${chatEpoch}`}:${chatEpoch}`}
            // The live conversation lives in the shared store, not in the
            // component — so closing this pane (or switching cards) no
            // longer ends the turn. The key above still hard-remounts the
            // panel on a chat switch so composer/draft state can't leak;
            // this one says WHICH conversation the remount reattaches to.
            // The epoch is what makes "New chat" a different conversation
            // rather than a reattach to the same unresolved one.
            sessionKey={`tinker:${cardId}:${chatSelId ?? `new-${chatEpoch}`}`}
            agent="tinker"
            card={cardId}
            origin="tinker"
            resumeId={chatSelId}
            title="Think with TinkerAgent"
            placeholder="hand the TinkerAgent a rough idea… (Enter to send)"
            emptyGlyph="✦"
            emptyHint="> tinker on this card — it sharpens ideas, asks the right questions, and keeps notes here"
            showNewSession={false}
            onNewChat={onNewChat}
            onTurnEnd={onTinkerTurnEnd}
            contextChips={contextChips}
            onRemoveChip={onRemoveChip}
            onClearChips={onClearChips}
            buildContext={buildSelectionContext}
            onChipsConsumed={onChipsConsumed}
            onSessionChange={(h) => setLiveChatId(h.session_id)}
            headerActions={
              <>
                <TinkerChatHistory
                  chats={chats}
                  activeId={liveChatId}
                  onSelect={onSelectChat}
                  onRefresh={loadChats}
                />
                <NewSessionButton label="New chat" color="var(--text-info)" onClick={onNewChat} />
              </>
            }
          />
        )}
      </div>
    </>
  )
}
