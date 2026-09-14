import React, { useCallback, useEffect, useState } from 'react'
import { getUsageSessions, getUsageSummary } from '../../api/client'
import { fmtCost, fmtPct, fmtTokens } from '../chat/ContextAside'

// Settings → Usage: what every session has spent.
//
// Two calls per range: /usage/summary (totals + per-model + a per-day series,
// so the three views cannot disagree) and /usage/sessions (per-conversation,
// most recently active first). No charting dependency — the bars and the
// sparkline are a handful of divs and one inline <svg>.
//
// The honesty rule from the live panel carries over: a model with no price
// entry makes a total an undercount, so it is labelled rather than presented
// as a confident figure.

const RANGES = [
  { key: '24h', label: '24 h', seconds: 24 * 3600 },
  { key: '7d', label: '7 days', seconds: 7 * 24 * 3600 },
  { key: '30d', label: '30 days', seconds: 30 * 24 * 3600 },
  { key: 'all', label: 'All', seconds: null },
]

function Card({ label, value, hint }) {
  return (
    <div style={{
      flex: '1 1 120px', minWidth: 110,
      background: 'var(--bg-card)', border: '1px solid var(--border-card)',
      borderRadius: 'var(--radius-card)', padding: '10px 12px',
    }}>
      <div style={{
        fontFamily: 'var(--font-mono)', fontSize: 9, letterSpacing: '0.1em',
        textTransform: 'uppercase', color: 'var(--text-dim)',
      }}>{label}</div>
      <div style={{
        fontFamily: 'var(--font-mono)', fontSize: 16, marginTop: 4,
        color: 'var(--text-primary)',
      }}>{value}</div>
      {hint && (
        <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 2 }}>
          {hint}
        </div>
      )}
    </div>
  )
}

// Horizontal bars, widest first, scaled to the largest row.
function ModelBars({ rows }) {
  const max = Math.max(1, ...rows.map((r) => r.input_tokens + r.output_tokens))
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      {rows.map((r) => {
        const total = r.input_tokens + r.output_tokens
        return (
          <div key={r.key} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span style={{
              width: 150, fontSize: 11, color: 'var(--text-secondary)',
              overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
              fontFamily: 'var(--font-mono)',
            }} title={r.key}>{r.key || '—'}</span>
            <div style={{
              flex: 1, height: 6, borderRadius: 3,
              background: 'rgba(255,255,255,0.06)', overflow: 'hidden',
            }}>
              <div style={{
                width: `${Math.max(2, (total / max) * 100)}%`, height: '100%',
                borderRadius: 3, background: 'var(--neon-green)', opacity: 0.8,
              }} />
            </div>
            <span style={{
              width: 108, textAlign: 'right', fontSize: 10,
              fontFamily: 'var(--font-mono)', color: 'var(--text-muted)',
            }}>
              {r.calls} · {fmtTokens(total)}
            </span>
          </div>
        )
      })}
    </div>
  )
}

// Per-day tokens as a bar sparkline. Hand-rolled SVG: one chart is not worth a
// charting dependency, and the shape of the data (a short ordered series) is
// exactly what a polyline-free bar row does well.
function DaySeries({ rows }) {
  if (rows.length < 2) return null
  const max = Math.max(1, ...rows.map((r) => r.input_tokens + r.output_tokens))
  const W = 100
  const barW = W / rows.length
  return (
    <div>
      <svg
        viewBox={`0 0 ${W} 28`}
        preserveAspectRatio="none"
        style={{ width: '100%', height: 40, display: 'block' }}
      >
        {rows.map((r, i) => {
          const h = ((r.input_tokens + r.output_tokens) / max) * 26
          return (
            <rect
              key={r.key}
              x={i * barW + barW * 0.15}
              y={28 - Math.max(1, h)}
              width={barW * 0.7}
              height={Math.max(1, h)}
              fill="var(--neon-green)"
              opacity="0.75"
            >
              <title>{`${r.key}: ${fmtTokens(r.input_tokens + r.output_tokens)} tokens · ${r.calls} calls`}</title>
            </rect>
          )
        })}
      </svg>
      <div style={{
        display: 'flex', justifyContent: 'space-between',
        fontSize: 9, color: 'var(--text-dim)', fontFamily: 'var(--font-mono)',
      }}>
        <span>{rows[0].key}</span>
        <span>{rows[rows.length - 1].key}</span>
      </div>
    </div>
  )
}

