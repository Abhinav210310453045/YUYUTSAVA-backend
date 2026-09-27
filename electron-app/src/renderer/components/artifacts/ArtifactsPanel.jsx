import React, { useEffect, useState, useCallback } from 'react'
import {
  listSessions,
  listVisuals,
  visualUrl,
  listArtifacts,
  listAllAttachments,
} from '../../api/client'
import { kindAccent, humanAge } from './kinds'
import { resolveBlock } from '../todos/artifactBlocks'
import Lightbox from './Lightbox'
import ArtifactModal from './ArtifactModal'
import VisualActions from './VisualActions'
import { useNav } from '../../nav/NavProvider'
import { useViewState, useScrollRestore } from '../../nav/useViewState'
import './ArtifactsPanel.css'

// A gallery of everything the agent has produced. Two feeds:
//   • Visuals (charts/diagrams/tables/…) served from /visuals, session-scoped.
//   • Artifacts & board files — general artifact_create outputs (/artifacts) and
//     every TODO card's attachments (/todos/attachments), rendered through the
//     shared block registry so an interactive HTML/JSX app, a doc, an audio clip
//     or a code file all show here and open big (ArtifactModal). Global, not
//     session-scoped, since general artifacts and card files aren't filed under
//     the visuals' session ids.

const BLOCK_ACCENT = {
  image: 'var(--text-info)',
  diagram: 'var(--text-info)',
  artifact: 'var(--text-info)',
  link: 'var(--text-lavender)',
  file: '#8fa1c7',
  video: '#f0a3c0',
  audio: '#7ee0c0',
}

function VisualCard({ v, onOpen, onDeleted }) {
  const accent = kindAccent(v.kind)
  return (
    <div className="hover-bulge visual-card" style={{ '--kind': accent }}>
      <img
        src={visualUrl(v.url)}
        alt={v.title || v.kind}
        loading="lazy"
        onClick={() => onOpen(v)}
        title={v.title || v.kind}
        className="visual-card-image"
      />
      <div className="visual-card-caption">
        <span className="artifact-kind">{v.kind}</span>
        <span className="artifact-name">{v.title || '—'}</span>
        <span className="artifact-meta">{humanAge(v.created_ts)}</span>
      </div>
      <VisualActions visual={v} onDeleted={onDeleted} />
    </div>
  )
}

// One general artifact or card attachment, rendered by the block the registry
// resolves for it. `item._cardId` is set for card attachments (so blocks fetch
// from the card's attachment route); general artifacts carry no cardId and the
// block resolves bytes from their `url`.
function ArtifactTile({ item, onExpand }) {
  const Block = resolveBlock(item)
  const accent = BLOCK_ACCENT[item.kind] || 'var(--text-info)'
  const source = item._cardId ? 'board' : 'chat'
  return (
    <figure className="artifact-tile" style={{ '--kind': accent }}>
      <figcaption className="artifact-tile-caption">
        <span className="artifact-kind">{item.kind}</span>
        <span className="artifact-name">{item.title || item.mime || 'artifact'}</span>
        <span
          className="artifact-meta artifact-meta--source"
          title={source === 'board' ? 'from a TODO card' : 'from chat/voice'}
        >
          {source}
        </span>
        <span className="artifact-meta">{humanAge(item.created_ts)}</span>
        <button
          onClick={() => onExpand(item)}
          title="open big view"
          className="artifact-tile-expand"
        >
          ⤢
        </button>
      </figcaption>
      <div className="artifact-tile-body">
        <Block attachment={item} cardId={item._cardId} />
      </div>
    </figure>
  )
}

const SectionTitle = ({ children }) => <span className="artifacts-title">{children}</span>

