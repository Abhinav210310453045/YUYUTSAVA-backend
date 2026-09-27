import React, { useEffect, useState, useCallback, useRef } from 'react'
import { getTodo, patchTodo, listTodoEvents } from '../../api/client'
import ArtifactModal from '../artifacts/ArtifactModal'
import ThinkBoard from './ThinkBoard'
import AttachmentsDrawer from './AttachmentsDrawer'
import CardHeader from './card/CardHeader'
import CardMeta from './card/CardMeta'
import TinkerPane from './card/TinkerPane'
import { useCardAttachments } from './card/useCardAttachments'
import { useCardSelection } from './card/useCardSelection'
import { useCollapsedObjectives } from './card/useCollapsedObjectives'
import { useViewState } from '../../nav/useViewState'

export default function TodoCardView({ cardId }) {
  // Everything the user can *see* about where they are inside a card is keyed
  // on the card, not on the route — so switching tinker chats (a param change)
  // leaves the drawers, selection and pane layout exactly as they were.
  const scope = `todos/card/${cardId}`

  const [card, setCard] = useState(null)
  const [error, setError] = useState(null)
  // Title is a live edit buffer: kept across navigation, and refresh() leaves
  // it alone while it's dirty so an uncommitted rename isn't clobbered.
  const [title, setTitle] = useViewState('title', '', scope)
  const [titleDirty, setTitleDirty] = useViewState('titleDirty', false, scope)
  const [patching, setPatching] = useState(false)
  // "Think with TinkerAgent" split: when open, the content area becomes
  // board | chat. The chat is the shared ChatPanel pointed at agent=tinker —
  // a card can hold many chats; the pane opens on the most recent one.
  const [thinkOpen, setThinkOpen] = useViewState('thinkOpen', false, scope)
  const [expandedAtt, setExpandedAtt] = useViewState('expandedAtt', null, scope) // attachment shown big
  const [attOpen, setAttOpen] = useViewState('attOpen', false, scope) // bottom drawer, closed by default
  const [activityOpen, setActivityOpen] = useViewState('activityOpen', false, scope)
  const [events, setEvents] = useState([])
  const [collapsedObjectives, onToggleCollapse] = useCollapsedObjectives(cardId, card)
  const {
    selectMode,
    selected,
    toggleSelect,
    toggleSelectMode,
    contextChips,
    buildSelectionContext,
    onRemoveChip,
    onClearChips,
    onChipsConsumed,
  } = useCardSelection(card, scope, setThinkOpen)

  // Esc sets this before blurring: the blur handler must skip the commit
  // because the reverted title state hasn't re-rendered into its closure yet.
  const revertingRef = useRef(false)
  // refresh() is called from callbacks that close over stale state; read the
  // dirty flag through a ref so it never reverts an edit in progress.
  const titleDirtyRef = useRef(titleDirty)
  titleDirtyRef.current = titleDirty
  // True while a board item is mid-drag — refreshes are skipped so an
  // optimistic move can't be clobbered by a stale server card.
  const draggingRef = useRef(false)

  const loadEvents = useCallback(async () => {
    try {
      setEvents(await listTodoEvents(cardId))
    } catch {
      /* strip is best-effort */
    }
  }, [cardId])

  const refresh = useCallback(async () => {
    if (draggingRef.current) return
    try {
      const c = await getTodo(cardId)
      setCard(c)
      if (!titleDirtyRef.current) setTitle(c.title)
      setError(null)
    } catch (e) {
      setError(e.message)
    }
  }, [cardId, setTitle])

  useEffect(() => {
    refresh()
  }, [refresh])
  useEffect(() => {
    if (activityOpen) loadEvents()
  }, [activityOpen, loadEvents])

  // The Journey document opens big as soon as it is on the card.
  const { uploading, onFiles, onAttachmentDeleted, journeyBusy, generateJourney } =
    useCardAttachments(cardId, setCard, (att) => {
      setExpandedAtt(att)
      if (activityOpen) loadEvents()
    })

  // A tinker turn may have written objectives/notes/events — re-pull both.
  const onTinkerTurnEnd = useCallback(() => {
    refresh()
    if (activityOpen) loadEvents()
  }, [refresh, loadEvents, activityOpen])

  // Shared partial-update path: PATCH, then swap in the returned card (the
  // response is the fully hydrated TodoCardV1, notes included).
  const patch = useCallback(
    async (fields) => {
      setPatching(true)
      try {
        const c = await patchTodo(cardId, fields)
        setCard(c)
        setTitle(c.title)
        setTitleDirty(false)
      } catch (e) {
        alert(`Update failed: ${e.message}`)
      } finally {
        setPatching(false)
      }
    },
    [cardId, setTitle, setTitleDirty],
  )

  const onTitleCommit = () => {
    if (revertingRef.current) {
      revertingRef.current = false
      setTitle(card?.title || '')
      setTitleDirty(false)
      return
    }
    const t = title.trim()
    if (!card || !t || t === card.title) {
      setTitle(card?.title || '')
      setTitleDirty(false)
      return
    }
    patch({ title: t })
  }

  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
      <CardHeader
        cardId={cardId}
        card={card}
        onCardChange={setCard}
        title={title}
        onTitleChange={(t) => {
          setTitle(t)
          setTitleDirty(true)
        }}
        onTitleCommit={onTitleCommit}
        onTitleRevert={() => {
          revertingRef.current = true
        }}
        patching={patching}
        patch={patch}
        selectMode={selectMode}
        onToggleSelectMode={toggleSelectMode}
        journeyBusy={journeyBusy}
        onJourney={generateJourney}
        thinkOpen={thinkOpen}
        onToggleThink={() => setThinkOpen((v) => !v)}
      />

      <div style={{ flex: 1, display: 'flex', overflow: 'hidden' }}>
        <div
          style={{
            flex: 1,
            minWidth: 0,
            display: 'flex',
            flexDirection: 'column',
            overflow: 'hidden',
          }}
        >
          {error && (
            <div
              style={{
                fontFamily: 'var(--font-mono)',
                fontSize: 11,
                color: 'var(--neon-red)',
                margin: '12px 24px',
                padding: '6px 10px',
                border: '1px solid rgba(255,51,102,0.25)',
                borderRadius: 6,
                background: 'rgba(255,51,102,0.05)',
              }}
            >
              {`> card: ${error}`}
            </div>
          )}

          {!card && !error && (
            <div
              style={{
                color: 'var(--text-muted)',
                fontFamily: 'var(--font-mono)',
                fontSize: 12,
                margin: '12px 24px',
              }}
            >
              loading…
            </div>
          )}

          {card && (
            <>
              <CardMeta
                card={card}
                activityOpen={activityOpen}
                onToggleActivity={() => setActivityOpen((v) => !v)}
                events={events}
              />

              {/* ── the think board: General Notes + phase columns ── */}
              <div style={{ flex: 1, minHeight: 0, display: 'flex', padding: '12px 24px 0' }}>
                <ThinkBoard
                  cardId={cardId}
                  card={card}
                  onCardChange={setCard}
                  draggingRef={draggingRef}
                  selectMode={selectMode}
                  selected={selected}
                  onToggleSelect={toggleSelect}
                  collapsedObjectives={collapsedObjectives}
                  onToggleCollapse={onToggleCollapse}
                  onFilesDropped={onFiles}
                />
              </div>

              {/* ── attachments: bottom drawer, VS Code terminal style ── */}
              <AttachmentsDrawer
                cardId={cardId}
                attachments={card.attachments}
                open={attOpen}
                onToggle={setAttOpen}
                uploading={uploading}
                onFiles={onFiles}
                onDeleted={onAttachmentDeleted}
                onExpand={setExpandedAtt}
              />
            </>
          )}
        </div>

        {thinkOpen && card && (
          <TinkerPane
            cardId={cardId}
            scope={scope}
            onTurnEnd={onTinkerTurnEnd}
            contextChips={contextChips}
            onRemoveChip={onRemoveChip}
            onClearChips={onClearChips}
            buildSelectionContext={buildSelectionContext}
            onChipsConsumed={onChipsConsumed}
          />
        )}
      </div>

      <ArtifactModal
        attachment={expandedAtt}
        cardId={cardId}
        onClose={() => setExpandedAtt(null)}
      />
    </div>
  )
}