function SessionTable({ rows }) {
  const th = {
    textAlign: 'left', fontFamily: 'var(--font-mono)', fontSize: 9,
    letterSpacing: '0.08em', textTransform: 'uppercase',
    color: 'var(--text-dim)', padding: '4px 8px', fontWeight: 400,
    whiteSpace: 'nowrap',
  }
  const td = {
    padding: '5px 8px', fontSize: 11, color: 'var(--text-secondary)',
    borderTop: '1px solid var(--border-subtle)', whiteSpace: 'nowrap',
  }
  const num = { ...td, fontFamily: 'var(--font-mono)', textAlign: 'right' }
  return (
    <div style={{ overflowX: 'auto' }}>
      <table style={{ borderCollapse: 'collapse', width: '100%', minWidth: 520 }}>
        <thead>
          <tr>
            <th style={th}>session</th>
            <th style={th}>origin</th>
            <th style={{ ...th, textAlign: 'right' }}>calls</th>
            <th style={{ ...th, textAlign: 'right' }}>in</th>
            <th style={{ ...th, textAlign: 'right' }}>cached</th>
            <th style={{ ...th, textAlign: 'right' }}>out</th>
            <th style={{ ...th, textAlign: 'right' }}>cost</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.thread_id || 'unattributed'}>
              <td style={{
                ...td, maxWidth: 240, overflow: 'hidden',
                textOverflow: 'ellipsis', whiteSpace: 'nowrap',
              }} title={`${r.title || r.thread_id}\n${(r.models || []).join(', ')}`}>
                {r.title || r.thread_id || 'unattributed'}
              </td>
              <td style={{ ...td, color: 'var(--text-muted)' }}>{r.origin || '—'}</td>
              <td style={num}>{r.calls}</td>
              <td style={num}>{fmtTokens(r.input_tokens)}</td>
              <td style={{ ...num, color: 'var(--text-muted)' }}>
                {r.cache_read_tokens
                  ? `${fmtPct(r.cache_read_tokens / Math.max(1, r.input_tokens))}`
                  : '—'}
              </td>
              <td style={num}>{fmtTokens(r.output_tokens)}</td>
              <td style={{ ...num, color: r.priced ? 'var(--text-secondary)' : 'var(--text-dim)' }}>
                {fmtCost(r.est_cost_usd, r.priced)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function UsageSettings() {
  const [range, setRange] = useState('7d')
  const [data, setData] = useState(null)
  const [error, setError] = useState(false)
  const [loading, setLoading] = useState(true)

  const load = useCallback(() => {
    const spec = RANGES.find((r) => r.key === range) || RANGES[1]
    // The API takes epoch SECONDS.
    const since = spec.seconds ? Math.floor(Date.now() / 1000 - spec.seconds) : null
    let alive = true
    setLoading(true)
    Promise.all([getUsageSummary(since), getUsageSessions(since, 100)])
      .then(([summary, sessions]) => {
        if (!alive) return
        setData({ summary, sessions: sessions?.rows || [] })
        setError(false)
      })
      .catch(() => { if (alive) setError(true) })
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [range])

  useEffect(() => load(), [load])

  if (error) {
    return (
      <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>
        Could not read usage — is the daemon running?
      </div>
    )
  }
  if (!data) {
    return <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>Loading…</div>
  }

  const { summary, sessions } = data
  const totals = summary?.totals || {}
  const unpriced = summary?.unpriced_models || []
  const cacheShare = totals.input_tokens
    ? totals.cache_read_tokens / totals.input_tokens
    : 0

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      {/* range picker + refresh */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        {RANGES.map((r) => (
          <button
            key={r.key}
            onClick={() => setRange(r.key)}
            style={{
              padding: '3px 10px', borderRadius: 'var(--radius-btn)',
              fontFamily: 'var(--font-mono)', fontSize: 10, cursor: 'pointer',
              background: range === r.key
                ? 'rgba(var(--accent-rgb), 0.12)' : 'transparent',
              border: `1px solid ${range === r.key
                ? 'rgba(var(--accent-rgb), 0.3)' : 'var(--border-subtle)'}`,
              color: range === r.key ? 'var(--neon-green)' : 'var(--text-muted)',
            }}
          >{r.label}</button>
        ))}
        <button
          onClick={load}
          title="Refresh"
          className="hover-bulge"
          style={{
            marginLeft: 'auto', background: 'transparent',
            border: '1px solid var(--border-subtle)',
            borderRadius: 'var(--radius-btn)', padding: '3px 8px',
            cursor: 'pointer', color: 'var(--text-muted)', fontSize: 11,
          }}
        >{loading ? '…' : '↻'}</button>
      </div>

      {!totals.calls ? (
        <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>
          No model calls recorded in this range.
        </div>
      ) : (
        <>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <Card label="calls" value={totals.calls} />
            <Card label="input" value={fmtTokens(totals.input_tokens)}
                  hint={totals.cache_read_tokens
                    ? `${fmtPct(cacheShare)} from cache` : undefined} />
            <Card label="output" value={fmtTokens(totals.output_tokens)} />
            <Card
              label="cost"
              value={unpriced.length && !totals.est_cost_usd
                ? 'unpriced'
                : fmtCost(totals.est_cost_usd, true)}
              hint={unpriced.length ? 'incomplete — see below' : undefined}
            />
          </div>

          {unpriced.length > 0 && (
            <div style={{
              fontSize: 11, color: 'var(--text-warning)', lineHeight: 1.6,
              background: 'rgba(255,255,255,0.03)',
              border: '1px solid var(--border-subtle)',
              borderRadius: 'var(--radius-card)', padding: '8px 10px',
            }}>
              No price entry for {unpriced.join(', ')}, so their spend counts as
              $0 and the total above is an undercount. Add them to{' '}
              <code style={{ fontFamily: 'var(--font-mono)' }}>
                ~/.yuyutsava/model_prices.json
              </code>{' '}
              as <code style={{ fontFamily: 'var(--font-mono)' }}>
                {'{"<model>": [$/1M in, $/1M out]}'}
              </code>. Prefixes match, so one entry covers a family.
            </div>
          )}

          {summary?.by_day?.length > 1 && (
            <div>
              <div style={{
                fontFamily: 'var(--font-mono)', fontSize: 10,
                letterSpacing: '0.1em', textTransform: 'uppercase',
                color: 'var(--text-dim)', marginBottom: 4,
              }}>tokens per day</div>
              <DaySeries rows={summary.by_day} />
            </div>
          )}

          {summary?.by_model?.length > 0 && (
            <div>
              <div style={{
                fontFamily: 'var(--font-mono)', fontSize: 10,
                letterSpacing: '0.1em', textTransform: 'uppercase',
                color: 'var(--text-dim)', marginBottom: 6,
              }}>by model</div>
              <ModelBars rows={summary.by_model} />
            </div>
          )}

          {sessions.length > 0 && (
            <div>
              <div style={{
                fontFamily: 'var(--font-mono)', fontSize: 10,
                letterSpacing: '0.1em', textTransform: 'uppercase',
                color: 'var(--text-dim)', marginBottom: 4,
              }}>by session ({sessions.length})</div>
              <SessionTable rows={sessions} />
            </div>
          )}

          <div style={{ fontSize: 11, color: 'var(--text-muted)', lineHeight: 1.6 }}>
            Cached input is charged at the full input rate here, so a
            well-cached conversation's cost is an upper bound. Deleting a
            session removes its usage rows with it.
          </div>
        </>
      )}
    </div>
  )
}
