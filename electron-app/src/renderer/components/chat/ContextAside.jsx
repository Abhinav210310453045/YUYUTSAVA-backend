import React from 'react'

// Live context + spend for one conversation, fed by the daemon's `usage`
// frames (see useConverse().usage). Two presentations of one snapshot:
//
//   <ContextMeter/>  one line for a screen header — always visible
//   <ContextAside/>  the full column — toggled, resizable
//
// The numbers come from the backend already formatted into segments, so this
// file never derives a figure the CLI panel does not also show; the two fronts
// read the same wire shape in the same order.
//
// Estimated vs measured stays visible: segment sizes are estimates (a provider
// reports one total for a prompt, not a figure per region) and carry "≈", the
// last call's figures are the provider's own and carry nothing. An unpriced
// model reads "unpriced", never "$0.00" — "we have no price for this" is not
// "this was free".

const APPROX = '≈'

export function fmtTokens(n) {
  const v = Math.max(0, Math.round(Number(n) || 0))
  if (v < 1000) return String(v)
  if (v < 1_000_000) {
    const k = v / 1000
    return k >= 100 ? `${Math.round(k)}k` : `${k.toFixed(1)}k`
  }
  const m = v / 1_000_000
  return m >= 100 ? `${Math.round(m)}M` : `${m.toFixed(1)}M`
}

export function fmtCost(usd, priced) {
  if (!priced) return 'unpriced'
  const v = Number(usd) || 0
  return v < 1 ? `$${v.toFixed(4)}` : `$${v.toFixed(2)}`
}

export function fmtPct(fraction) {
  const pct = Math.max(0, Math.min(1, Number(fraction) || 0)) * 100
  if (pct > 0 && pct < 1) return '<1%'
  return `${Math.round(pct)}%`
}

// Shared 4px meter, same idiom as the todo board's objective progress.
function Bar({ fraction, height = 4 }) {
  const pct = Math.max(0, Math.min(1, Number(fraction) || 0)) * 100
  return (
    <div style={{
      flex: 1, height, borderRadius: height / 2,
      background: 'rgba(255,255,255,0.06)', overflow: 'hidden',
    }}>
      <div style={{
        // A real 7% must not round to an invisible sliver: the bar exists to
        // deny "nothing is used".
        width: `${pct > 0 ? Math.max(2, pct) : 0}%`,
        height: '100%', borderRadius: height / 2,
        background: 'var(--neon-green)', opacity: 0.85,
        transition: 'width 0.3s ease',
      }} />
    </div>
  )
}

function Row({ label, value, dim, approx }) {
  return (
    <div style={{
      display: 'flex', alignItems: 'baseline', justifyContent: 'space-between',
      gap: 8, fontSize: 11,
    }}>
      <span style={{ color: 'var(--text-muted)' }}>{label}</span>
      <span style={{
        fontFamily: 'var(--font-mono)',
        color: dim ? 'var(--text-dim)' : 'var(--text-secondary)',
        whiteSpace: 'nowrap',
      }}>
        {approx ? APPROX : ''}{value}
      </span>
    </div>
  )
}

function SectionLabel({ children }) {
  return (
    <div style={{
      fontFamily: 'var(--font-mono)', fontSize: 10, letterSpacing: '0.1em',
      textTransform: 'uppercase', color: 'var(--text-dim)',
      marginTop: 4, marginBottom: 2,
    }}>
      {children}
    </div>
  )
}

// One-line meter for a screen header. Renders nothing until there is something
// real to say — a header that reads "0%" before the first call is a lie that
// looks like data.
export function ContextMeter({ usage, onClick, open }) {
  if (!usage) return null
  const cacheShare = usage.call?.cache_read_tokens
    ? ` · ↺${fmtPct(usage.call.cache_hit_fraction)}`
    : ''
  return (
    <button
      onClick={onClick}
      title={open ? 'Hide context panel' : 'Show context panel'}
      className="hover-bulge"
      style={{
        display: 'flex', alignItems: 'center', gap: 8,
        background: open ? 'rgba(var(--accent-rgb), 0.12)' : 'transparent',
        border: `1px solid ${open ? 'rgba(var(--accent-rgb), 0.3)' : 'var(--border-subtle)'}`,
        borderRadius: 'var(--radius-btn)', padding: '3px 8px',
        cursor: 'pointer', fontFamily: 'var(--font-mono)', fontSize: 10,
        color: 'var(--text-secondary)',
      }}
    >
      <span style={{ width: 34 }}><Bar fraction={usage.used_fraction} height={3} /></span>
      <span>{fmtPct(usage.used_fraction)}</span>
      <span style={{ color: 'var(--text-dim)' }}>
        in {fmtTokens(usage.call?.input_tokens)}{cacheShare}
      </span>
    </button>
  )
}

