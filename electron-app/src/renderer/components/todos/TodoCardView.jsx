import React, { useEffect, useState, useCallback, useMemo, useRef } from 'react'
import {
  getTodo, patchTodo, uploadTodoAttachment,
  addTodoObjective, listTodoEvents, generateTodoArtifact, listTodoChats,
} from '../../api/client'
import { STATUS_ACCENT, PHASE_ACCENT, TagChips, PinIcon, humanAge } from './shared'
import ArtifactModal from '../artifacts/ArtifactModal'
import ChatPanel from '../chat/ChatPanel'
import NewSessionButton from '../common/NewSessionButton'
import ResizeHandle from '../common/ResizeHandle'
import ThinkBoard from './ThinkBoard'
import AttachmentsDrawer from './AttachmentsDrawer'
import TinkerChatHistory from './TinkerChatHistory'

const STATUSES = ['inbox', 'active', 'done', 'archived']

// Tinker pane width bounds + persistence (localStorage) — the divider is the
// same ResizeHandle as the app's activity rail.
const TINKER_MIN = 380
const TINKER_MAX = 800
const TINKER_W_KEY = 'yy.todo.tinkerW'
// Per-card objective collapse state — survives navigation and reloads so an
// expanded objective comes back exactly as it was left.
const collapseKey = (cardId) => `yy.todo.collapse.${cardId}`

// One line per todo_events row — the frontend twin of the journey document's
// timeline humanizer (block_journey.py).
function humanizeEvent(e) {
  const p = e.payload || {}
  const title = p.title || ''
  switch (e.kind) {
    case 'card_status': return `card moved ${p.from} → ${p.to}`
    case 'objective_created': return `objective added: “${title}” [${p.phase}]`
    case 'objective_phase': return `“${title}”: ${p.from} → ${p.to}${p.reason ? ` — ${p.reason}` : ''}`
    case 'objective_updated': return `“${title}”: ${(p.fields || []).join(', ')} changed`
    case 'objective_deleted': return `objective removed: “${title}”`
    case 'note_assigned': return 'a note was assigned to an objective'
    case 'artifact_attached': return `attached ${p.kind}${title ? `: ${title}` : ''}`
    case 'journey_generated': return 'journey document generated'
    default: return e.kind
  }
}

