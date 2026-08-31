import React, { useState } from 'react'

// The one ask card. Inline in the owning chat, in the Inbox, and in the
// always-on-top overlay — all three render this, so an ask looks and behaves
// identically wherever you happen to answer it.
//
// Collapsed: who is asking, a one-line summary, the options.
// Expanded: the full command, every path, the reason, risk · zone, the session
// it belongs to and the agent path. That detail comes from the structured
// `interrupt_value`, which the wire record now carries — without it a card can
// only ever show a truncated summary, which is not enough to judge a
// permission request by.

const SURFACE_LABEL = {
  chat: 'Chat', voice: 'Voice', tinker: 'TinkerAgent',
  background: 'Background task', cli: 'CLI',
}

// Consent option metadata: nicer labels + whether the option is affirmative
// (accent) vs. a rejection (red). session/project are the Claude/Cursor-style
// "allow for this session / project" allowlist scopes — losing them would
// silently downgrade every permission grant to a one-shot.
const OPTION_META = {
  approve: { label: 'Approve once', affirmative: true },
  session: { label: 'Allow for session', affirmative: true },
  project: { label: 'Allow for project', affirmative: true },
  reject: { label: 'Reject', affirmative: false },
  yes: { label: 'Yes', affirmative: true },
  no: { label: 'No', affirmative: false },
}

function optMeta(opt) {
  const key = String(opt).toLowerCase()
  if (OPTION_META[key]) return OPTION_META[key]
  return { label: opt, affirmative: /allow|approve|yes|session|project/.test(key) }
}

// Defensive: the server formats bodies into readable text, but a raw JSON blob
// arriving from an older producer should still render as indented lines.
function prettyBody(body) {
  if (body == null) return ''
  if (typeof body !== 'string') {
    try { return JSON.stringify(body, null, 2) } catch { return String(body) }
  }
  const trimmed = body.trim()
  if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
    try { return JSON.stringify(JSON.parse(trimmed), null, 2) } catch { /* not JSON */ }
  }
  return body
}

// One-line gist for the collapsed state: the operation and its target, not the
// whole multi-line body.
function summarize(ask) {
  const iv = ask.interrupt_value || {}
  if (iv.type === 'task_runner_permission') {
    const paths = Array.isArray(iv.paths) ? iv.paths : (iv.paths ? [iv.paths] : [])
    const op = String(iv.operation || '').toUpperCase()
    const first = paths[0] || ''
    const more = paths.length > 1 ? ` +${paths.length - 1} more` : ''
    return `${op} ${first}${more}`.trim()
  }
  if (iv.type === 'permission_request') return iv.command || iv.reason || ''
  if (iv.type === 'user_question' || iv.type === 'orchestrator_ask') {
    return iv.question || ask.body || ''
  }
  return (ask.body || '').split('\n')[0]
}

function Chip({ children, title, tone = 'neutral', glow = false }) {
  const tones = {
    neutral: { color: 'var(--text-muted)', bg: 'rgba(255,255,255,0.05)', bd: 'rgba(255,255,255,0.10)', g: 'transparent' },
    amber: { color: 'var(--neon-amber)', bg: 'rgba(251,191,36,0.10)', bd: 'rgba(251,191,36,0.35)', g: 'rgba(251,191,36,0.25)' },
    cyan: { color: 'var(--text-cyan)', bg: 'rgba(34,211,238,0.10)', bd: 'rgba(34,211,238,0.35)', g: 'rgba(34,211,238,0.22)' },
    red: { color: 'var(--neon-red)', bg: 'rgba(255,51,102,0.10)', bd: 'rgba(255,51,102,0.35)', g: 'rgba(255,51,102,0.22)' },
  }
  const t = tones[tone] || tones.neutral
  return (
    <span title={title} style={{
      display: 'inline-flex', alignItems: 'center',
      fontFamily: 'var(--font-mono)', fontSize: 9, fontWeight: 'var(--fw-semibold)',
      letterSpacing: '0.1em', textTransform: 'uppercase',
      color: t.color, background: t.bg,
      border: `1px solid ${t.bd}`,
      borderRadius: 999, padding: '3px 9px', whiteSpace: 'nowrap',
      overflow: 'hidden', textOverflow: 'ellipsis', minWidth: 0,
      boxShadow: glow ? `0 0 14px ${t.g}` : 'none',
    }}>{children}</span>
  )
}

// A quiet "someone is waiting on you" pulse. The card is the only thing
// standing between an agent and the rest of its work, so it should read as
// live rather than as a static notice.
function WaitingDot({ color = 'var(--neon-amber)' }) {
  return (
    <span style={{
      width: 6, height: 6, borderRadius: '50%', flexShrink: 0,
      background: color, boxShadow: `0 0 10px ${color}`,
      animation: 'voice-idle 1.8s ease-in-out infinite',
    }} />
  )
}