export default function ContextAside({ usage }) {
  return (
    <div style={{
      display: 'flex', flexDirection: 'column', gap: 6,
      padding: '14px 14px 20px', overflowY: 'auto', height: '100%',
      background: 'var(--bg-panel)',
    }}>
      <div style={{
        fontFamily: 'var(--font-mono)', fontSize: 10, letterSpacing: '0.1em',
        textTransform: 'uppercase', color: 'var(--neon-green)',
      }}>
        context
      </div>

      {!usage ? (
        <div style={{ fontSize: 11, color: 'var(--text-dim)', lineHeight: 1.6 }}>
          No model call yet. The window, the last call and this session's spend
          appear here as soon as the agent runs.
        </div>
      ) : (
        <>
          <div style={{
            fontSize: 11, color: 'var(--text-secondary)',
            fontFamily: 'var(--font-mono)', wordBreak: 'break-all',
          }}>
            {usage.model || 'model unknown'}
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 2 }}>
            <Bar fraction={usage.used_fraction} />
            <span style={{
              fontFamily: 'var(--font-mono)', fontSize: 11,
              color: 'var(--text-primary)', whiteSpace: 'nowrap',
            }}>
              {fmtPct(usage.used_fraction)}
            </span>
          </div>
          <div style={{
            fontSize: 10, color: 'var(--text-dim)', fontFamily: 'var(--font-mono)',
          }}>
            {fmtTokens(usage.used_tokens)} / {fmtTokens(usage.max_input_tokens)}
          </div>
          {usage.compact_trigger_tokens > 0
            && usage.used_tokens >= usage.compact_trigger_tokens && (
            <div style={{ fontSize: 10, color: 'var(--neon-amber)' }}>
              compaction imminent — older turns will be summarised
            </div>
          )}

          <SectionLabel>window</SectionLabel>
          {(usage.segments || []).map((s) => (
            <Row key={s.key} label={s.label} value={fmtTokens(s.tokens)} approx />
          ))}
          <Row label="free" value={fmtTokens(usage.free_tokens)} approx dim />

          <SectionLabel>last call #{usage.call?.n ?? 0}</SectionLabel>
          <Row label="in" value={fmtTokens(usage.call?.input_tokens)} />
          {usage.call?.cache_read_tokens > 0 && (
            <Row
              label="from cache"
              value={`${fmtTokens(usage.call.cache_read_tokens)} · ${fmtPct(usage.call.cache_hit_fraction)}`}
            />
          )}
          <Row label="out" value={fmtTokens(usage.call?.output_tokens)} />
          <Row
            label="cost"
            value={fmtCost(usage.call?.est_cost_usd, usage.call?.priced)}
            dim={!usage.call?.priced}
          />

          <SectionLabel>session</SectionLabel>
          <Row label="calls" value={usage.session?.calls ?? 0} />
          <Row label="in" value={fmtTokens(usage.session?.input_tokens)} />
          <Row label="out" value={fmtTokens(usage.session?.output_tokens)} />
          {usage.session?.cache_read_tokens > 0 && (
            <Row label="cached" value={fmtTokens(usage.session.cache_read_tokens)} />
          )}
          <Row
            label="cost"
            value={fmtCost(usage.session?.est_cost_usd, usage.call?.priced)}
            dim={!usage.call?.priced}
          />
          <Row label="compacted" value={usage.session?.compactions ?? 0} />
          <Row label="offloaded" value={usage.session?.offloads ?? 0} />
          {usage.offloaded_digests > 0 && (
            <Row label="digests in ctx" value={usage.offloaded_digests} dim />
          )}

          <div style={{
            marginTop: 10, fontSize: 10, color: 'var(--text-dim)', lineHeight: 1.6,
          }}>
            {APPROX} estimated{usage.calibrated ? '' : ', uncalibrated'}.
            Last-call figures are the provider's own.
            {!usage.call?.priced && ' No price entry for this model, so cost is unknown rather than zero.'}
          </div>
        </>
      )}
    </div>
  )
}