export default function TodoCardView({ cardId, onBack }) {
  const [card, setCard] = useState(null)
  const [error, setError] = useState(null)
  const [title, setTitle] = useState('')
  const [patching, setPatching] = useState(false)
  // "Think with TinkerAgent" split: when open, the content area becomes
  // board | chat. The chat is the shared ChatPanel pointed at agent=tinker —
  // a card can hold many chats; the pane opens on the most recent one.
  const [thinkOpen, setThinkOpen] = useState(false)
  // The card's tinker chats (newest first) + which one the pane shows.
  // chatSel.id: undefined = not resolved yet, null = fresh chat (no resume),
  // else a session id; epoch bumps force a ChatPanel remount via key.
  const [chats, setChats] = useState(null)
  const [chatSel, setChatSel] = useState({ id: undefined, epoch: 0 })
  const [liveChatId, setLiveChatId] = useState(null)
  const [uploading, setUploading] = useState(0) // in-flight upload count
  const [expandedAtt, setExpandedAtt] = useState(null) // attachment shown big
  const [attOpen, setAttOpen] = useState(false) // bottom drawer, closed by default
  // Header "+ Objective" popover input.
  const [objComposerOpen, setObjComposerOpen] = useState(false)
  const [newObjective, setNewObjective] = useState('')
  const [addingObjective, setAddingObjective] = useState(false)
  // Multi-select of notes/objectives → context chips on the tinker composer.
  const [selectMode, setSelectMode] = useState(false)
  const [selected, setSelected] = useState(() => new Set()) // 'obj:…' | 'note:…'
  const [journeyBusy, setJourneyBusy] = useState(false)
  const [activityOpen, setActivityOpen] = useState(false)
  const [events, setEvents] = useState([])
  // Objective collapse state — persisted per card, pruned to live ids on load.
  const [collapsedObjectives, setCollapsedObjectives] = useState(() => {
    try { return new Set(JSON.parse(localStorage.getItem(collapseKey(cardId)) || '[]')) } catch { return new Set() }
  })
  // Resizable tinker pane — same handle + drag pattern as App.jsx's activity rail.
  const [tinkerW, setTinkerW] = useState(() => {
    const v = Number(localStorage.getItem(TINKER_W_KEY))
    return v >= TINKER_MIN && v <= TINKER_MAX ? v : 480
  })
  const [tinkerDragging, setTinkerDragging] = useState(false)

  const titleRef = useRef(null)
  // Esc sets this before blurring: the blur handler must skip the commit
  // because the reverted title state hasn't re-rendered into its closure yet.
  const revertingRef = useRef(false)
  // True while a board item is mid-drag — refreshes are skipped so an
  // optimistic move can't be clobbered by a stale server card.
  const draggingRef = useRef(false)

  const loadEvents = useCallback(async () => {
    try { setEvents(await listTodoEvents(cardId)) } catch { /* strip is best-effort */ }
  }, [cardId])

  const refresh = useCallback(async () => {
    if (draggingRef.current) return
    try {
      const c = await getTodo(cardId)
      setCard(c)
      setTitle(c.title)
      setError(null)
    } catch (e) {
      setError(e.message)
    }
  }, [cardId])

  useEffect(() => { refresh() }, [refresh])
  useEffect(() => { if (activityOpen) loadEvents() }, [activityOpen, loadEvents])

  // Prune collapse state to objectives that still exist once the card loads.
  useEffect(() => {
    if (!card) return
    setCollapsedObjectives((s) => {
      const live = new Set(card.objectives.map((o) => o.objective_id))
      const next = new Set([...s].filter((id) => live.has(id)))
      if (next.size === s.size) return s
      try { localStorage.setItem(collapseKey(cardId), JSON.stringify([...next])) } catch { /* quota */ }
      return next
    })
  }, [card, cardId])

  const onToggleCollapse = useCallback((objectiveId) => {
    setCollapsedObjectives((s) => {
      const next = new Set(s)
      if (next.has(objectiveId)) next.delete(objectiveId); else next.add(objectiveId)
      try { localStorage.setItem(collapseKey(cardId), JSON.stringify([...next])) } catch { /* quota */ }
      return next
    })
  }, [cardId])

  // The card's chat sessions — resolves which chat the tinker pane opens on
  // (most recent, or fresh when the card has none). Best-effort: on failure
  // fall back to a fresh chat, which always works.
  const loadChats = useCallback(async () => {
    try {
      const rows = await listTodoChats(cardId)
      setChats(rows)
      setChatSel((s) => (s.id === undefined ? { id: rows[0]?.id ?? null, epoch: s.epoch } : s))
    } catch {
      setChats((c) => c ?? [])
      setChatSel((s) => (s.id === undefined ? { id: null, epoch: s.epoch } : s))
    }
  }, [cardId])

  useEffect(() => { if (thinkOpen) loadChats() }, [thinkOpen, loadChats])

  const onNewChat = useCallback(() => {
    // null id + epoch bump remounts the ChatPanel with no resume_id → the
    // server mints a fresh todo:<card>:<ULID> session.
    setChatSel((s) => ({ id: null, epoch: s.epoch + 1 }))
  }, [])

  const onSelectChat = useCallback((s) => {
    setChatSel((p) => ({ id: s.id, epoch: p.epoch + 1 }))
  }, [])

  // A tinker turn may have written objectives/notes/events — re-pull both;
  // the chat list too (first turn sets the session's title + message count).
  const onTinkerTurnEnd = useCallback(() => {
    refresh()
    if (activityOpen) loadEvents()
    loadChats()
  }, [refresh, loadEvents, activityOpen, loadChats])

  const startTinkerDrag = useCallback((e) => {
    e.preventDefault()
    const startX = e.clientX
    const startW = tinkerW
    setTinkerDragging(true)
    let w = startW
    const onMove = (ev) => {
      w = Math.min(TINKER_MAX, Math.max(TINKER_MIN, startW - (ev.clientX - startX)))
      setTinkerW(w)
    }
    const onUp = () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      setTinkerDragging(false)
      localStorage.setItem(TINKER_W_KEY, String(w))
    }
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
  }, [tinkerW])

  // Bounded so the reference block the tinker receives stays reviewable.
  const MAX_SELECTION = 20
  const toggleSelect = useCallback((key) => {
    setSelected((s) => {
      const next = new Set(s)
      if (next.has(key)) next.delete(key)
      else if (next.size < MAX_SELECTION) next.add(key)
      else return s // cap reached — additional checks are ignored
      return next
    })
    // Chips live on the tinker composer — surface it as soon as a selection
    // starts so the context is visible where it acts.
    setThinkOpen(true)
  }, [])

  // Selection → composer chips: one pill per selected objective/note.
  const contextChips = useMemo(() => {
    if (!card || selected.size === 0) return null
    const chips = []
    for (const o of card.objectives) {
      if (selected.has(`obj:${o.objective_id}`)) {
        chips.push({
          key: `obj:${o.objective_id}`,
          label: `◆ ${o.title}`,
          title: `objective “${o.title}” [${o.phase}]`,
          accent: PHASE_ACCENT[o.phase] || PHASE_ACCENT.abandoned,
        })
      }
    }
    for (const n of card.notes) {
      if (selected.has(`note:${n.note_id}`)) {
        const excerpt = n.body.replace(/\n/g, ' ')
        chips.push({
          key: `note:${n.note_id}`,
          label: `✎ ${excerpt.length > 30 ? `${excerpt.slice(0, 30)}…` : excerpt}`,
          title: `note by ${n.author}`,
        })
      }
    }
    return chips
  }, [card, selected])

  // The invisible reference block that rides the next tinker message — the
  // tinker prompt knows these stable-id lines as its scope for the turn.
  const buildSelectionContext = useCallback(() => {
    if (!card || selected.size === 0) return ''
    const lines = []
    for (const o of card.objectives) {
      if (selected.has(`obj:${o.objective_id}`)) {
        lines.push(`[objective ${o.objective_id} "${o.title}" phase=${o.phase}]`)
      }
    }
    for (const n of card.notes) {
      if (selected.has(`note:${n.note_id}`)) {
        const excerpt = n.body.length > 120 ? `${n.body.slice(0, 120)}…` : n.body
        lines.push(`[note ${n.note_id} by ${n.author}] "${excerpt.replace(/\n/g, ' ')}"`)
      }
    }
    return lines.join('\n')
  }, [card, selected])

  const onRemoveChip = useCallback((key) => {
    setSelected((s) => {
      const next = new Set(s)
      next.delete(key)
      return next
    })
  }, [])

  const onClearChips = useCallback(() => setSelected(new Set()), [])

  // Chips are one-shot: the block is in the thread after a successful send —
  // keeping them would silently re-attach stale context to later turns.
  const onChipsConsumed = useCallback(() => setSelected(new Set()), [])

  const onAddObjective = async () => {
    const t = newObjective.trim()
    if (!t || addingObjective) return
    setAddingObjective(true)
    try {
      const obj = await addTodoObjective(cardId, t)
      setCard((c) => (c ? { ...c, objectives: [...c.objectives, obj] } : c))
      setNewObjective('')
      setObjComposerOpen(false)
      // New objectives land in thinking — bring that column into view.
      requestAnimationFrame(() => {
        document.getElementById('yy-phase-thinking')?.scrollIntoView({ behavior: 'smooth', inline: 'nearest', block: 'nearest' })
      })
    } catch (e) {
      alert(`Add objective failed: ${e.message}`)
    } finally {
      setAddingObjective(false)
    }
  }

  // "Journey of the plan": compile the think flow into an HTML artifact and
  // open it big immediately; it also lands as a tile in the drawer below.
  // Journey is a singleton block — regeneration returns the SAME attachment
  // id with fresh content, so upsert the tile instead of appending a twin.
  const onJourney = async () => {
    if (journeyBusy) return
    setJourneyBusy(true)
    try {
      const att = await generateTodoArtifact(cardId, 'journey')
      setCard((c) => {
        if (!c) return c
        const exists = c.attachments.some((a) => a.attachment_id === att.attachment_id)
        return {
          ...c,
          attachments: exists
            ? c.attachments.map((a) => (a.attachment_id === att.attachment_id ? att : a))
            : [...c.attachments, att],
        }
      })
      setExpandedAtt(att)
      if (activityOpen) loadEvents()
    } catch (e) {
      alert(`Journey failed: ${e.message}`)
    } finally {
      setJourneyBusy(false)
    }
  }

  // Shared partial-update path: PATCH, then swap in the returned card (the
  // response is the fully hydrated TodoCardV1, notes included).
  const patch = useCallback(async (fields) => {
    setPatching(true)
    try {
      const c = await patchTodo(cardId, fields)
      setCard(c)
      setTitle(c.title)
    } catch (e) {
      alert(`Update failed: ${e.message}`)
    } finally {
      setPatching(false)
    }
  }, [cardId])

  const onTitleCommit = () => {
    if (revertingRef.current) { revertingRef.current = false; setTitle(card?.title || ''); return }
    const t = title.trim()
    if (!card || !t || t === card.title) { setTitle(card?.title || ''); return }
    patch({ title: t })
  }

  // Shared by every drop target on the board and the drawer: upload each file
  // (with its objective/note association when dropped onto one), appending the
  // returned row; a per-file failure reports and moves on to the next.
  const onFiles = useCallback(async (files, assoc = {}) => {
    for (const file of Array.from(files || [])) {
      setUploading((n) => n + 1)
      try {
        const att = await uploadTodoAttachment(cardId, file, assoc)
        setCard((c) => (c ? { ...c, attachments: [...c.attachments, att] } : c))
      } catch (e) {
        alert(`Upload of ${file.name} failed: ${e.message}`)
      } finally {
        setUploading((n) => n - 1)
      }
    }
  }, [cardId])

  const onAttachmentDeleted = useCallback((attachmentId) => {
    setCard((c) => (c ? {
      ...c,
      attachments: c.attachments.filter((a) => a.attachment_id !== attachmentId),
    } : c))
  }, [])

  const accent = STATUS_ACCENT[card?.status] || STATUS_ACCENT.inbox
  const doneCount = card ? card.objectives.filter((o) => o.phase === 'completed').length : 0

  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
      <div style={{
        display: 'flex', alignItems: 'center', gap: 10,
        padding: '14px 24px', borderBottom: '1px solid var(--border-subtle)', background: 'var(--bg-bar)',
      }}>
        <button
          onClick={onBack}
          title="back to board"
          style={{
            fontFamily: 'var(--font-mono)', fontSize: 11, padding: '5px 12px',
            background: 'transparent', color: 'var(--text-muted)',
            border: '1px solid var(--border-card)', borderRadius: 6, cursor: 'pointer',
          }}
        >
          ← Board
        </button>

        {card && (
          <>
            {/* Renamable title — commit on Enter or blur, Esc reverts. */}
            <input
              ref={titleRef}
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              onBlur={onTitleCommit}
              onKeyDown={(e) => {
                if (e.key === 'Enter') titleRef.current?.blur()
                if (e.key === 'Escape') { revertingRef.current = true; titleRef.current?.blur() }
              }}
              disabled={patching}
              title="click to rename"
              style={{
                flex: 1, minWidth: 0,
                background: 'transparent', color: 'var(--text-primary)',
                border: '1px solid transparent', borderRadius: 6, padding: '5px 10px',
                fontFamily: 'var(--font-mono)', fontSize: 13, fontWeight: 'var(--fw-semibold)',
                outline: 'none',
              }}
              onFocus={(e) => { e.target.style.border = '1px solid var(--border-card)'; e.target.style.background = 'var(--bg-card)' }}
              onBlurCapture={(e) => { e.target.style.border = '1px solid transparent'; e.target.style.background = 'transparent' }}
            />

            {doneCount > 0 || card.objectives.length > 0 ? (
              <span style={{
                fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--neon-green)',
                background: 'rgba(var(--accent-rgb),0.08)', border: '1px solid rgba(var(--accent-rgb),0.2)',
                borderRadius: 10, padding: '1px 7px', whiteSpace: 'nowrap',
              }}>
                {doneCount}/{card.objectives.length}
              </span>
            ) : null}

            <button
              onClick={() => patch({ pinned: !card.pinned })}
              disabled={patching}
              title={card.pinned ? 'unpin' : 'pin to top'}
              style={{
                width: 28, height: 28, borderRadius: 6,
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                background: card.pinned ? 'rgba(250,204,21,0.10)' : 'transparent',
                border: `1px solid ${card.pinned ? 'rgba(250,204,21,0.3)' : 'var(--border-card)'}`,
                cursor: 'pointer',
              }}
            >
              <PinIcon color={card.pinned ? 'var(--text-warning)' : 'var(--text-muted)'} />
            </button>

            <select
              value={card.status}
              onChange={(e) => patch({ status: e.target.value })}
              disabled={patching}
              title="card status"
              style={{
                background: 'var(--bg-card)', color: accent.bar,
                border: `1px solid ${accent.border}`, borderRadius: 6, padding: '5px 8px',
                fontFamily: 'var(--font-mono)', fontSize: 11, cursor: 'pointer',
              }}
            >
              {STATUSES.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>

            {/* "+ Objective" popover — new objectives start in thinking. */}
            {objComposerOpen ? (
              <input
                autoFocus
                value={newObjective}
                onChange={(e) => setNewObjective(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') onAddObjective()
                  if (e.key === 'Escape') { setObjComposerOpen(false); setNewObjective('') }
                }}
                onBlur={() => { if (!newObjective.trim()) setObjComposerOpen(false) }}
                placeholder="objective title… (Enter)"
                disabled={addingObjective}
                style={{
                  width: 220, background: 'var(--bg-card)', color: 'var(--text-primary)',
                  border: '1px solid rgba(167,139,250,0.4)', borderRadius: 6,
                  padding: '5px 10px', fontFamily: 'var(--font-mono)', fontSize: 11,
                  outline: 'none',
                }}
              />
            ) : (
              <button
                onClick={() => setObjComposerOpen(true)}
                title="add an objective (a small, checkable step) — starts in thinking"
                style={{
                  fontFamily: 'var(--font-mono)', fontSize: 11, padding: '5px 12px',
                  background: 'rgba(167,139,250,0.10)', color: 'var(--text-lavender)',
                  border: '1px solid rgba(167,139,250,0.3)', borderRadius: 6,
                  cursor: 'pointer', whiteSpace: 'nowrap',
                }}
              >
                + Objective
              </button>
            )}

            {/* Multi-select of notes/objectives → a reference block seeded
                into the tinker chat. The count button appears once armed. */}
            <button
              onClick={() => { setSelectMode((v) => !v); setSelected(new Set()) }}
              title={selectMode ? 'exit selection' : 'select notes/objectives to ask the TinkerAgent about'}
              style={{
                fontFamily: 'var(--font-mono)', fontSize: 11, padding: '5px 12px',
                background: selectMode ? 'rgba(196,181,253,0.16)' : 'transparent',
                color: selectMode ? 'var(--text-lavender)' : 'var(--text-muted)',
                border: `1px solid ${selectMode ? 'rgba(167,139,250,0.4)' : 'var(--border-card)'}`,
                borderRadius: 6, cursor: 'pointer', whiteSpace: 'nowrap',
              }}
            >
              {selectMode ? '✕ Select' : '☐ Select'}
            </button>

            <button
              onClick={onJourney}
              disabled={journeyBusy}
              title="compile this card's journey — objectives, notes, and timeline — into a document"
              style={{
                fontFamily: 'var(--font-mono)', fontSize: 11, padding: '5px 12px',
                background: 'rgba(250,204,21,0.08)', color: 'var(--text-warning)',
                border: '1px solid rgba(250,204,21,0.3)', borderRadius: 6,
                cursor: journeyBusy ? 'default' : 'pointer',
                opacity: journeyBusy ? 0.6 : 1, whiteSpace: 'nowrap',
              }}
            >
              {journeyBusy ? '… compiling' : '📜 Journey'}
            </button>

            <button
              onClick={() => setThinkOpen((v) => !v)}
              title={thinkOpen ? 'close the TinkerAgent chat' : 'think on this card with the TinkerAgent'}
              style={{
                fontFamily: 'var(--font-mono)', fontSize: 11, padding: '5px 12px',
                background: thinkOpen ? 'rgba(120,160,255,0.18)' : 'rgba(120,160,255,0.06)',
                color: 'var(--text-info)',
                border: `1px solid rgba(120,160,255,${thinkOpen ? 0.5 : 0.3})`,
                borderRadius: 6, cursor: 'pointer', whiteSpace: 'nowrap',
              }}
            >
              {thinkOpen ? '✕ Tinker' : '✦ Tinker'}
            </button>
          </>
        )}
      </div>

      <div style={{ flex: 1, display: 'flex', overflow: 'hidden' }}>
        <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
          {error && (
            <div style={{
              fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--neon-red)',
              margin: '12px 24px', padding: '6px 10px',
              border: '1px solid rgba(255,51,102,0.25)',
              borderRadius: 6, background: 'rgba(255,51,102,0.05)',
            }}>
              {`> card: ${error}`}
            </div>
          )}

          {!card && !error && (
            <div style={{ color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', fontSize: 12, margin: '12px 24px' }}>
              loading…
            </div>
          )}

          {card && (
            <>
              {/* Meta strip: tags, timestamps, activity toggle. */}
              <div style={{
                display: 'flex', alignItems: 'center', gap: 12,
                padding: '10px 24px 0', fontFamily: 'var(--font-mono)', fontSize: 11,
                color: 'var(--text-muted)', flexShrink: 0,
              }}>
                <TagChips tags={card.tags} />
                <span style={{ flex: 1 }} />
                <button
                  onClick={() => setActivityOpen((v) => !v)}
                  style={{
                    fontFamily: 'var(--font-mono)', fontSize: 10, padding: '2px 8px',
                    background: 'transparent', color: 'var(--text-muted)',
                    border: '1px solid var(--border-card)', borderRadius: 6, cursor: 'pointer',
                  }}
                >
                  {activityOpen ? '▾ Activity' : '▸ Activity'}
                </button>
                <span>created {humanAge(card.created_ts)}</span>
                <span style={{ color: 'var(--text-dim)' }}>·</span>
                <span>updated {humanAge(card.updated_ts)}</span>
              </div>

              {activityOpen && (
                <div style={{
                  display: 'flex', flexDirection: 'column', gap: 4,
                  fontFamily: 'var(--font-mono)', fontSize: 11,
                  borderLeft: '2px solid var(--border-card)',
                  margin: '8px 24px 0', paddingLeft: 12,
                  maxHeight: 140, overflowY: 'auto', flexShrink: 0,
                }}>
                  {events.length === 0 && (
                    <span style={{ color: 'var(--text-dim)' }}>{'> no activity recorded yet'}</span>
                  )}
                  {events.map((e) => (
                    <div key={e.event_id} style={{ display: 'flex', gap: 8, color: 'var(--text-muted)' }}>
                      <span style={{ color: 'var(--text-dim)', whiteSpace: 'nowrap' }}>{humanAge(e.created_ts)}</span>
                      <span style={{ color: 'var(--text-info)' }}>[{e.actor}]</span>
                      <span style={{ minWidth: 0, wordBreak: 'break-word' }}>{humanizeEvent(e)}</span>
                    </div>
                  ))}
                </div>
              )}

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
          <>
            {/* Same drag-bar resize as the app's activity rail; width persists. */}
            <ResizeHandle onMouseDown={startTinkerDrag} side="right" />
            <div style={{
              width: tinkerW, minWidth: TINKER_MIN, maxWidth: TINKER_MAX, flexShrink: 0,
              borderLeft: '1px solid var(--border-subtle)',
              display: 'flex', flexDirection: 'column', overflow: 'hidden',
              transition: tinkerDragging ? 'none' : 'width 0.15s ease',
            }}>
              {/* The shared chat surface, unforked: agent=tinker + card routes
                  to the card's bundle; resumeId picks WHICH of the card's
                  chats (none = fresh). The key hard-remounts on chat switch so
                  composer/draft state never leaks across sessions. */}
              {chatSel.id !== undefined && (
                <ChatPanel
                  key={`tinker:${cardId}:${chatSel.id ?? `new-${chatSel.epoch}`}:${chatSel.epoch}`}
                  agent="tinker"
                  card={cardId}
                  origin="tinker"
                  resumeId={chatSel.id}
                  title="Think with TinkerAgent"
                  placeholder="hand the TinkerAgent a rough idea… (Enter to send)"
                  emptyGlyph="✦"
                  emptyHint="> tinker on this card — it sharpens ideas, asks the right questions, and keeps notes here"
                  showNewSession={false}
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
                      <NewSessionButton
                        label="New chat"
                        color="var(--text-info)"
                        onClick={onNewChat}
                      />
                    </>
                  }
                />
              )}
            </div>
          </>
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
