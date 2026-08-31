import React, { useEffect, useState, useCallback } from 'react'
import { listSessions, listVisuals, visualUrl, listArtifacts, listAllAttachments } from '../../api/client'
import { kindAccent, humanAge } from './kinds'
import { resolveBlock } from '../todos/artifactBlocks'
import Lightbox from './Lightbox'
import ArtifactModal from './ArtifactModal'
import VisualActions from './VisualActions'
import { useNav } from '../../nav/NavProvider'
import { useViewState, useScrollRestore } from '../../nav/useViewState'

// A gallery of everything the agent has produced. Two feeds:
//   • Visuals (charts/diagrams/tables/…) served from /visuals, session-scoped.
//   • Artifacts & board files — general artifact_create outputs (/artifacts) and
//     every TODO card's attachments (/todos/attachments), rendered through the
//     shared block registry so an interactive HTML/JSX app, a doc, an audio clip
//     or a code file all show here and open big (ArtifactModal). Global, not
//     session-scoped, since general artifacts and card files aren't filed under
//     the visuals' session ids.

const BLOCK_ACCENT = {
  image: 'var(--text-info)', diagram: 'var(--text-info)', artifact: 'var(--text-info)',
  link: 'var(--text-lavender)', file: '#8fa1c7', video: '#f0a3c0', audio: '#7ee0c0',
}

function VisualCard({ v, onOpen, onDeleted }) {
  const accent = kindAccent(v.kind)
  return (
    <div
      className="hover-bulge"
      style={{
        display: 'flex', flexDirection: 'column', gap: 8, padding: 8,
        background: 'var(--bg-card)', border: `1px solid ${accent}55`,
        borderRadius: 10, textAlign: 'left', '--bulge-glow': `${accent}44`,
      }}
    >
      <img
        src={visualUrl(v.url)} alt={v.title || v.kind} loading="lazy"
        onClick={() => onOpen(v)} title={v.title || v.kind}
        style={{ width: '100%', height: 150, objectFit: 'contain', background: 'var(--bg-deep)', borderRadius: 6, cursor: 'zoom-in' }}
      />
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <span style={{
          fontFamily: 'var(--font-mono)', fontSize: 9, textTransform: 'uppercase',
          letterSpacing: '0.06em', color: accent, border: `1px solid ${accent}55`,
          borderRadius: 6, padding: '1px 6px',
        }}>{v.kind}</span>
        <span style={{
          fontSize: 11, color: 'var(--text-primary)', overflow: 'hidden',
          whiteSpace: 'nowrap', textOverflow: 'ellipsis', flex: 1,
        }}>{v.title || '—'}</span>
        <span style={{ fontSize: 10, color: 'var(--text-dim)' }}>{humanAge(v.created_ts)}</span>
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
    <figure
      style={{
        margin: 0, borderRadius: 10, overflow: 'hidden',
        border: `1px solid ${accent}44`, background: 'var(--bg-card)',
      }}
    >
      <figcaption style={{
        display: 'flex', alignItems: 'center', gap: 6, padding: '6px 10px',
        borderBottom: `1px solid ${accent}22`, fontFamily: 'var(--font-mono)',
      }}>
        <span style={{
          fontSize: 9, textTransform: 'uppercase', letterSpacing: '0.06em',
          color: accent, border: `1px solid ${accent}55`, borderRadius: 6,
          padding: '1px 6px',
        }}>{item.kind}</span>
        <span style={{
          fontSize: 11, color: 'var(--text-primary)', overflow: 'hidden',
          whiteSpace: 'nowrap', textOverflow: 'ellipsis', flex: 1,
        }}>{item.title || item.mime || 'artifact'}</span>
        <span style={{ fontSize: 9, color: 'var(--text-dim)' }} title={source === 'board' ? 'from a TODO card' : 'from chat/voice'}>{source}</span>
        <span style={{ fontSize: 10, color: 'var(--text-dim)' }}>{humanAge(item.created_ts)}</span>
        <button
          onClick={() => onExpand(item)}
          title="open big view"
          style={{
            background: 'transparent', color: accent, cursor: 'pointer',
            border: `1px solid ${accent}55`, borderRadius: 6,
            padding: '1px 7px', fontSize: 11, fontFamily: 'var(--font-mono)',
          }}
        >⤢</button>
      </figcaption>
      <div style={{ padding: 10, minWidth: 0, maxHeight: 260, overflow: 'hidden' }}>
        <Block attachment={item} cardId={item._cardId} />
      </div>
    </figure>
  )
}

const SectionTitle = ({ children }) => (
  <span style={{
    fontFamily: 'var(--font-mono)', fontSize: 11, letterSpacing: '0.1em',
    textTransform: 'uppercase', color: 'var(--text-primary)', fontWeight: 'var(--fw-semibold)',
  }}>{children}</span>
)