function DetailRow({ label, children }) {
  if (children == null || children === '') return null
  return (
    <div style={{ display: 'flex', gap: 8, alignItems: 'flex-start' }}>
      <span style={{
        fontFamily: 'var(--font-mono)', fontSize: 9, color: 'var(--text-dim)',
        textTransform: 'uppercase', letterSpacing: '0.1em',
        minWidth: 58, paddingTop: 1, flexShrink: 0,
      }}>{label}</span>
      <span className="selectable" style={{
        fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--text-secondary)',
        wordBreak: 'break-word', whiteSpace: 'pre-wrap', flex: 1,
      }}>{children}</span>
    </div>
  )
}

function Chevron({ open }) {
  return (
    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" aria-hidden
         style={{ transform: `rotate(${open ? 90 : 0}deg)`, transition: 'transform 0.15s' }}>
      <polyline points="9 18 15 12 9 6" />
    </svg>
  )
}

export default function AskCard({
  ask,
  onAnswer,               // (ask, response) => Promise<boolean>
  answering = false,
  // Compact chrome for the overlay, where vertical space is scarce.
  dense = false,
  // Rendered top-right — the overlay's X (hide without answering).
  headerAction = null,
  defaultExpanded = false,
}) {
  const [expanded, setExpanded] = useState(defaultExpanded)
  const [freeText, setFreeText] = useState('')
  if (!ask) return null

  const iv = ask.interrupt_value || {}
  const options = ask.options || []
  const hasOptions = options.length > 0
  const isBackground = ask.surface === 'background'
    || !!(ask.agent_path && ask.agent_path.endsWith('#bg'))
  const who = ask.agent_label || SURFACE_LABEL[ask.surface] || 'Agent'
  const paths = Array.isArray(iv.paths) ? iv.paths : (iv.paths ? [iv.paths] : [])
  const summary = summarize(ask)

  const btnBase = {
    padding: dense ? '8px 14px' : '8px 16px',
    borderRadius: 999,
    fontSize: dense ? 11 : 11,
    fontFamily: 'var(--font-mono)',
    fontWeight: 'var(--fw-semibold)',
    letterSpacing: '0.04em',
    border: '1px solid',
    cursor: answering ? 'progress' : 'pointer',
    opacity: answering ? 0.55 : 1,
    transition: 'transform 0.12s ease, box-shadow 0.15s, background 0.15s',
  }
  // The first affirmative option is the one you'll reach for most, so it gets
  // the brand gradient and everything else stays quiet. Rejection is never
  // styled as the easy default — it's a real decision too.
  const primaryOpt = options.find((o) => optMeta(o).affirmative)

  const styleFor = (opt) => {
    const meta = optMeta(opt)
    if (opt === primaryOpt) {
      return {
        ...btnBase,
        border: '1px solid rgba(var(--accent-rgb),0.55)',
        background: 'rgba(var(--accent-rgb),0.16)',
        color: 'var(--neon-green)',
        boxShadow: '0 0 16px rgba(var(--accent-rgb),0.18)',
      }
    }
    if (meta.affirmative) {
      return {
        ...btnBase,
        color: 'var(--neon-green)',
        borderColor: 'rgba(var(--accent-rgb),0.30)',
        background: 'rgba(var(--accent-rgb),0.07)',
      }
    }
    return {
      ...btnBase,
      color: 'var(--neon-red)',
      borderColor: 'rgba(255,51,102,0.30)',
      background: 'rgba(255,51,102,0.07)',
    }
  }

  return (
    <div style={{
      position: 'relative',
      // Opaque on purpose. The overlay window is transparent, so a translucent
      // card would read as washed out against the desktop and let whatever
      // shares the window show through it.
      background: 'linear-gradient(160deg, #1c1c30 0%, #121220 100%)',
      border: '1px solid rgba(251,191,36,0.22)',
      borderRadius: 18,
      padding: dense ? '14px 16px 15px' : '16px 18px',
      display: 'flex',
      flexDirection: 'column',
      gap: dense ? 10 : 12,
      boxShadow: '0 10px 40px rgba(0,0,0,0.55), 0 0 30px rgba(251,191,36,0.07)',
      overflow: 'hidden',
    }}>
      {/* top shine — the hairline that makes the panel read as glass */}
      <span aria-hidden style={{
        position: 'absolute', top: 0, left: 14, right: 14, height: 1,
        background: 'linear-gradient(90deg, transparent, rgba(251,191,36,0.55), transparent)',
      }} />
      {/* header — who is asking, and what for */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, minWidth: 0 }}>
        <WaitingDot />
        <Chip tone="amber" glow>{iv.type === 'user_question' || iv.type === 'orchestrator_ask' ? 'Question' : 'Permission'}</Chip>
        <Chip tone={isBackground ? 'cyan' : 'neutral'} title={ask.agent_path || undefined}>{who}</Chip>
        {iv.risk_level && (
          <Chip tone={/high|critical/i.test(iv.risk_level) ? 'red' : 'neutral'}>
            {iv.risk_level} risk
          </Chip>
        )}
        <span style={{ marginLeft: 'auto', display: 'inline-flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>
          <button
            onClick={() => setExpanded((v) => !v)}
            title={expanded ? 'Hide details' : 'Show the full command, paths and reason'}
            className="tap-pop"
            style={{
              display: 'inline-flex', alignItems: 'center', gap: 4,
              background: 'rgba(255,255,255,0.05)',
              border: '1px solid var(--glass-border)',
              borderRadius: 999, cursor: 'pointer',
              color: expanded ? 'var(--text-secondary)' : 'var(--text-muted)',
              fontFamily: 'var(--font-mono)',
              fontSize: 9, letterSpacing: '0.08em', textTransform: 'uppercase',
              padding: dense ? 0 : '4px 10px',
              width: dense ? 22 : undefined, height: dense ? 22 : undefined,
              justifyContent: 'center',
            }}
          >
            <Chevron open={expanded} />
            {dense ? null : (expanded ? 'less' : 'details')}
          </button>
          {headerAction}
        </span>
      </div>

      {/* The headline sits on its own line so a long title never squeezes the
          chips or the controls off the row. */}
      <div style={{
        color: 'var(--text-primary)', fontWeight: 700,
        fontFamily: 'var(--font-ui)',
        fontSize: dense ? 15 : 14, letterSpacing: '-0.01em', lineHeight: 1.25,
        marginTop: -2,
      }}>
        {ask.title || 'Permission request'}
      </div>

      {/* collapsed: one line. expanded: everything needed to judge it. */}
      {!expanded ? (
        <div className="selectable" style={{
          fontFamily: 'var(--font-mono)', fontSize: dense ? 11 : 11,
          color: 'var(--text-secondary)', lineHeight: 1.5,
          background: 'rgba(0,0,0,0.28)',
          border: '1px solid var(--border-subtle)',
          borderRadius: 10, padding: '8px 10px',
          overflow: 'hidden', textOverflow: 'ellipsis',
          display: '-webkit-box', WebkitLineClamp: dense ? 3 : 2,
          WebkitBoxOrient: 'vertical',
        }}>
          {summary}
        </div>
      ) : (
        <div style={{
          display: 'flex', flexDirection: 'column', gap: 7,
          background: 'rgba(0,0,0,0.34)',
          border: '1px solid rgba(251,191,36,0.14)',
          borderRadius: 12, padding: '10px 12px',
          maxHeight: dense ? 190 : 320, overflowY: 'auto',
        }}>
          <DetailRow label="what">{prettyBody(ask.body)}</DetailRow>
          {iv.command ? <DetailRow label="command">{iv.command}</DetailRow> : null}
          {paths.length > 0 && <DetailRow label="paths">{paths.join('\n')}</DetailRow>}
          {iv.reason ? <DetailRow label="reason">{iv.reason}</DetailRow> : null}
          {(iv.zone || iv.risk_level) && (
            <DetailRow label="scope">
              {[iv.zone && `zone: ${iv.zone}`, iv.risk_level && `risk: ${iv.risk_level}`]
                .filter(Boolean).join(' · ')}
            </DetailRow>
          )}
          <DetailRow label="session">{ask.thread_id || ask.session_id || '—'}</DetailRow>
          {ask.card_id ? <DetailRow label="card">{ask.card_id}</DetailRow> : null}
          {ask.task_id ? <DetailRow label="task">{ask.task_id}</DetailRow> : null}
          <DetailRow label="agent">{ask.agent_path || who}</DetailRow>
        </div>
      )}

      {/* answer */}
      {hasOptions ? (
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          {options.map((opt) => (
            <button
              key={opt}
              onClick={() => !answering && onAnswer?.(ask, opt)}
              disabled={answering}
              className="tap-pop"
              style={styleFor(opt)}
            >
              {optMeta(opt).label}
            </button>
          ))}
        </div>
      ) : (
        <div style={{ display: 'flex', gap: 8 }}>
          <input
            type="text"
            value={freeText}
            onChange={(e) => setFreeText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && freeText.trim() && !answering) {
                onAnswer?.(ask, freeText.trim())
                setFreeText('')
              }
            }}
            placeholder="Type a reply and press Enter…"
            disabled={answering}
            style={{
              flex: 1, fontFamily: 'var(--font-mono)', fontSize: 11,
              background: 'rgba(0,0,0,0.35)', color: 'var(--text-primary)',
              border: '1px solid var(--glass-border)', borderRadius: 999,
              padding: '8px 14px', outline: 'none',
            }}
          />
          <button
            onClick={() => !answering && onAnswer?.(ask, freeText.trim() || 'reject')}
            disabled={answering}
            className="tap-pop"
            style={{
              ...btnBase,
              border: '1px solid rgba(var(--accent-rgb),0.55)',
              background: 'rgba(var(--accent-rgb),0.16)',
              color: 'var(--neon-green)',
              boxShadow: '0 0 16px rgba(var(--accent-rgb),0.18)',
            }}
          >
            Send
          </button>
        </div>
      )}
    </div>
  )
}