export default function ArtifactsPanel() {
  const { params, push, pop } = useNav()
  const [sessions, setSessions] = useState([])
  const [sessionId, setSessionId] = useViewState('sessionId', null)
  const [visuals, setVisuals] = useState([])
  const [items, setItems] = useState([]) // general artifacts + card attachments
  const [loading, setLoading] = useState(false)
  const [loadingItems, setLoadingItems] = useState(false)
  // Which item is open big is a depth level, so the back arrow closes the
  // modal exactly like Esc does — and a reload reopens it.
  const zoomed = params.visualId
    ? visuals.find((v) => v.visual_id === params.visualId) || null
    : null
  const expanded = params.artifactId
    ? items.find((i) => i.attachment_id === params.artifactId) || null
    : null
  const openVisual = useCallback((v) => push({ visualId: v.visual_id }), [push])
  const openArtifact = useCallback((it) => push({ artifactId: it.attachment_id }), [push])
  const scrollRef = useScrollRestore(!loadingItems)

  const onDeleted = useCallback(
    (visualId) => {
      setVisuals((cur) => cur.filter((v) => v.visual_id !== visualId))
      // The lightbox was showing what just got deleted — step back out of it.
      if (params.visualId === visualId) pop()
    },
    [params.visualId, pop],
  )

  const refreshItems = useCallback(() => {
    setLoadingItems(true)
    Promise.all([listArtifacts().catch(() => []), listAllAttachments().catch(() => [])])
      .then(([arts, atts]) => {
        const merged = [
          ...arts.map((a) => ({ ...a })),
          ...atts.map((a) => ({ ...a, _cardId: a.card_id })),
        ].sort((x, y) => (y.created_ts || 0) - (x.created_ts || 0))
        setItems(merged)
      })
      .finally(() => setLoadingItems(false))
  }, [])

  useEffect(() => {
    listSessions(null, 100)
      .then((rows) => {
        setSessions(rows)
        if (rows.length) setSessionId((cur) => cur || rows[0].id)
      })
      .catch(() => {})
    refreshItems()
  }, [refreshItems, setSessionId])

  const refresh = useCallback((id) => {
    if (!id) return
    setLoading(true)
    listVisuals(id)
      .then(setVisuals)
      .catch(() => setVisuals([]))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    refresh(sessionId)
  }, [sessionId, refresh])

  return (
    <div className="artifacts">
      <div className="artifacts-bar">
        <SectionTitle>Artifacts</SectionTitle>
      </div>

      <div ref={scrollRef} className="artifacts-body">
        {/* ── general artifacts + board attachments (global) ────────── */}
        <div className="artifacts-section">
          <SectionTitle>Documents, interactive & board files</SectionTitle>
          <button
            onClick={refreshItems}
            title="Refresh"
            className="artifacts-refresh artifacts-refresh--end"
          >
            ↻
          </button>
        </div>
        {loadingItems && <div className="artifacts-note">loading…</div>}
        {!loadingItems && items.length === 0 && (
          <div className="artifacts-note artifacts-note--empty-items">
            {'> no documents, interactive artifacts, or board files yet'}
          </div>
        )}
        {!loadingItems && items.length > 0 && (
          <div className="artifacts-grid artifacts-grid--items">
            {items.map((it) => (
              <ArtifactTile key={it.attachment_id} item={it} onExpand={openArtifact} />
            ))}
          </div>
        )}

        {/* ── visuals (session-scoped) ──────────────────────────────── */}
        <div className="artifacts-section">
          <SectionTitle>Visuals</SectionTitle>
          <select
            value={sessionId || ''}
            onChange={(e) => setSessionId(e.target.value)}
            className="artifacts-session"
          >
            {sessions.length === 0 && <option value="">no sessions</option>}
            {sessions.map((s) => (
              <option key={s.id} value={s.id}>
                {s.workspace.split('/').filter(Boolean).pop() || s.id} · {s.id.slice(0, 8)} ·{' '}
                {s.origin}
              </option>
            ))}
          </select>
          <button onClick={() => refresh(sessionId)} title="Refresh" className="artifacts-refresh">
            ↻
          </button>
        </div>
        {loading && <div className="artifacts-note">loading…</div>}
        {!loading && visuals.length === 0 && (
          <div className="artifacts-note artifacts-note--empty-visuals">
            {'> no visuals yet — ask the agent to chart or diagram something'}
          </div>
        )}
        {!loading && visuals.length > 0 && (
          <div className="artifacts-grid artifacts-grid--visuals">
            {visuals.map((v) => (
              <VisualCard key={v.visual_id} v={v} onOpen={openVisual} onDeleted={onDeleted} />
            ))}
          </div>
        )}
      </div>

      <Lightbox v={zoomed} onClose={pop} onDeleted={onDeleted} />
      <ArtifactModal attachment={expanded} cardId={expanded?._cardId} onClose={pop} />
    </div>
  )
}