export default function ArtifactsPanel() {
  const { params, push, pop } = useNav()
  const [sessions, setSessions] = useState([])
  const [sessionId, setSessionId] = useViewState('sessionId', null)
  const [visuals, setVisuals] = useState([])
  const [items, setItems] = useState([])   // general artifacts + card attachments
  const [loading, setLoading] = useState(false)
  const [loadingItems, setLoadingItems] = useState(false)
  // Which item is open big is a depth level, so the back arrow closes the
  // modal exactly like Esc does — and a reload reopens it.
  const zoomed = params.visualId ? visuals.find((v) => v.visual_id === params.visualId) || null : null
  const expanded = params.artifactId ? items.find((i) => i.attachment_id === params.artifactId) || null : null
  const openVisual = useCallback((v) => push({ visualId: v.visual_id }), [push])
  const openArtifact = useCallback((it) => push({ artifactId: it.attachment_id }), [push])
  const scrollRef = useScrollRestore(!loadingItems)

  const onDeleted = useCallback((visualId) => {
    setVisuals((cur) => cur.filter((v) => v.visual_id !== visualId))
    // The lightbox was showing what just got deleted — step back out of it.
    if (params.visualId === visualId) pop()
  }, [params.visualId, pop])

  const refreshItems = useCallback(() => {
    setLoadingItems(true)
    Promise.all([
      listArtifacts().catch(() => []),
      listAllAttachments().catch(() => []),
    ])
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
    listSessions(null, 100).then((rows) => {
      setSessions(rows)
      if (rows.length) setSessionId((cur) => cur || rows[0].id)
    }).catch(() => {})
    refreshItems()
  }, [refreshItems])

  const refresh = useCallback((id) => {
    if (!id) return
    setLoading(true)
    listVisuals(id)
      .then(setVisuals)
      .catch(() => setVisuals([]))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { refresh(sessionId) }, [sessionId, refresh])

  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden', position: 'relative' }}>
      <div style={{
        display: 'flex', alignItems: 'center', gap: 10,
        padding: '14px 24px', borderBottom: '1px solid var(--border-subtle)', background: 'var(--bg-bar)',
      }}>
        <SectionTitle>Artifacts</SectionTitle>
      </div>

      <div ref={scrollRef} style={{ flex: 1, overflowY: 'auto', padding: '20px 24px' }}>
        {/* ── general artifacts + board attachments (global) ────────── */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14 }}>
          <SectionTitle>Documents, interactive & board files</SectionTitle>
          <button
            onClick={refreshItems} title="Refresh"
            style={{
              marginLeft: 'auto', fontFamily: 'var(--font-mono)', fontSize: 11,
              padding: '5px 10px', background: 'rgba(var(--accent-rgb),0.08)',
              color: 'var(--neon-green)', border: '1px solid rgba(var(--accent-rgb),0.25)',
              borderRadius: 6, cursor: 'pointer',
            }}
          >↻</button>
        </div>
        {loadingItems && (
          <div style={{ color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', fontSize: 12 }}>loading…</div>
        )}
        {!loadingItems && items.length === 0 && (
          <div style={{
            color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', fontSize: 12,
            padding: '4px 0 20px',
          }}>{'> no documents, interactive artifacts, or board files yet'}</div>
        )}
        {!loadingItems && items.length > 0 && (
          <div style={{
            display: 'grid', gap: 14, marginBottom: 26,
            gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))',
          }}>
            {items.map((it) => (
              <ArtifactTile key={it.attachment_id} item={it} onExpand={openArtifact} />
            ))}
          </div>
        )}

        {/* ── visuals (session-scoped) ──────────────────────────────── */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14 }}>
          <SectionTitle>Visuals</SectionTitle>
          <select
            value={sessionId || ''}
            onChange={(e) => setSessionId(e.target.value)}
            style={{
              marginLeft: 'auto', background: 'var(--bg-card)', color: 'var(--text-primary)',
              border: '1px solid var(--border-card)', borderRadius: 6, padding: '5px 8px',
              fontFamily: 'var(--font-mono)', fontSize: 11, maxWidth: 320,
            }}
          >
            {sessions.length === 0 && <option value="">no sessions</option>}
            {sessions.map((s) => (
              <option key={s.id} value={s.id}>
                {(s.workspace.split('/').filter(Boolean).pop() || s.id)} · {s.id.slice(0, 8)} · {s.origin}
              </option>
            ))}
          </select>
          <button
            onClick={() => refresh(sessionId)} title="Refresh"
            style={{
              fontFamily: 'var(--font-mono)', fontSize: 11, padding: '5px 10px',
              background: 'rgba(var(--accent-rgb),0.08)', color: 'var(--neon-green)',
              border: '1px solid rgba(var(--accent-rgb),0.25)', borderRadius: 6, cursor: 'pointer',
            }}
          >↻</button>
        </div>
        {loading && (
          <div style={{ color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', fontSize: 12 }}>loading…</div>
        )}
        {!loading && visuals.length === 0 && (
          <div style={{
            color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', fontSize: 12,
            padding: '4px 0',
          }}>{'> no visuals yet — ask the agent to chart or diagram something'}</div>
        )}
        {!loading && visuals.length > 0 && (
          <div style={{
            display: 'grid', gap: 14,
            gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))',
          }}>
            {visuals.map((v) => <VisualCard key={v.visual_id} v={v} onOpen={openVisual} onDeleted={onDeleted} />)}
          </div>
        )}
      </div>

      <Lightbox v={zoomed} onClose={pop} onDeleted={onDeleted} />
      <ArtifactModal
        attachment={expanded} cardId={expanded?._cardId}
        onClose={pop}
      />
    </div>
  )
}
